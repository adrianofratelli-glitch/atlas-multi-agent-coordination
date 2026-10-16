import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Hashable

from . import atlas_compat as ac

logger = logging.getLogger(__name__)

KB_VECTOR_INDEX = "kb_autoembed_v1"
KB_LEXICAL_INDEX = "kb_lexical_v1"
KB_PROJECT_FIELDS = ("article_id", "title", "content", "category")
# Sinônimos do Atlas Search (2026-10): coleção-fonte no MESMO banco do índice e mapeamento com
# o MESMO analyzer do campo (lucene.portuguese), como a doc exige.
KB_SYNONYMS_COLLECTION = "kb_synonyms"
KB_SYNONYM_MAPPING = "kb_sinonimos"
# Config viva do retrieval ("agents are data"): `agent_registry.<agente>.retrieval.fusion` (pesos,
# candidatos por perna, fuzzy, sinônimos) e `.retrieval.rerank`. O registry já é relido a cada hop
# (orchestration_graph), então a mudança vale no turno seguinte SEM round-trip extra — uma
# collection própria custava +~250 ms sequenciais por turno (medido no eval do KB).
KB_LEGS = ("vector", "lexical")
DEFAULT_KB_BRANCH = 20
KB_LIMIT = 4
MAX_QUERY_CHARS = 2000
STRATEGIES = ("rankFusion", "rankFusion+rerank", "rerank_indisponivel", "rankFusion_basico",
              "rrf_aplicacao", "vetor", "rrf_local")

_capabilities = ac.CapabilityCache(ttl_seconds=300)


def reciprocal_rank_fusion(
    rankings: list[list[dict]], *, key: str = "_id", k: int = 60, limit: int = 5
) -> list[dict]:
    """Combina rankings lexical e vetorial sem comparar escalas de score.

    Continua sendo o caminho de DEMO_MODE/CI (ranking local, sem Atlas). No Atlas real a
    fusão roda server-side em build_kb_rank_fusion_pipeline — mesma matemática, uma ida ao banco.
    """
    scores: dict[Hashable, float] = defaultdict(float)
    documents: dict[Hashable, dict] = {}
    for ranking in rankings:
        for position, document in enumerate(ranking, start=1):
            document_key = document[key]
            documents[document_key] = document
            scores[document_key] += 1 / (k + position)
    ordered = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [{**documents[item], "rrf_score": round(scores[item], 6)} for item in ordered]


def kb_config(doc: Any) -> tuple[dict, list[str]]:
    """Documento de config → dict com defaults seguros + avisos. Nunca levanta."""
    cfg, warns = ac.FusionConfig.from_doc(doc, legs=KB_LEGS, min_branch=KB_LIMIT, max_branch=100,
                                          default_branch=DEFAULT_KB_BRANCH)
    synonyms = doc.get("synonyms", True) if isinstance(doc, dict) else True
    if not isinstance(synonyms, bool):
        synonyms, warns = True, warns + ["synonyms não é booleano; usando true"]
    return {"weights": cfg.weights, "per_branch_limit": cfg.per_branch_limit,
            "num_candidates": cfg.num_candidates, "fuzzy_max_edits": cfg.fuzzy_max_edits,
            "score_details": cfg.score_details, "synonyms": synonyms}, warns


def _retrieval_block(agent_doc: Any, key: str):
    if isinstance(agent_doc, dict) and isinstance(agent_doc.get("retrieval"), dict):
        return agent_doc["retrieval"].get(key)
    return None


def rerank_config(agent_doc: Any) -> tuple[dict, list[str]]:
    """`agent_registry.<agente>.retrieval.rerank` → config validada (desligada se faltar)."""
    raw = _retrieval_block(agent_doc, "rerank")
    cfg, warns = ac.RerankConfig.from_doc(raw)
    return {"enabled": cfg.enabled, "model": cfg.model, "pool": max(cfg.pool, KB_LIMIT),
            "top_n": min(cfg.top_n, KB_LIMIT)}, warns


def build_kb_vector_pipeline(query: str, *, limit: int = 10, category: str | None = None,
                             num_candidates: int | None = None) -> list[dict]:
    """Perna semântica: Atlas Vector Search com Automated Embedding (voyage-4)."""
    stage: dict[str, Any] = {
        "index": KB_VECTOR_INDEX,
        "path": "content",
        "query": {"text": query},
        "model": "voyage-4",
        "numCandidates": num_candidates or max(50, limit * 5),
        "limit": limit,
    }
    if category:
        stage["filter"] = {"category": category}   # pré-filtro no servidor (campo filter do índice)
    return [
        {"$vectorSearch": stage},
        {"$project": {field: 1 for field in KB_PROJECT_FIELDS} | {"_id": 0}},
    ]


def build_kb_lexical_pipeline(query: str, *, limit: int = 10, category: str | None = None,
                              fuzzy_max_edits: int = 0, synonyms: bool = False) -> list[dict]:
    """Perna lexical: BM25 com boost no título (analyzer português).

    Sinônimos e fuzzy são cláusulas `should` SEPARADAS: a doc do operador `text` proíbe os
    dois na mesma cláusula, e pede `matchCriteria` sempre que houver `synonyms`.
    """
    should: list[dict] = [
        {"text": {"query": query, "path": "title", "score": {"boost": {"value": 2}}}},
        {"text": {"query": query, "path": "content"}},
    ]
    if synonyms:
        should.append({"text": {"query": query, "path": ["title", "content"],
                                "synonyms": KB_SYNONYM_MAPPING, "matchCriteria": "any"}})
    if fuzzy_max_edits:
        should.append({"text": {"query": query, "path": ["title", "content"],
                                "fuzzy": {"maxEdits": fuzzy_max_edits}, "score": {"boost": {"value": 0.5}}}})
    compound: dict[str, Any] = {"should": should, "minimumShouldMatch": 1}
    if category:
        compound["filter"] = [{"equals": {"path": "category", "value": category}}]
    return [
        {"$search": {"index": KB_LEXICAL_INDEX, "compound": compound}},
        {"$limit": limit},
        {"$project": {field: 1 for field in KB_PROJECT_FIELDS} | {"_id": 0}},
    ]


def build_kb_rank_fusion_pipeline(query: str, *, limit: int = KB_LIMIT, config: dict | None = None,
                                  rerank: dict | None = None, category: str | None = None,
                                  basic: bool = False) -> list[dict]:
    """Híbrido server-side: $rankFusion funde as duas pernas dentro do banco (MongoDB 8.0+).

    Uma agregação em vez de dois round-trips + fusão em Python. `scoreDetails` explica cada
    artigo (rank, peso, contribuição por perna); `$rerank` (opcional) reordena DEPOIS da fusão —
    dentro dela o servidor rejeita (code 12108704) — e o score da fusão é copiado antes, porque
    o `$rerank` substitui `$meta: "score"`. `basic=True` tira sinônimos e fuzzy (fallback).
    """
    cfg = config or kb_config(None)[0]
    branch = max(cfg["per_branch_limit"], rerank["pool"] if rerank else 0)
    vector = build_kb_vector_pipeline(query, limit=branch, category=category,
                                      num_candidates=max(cfg["num_candidates"], branch))[:1]
    lexical = build_kb_lexical_pipeline(query, limit=branch, category=category,
                                        fuzzy_max_edits=0 if basic else cfg["fuzzy_max_edits"],
                                        synonyms=False if basic else cfg["synonyms"])[:2]
    pool = rerank["pool"] if rerank else limit
    stages = [
        ac.build_rank_fusion({"vector": vector, "lexical": lexical}, weights=cfg["weights"],
                             score_details=cfg["score_details"]),
        {"$limit": pool},
        ac.fusion_meta_stage(score_details=cfg["score_details"]),
    ]
    if rerank:
        stages += ac.build_rerank_stages(query, ["title", "content"],
                                         ac.RerankConfig(enabled=True, model=rerank["model"],
                                                         pool=pool, top_n=limit))
    stages += [
        {"$limit": limit},
        {"$project": {field: 1 for field in KB_PROJECT_FIELDS}
         | {"_id": 0, "rrf_score": "$fusion_score", "score_details": 1, "rerank_score": 1}},
    ]
    return stages


@dataclass
class KbSearch:
    """Resultado do retrieval do KB, com o que a timeline precisa para explicar."""
    articles: list[dict]
    strategy: str
    fallbacks: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    rerank: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    search_ms: float = 0.0
    rerank_ms: float | None = None

    def why(self) -> list[dict]:
        """Por artigo: rank/peso/contribuição por perna (None = detalhe indisponível)."""
        return [{"article_id": a.get("article_id"), "explain": ac.explain_dict(a.get("score_details")),
                 "rerank_score": a.get("rerank_score") if isinstance(a.get("rerank_score"), (int, float)) else None}
                for a in self.articles[:KB_LIMIT]]


def classify(exc: BaseException) -> str:
    return ac.classify_stage_error(exc)


def _cap_failed(name: str, exc: BaseException) -> str:
    return _capabilities.record_failure(name, exc)


async def search_kb_explained(store, message: str, *, agent_doc: dict | None = None,
                              category: str | None = None, strategy: str | None = None,
                              local_rank=None) -> KbSearch:
    """Cascata (strategy sempre um valor de STRATEGIES):
    rankFusion(+rerank) → rerank_indisponivel → rankFusion_basico (sem sinônimos/fuzzy) →
    rrf_aplicacao (duas pernas em paralelo) → vetor → rrf_local (DEMO_MODE/CI).
    `strategy` força um caminho (eval): "rrf_aplicacao", "rankFusion", "rankFusion+rerank".
    """
    message = (message if isinstance(message, str) else "")[:MAX_QUERY_CHARS] or " "
    category = category if isinstance(category, str) and category else None
    started = perf_counter()
    cfg, warns = kb_config(_retrieval_block(agent_doc, "fusion"))
    rr, rwarns = rerank_config(agent_doc)
    want_rerank = rr["enabled"] if strategy is None else strategy == "rankFusion+rerank"
    out = KbSearch([], "rrf_local", config={k: cfg[k] for k in ("weights", "per_branch_limit", "fuzzy_max_edits", "synonyms")},
                   rerank={"enabled": want_rerank, "model": rr["model"], "pool": rr["pool"], "status": "off",
                           "preview": rr["model"].startswith("rerank-3")},
                   warnings=(warns + [f"rerank: {w}" for w in rwarns])[:5])

    async def fused(*, rerank: dict | None, basic: bool) -> list[dict]:
        return await store.aggregate("kb_articles", build_kb_rank_fusion_pipeline(
            message, limit=KB_LIMIT, config=cfg, rerank=rerank, category=category, basic=basic))

    if not store.memory and strategy != "rrf_aplicacao":
        if want_rerank and _capabilities.usable("rerank"):
            t0 = perf_counter()
            try:
                articles = await fused(rerank=rr, basic=False)
                out.rerank_ms = round((perf_counter() - t0) * 1000, 1)
                out.rerank["status"] = "ran"
                _capabilities.record_success("rerank")
                if articles:
                    out.articles, out.strategy = articles, "rankFusion+rerank"
            except Exception as exc:  # noqa: BLE001 — $rerank rejeitado → no-op
                out.fallbacks.append(f"rerank {_cap_failed('rerank', exc)}: {type(exc).__name__}")
                out.rerank["status"] = "unavailable"
        elif want_rerank:
            out.rerank["status"] = "unavailable"
            out.fallbacks.append("rerank: indisponível (cache de capability)")
        if not out.articles and _capabilities.usable("rank_fusion"):
            # Sinônimos/fuzzy têm capability PRÓPRIA: índice sem o mapeamento (demo antes do
            # reset) não pode derrubar a fusão inteira por 5 min — só a cláusula extra.
            rich = _capabilities.usable("kb_synonyms")
            try:
                articles = await fused(rerank=None, basic=not rich)
                _capabilities.record_success("rank_fusion")
                if articles:
                    out.articles = articles
                    out.strategy = ("rerank_indisponivel" if want_rerank
                                    else "rankFusion" if rich else "rankFusion_basico")
                if not rich:
                    out.fallbacks.append("sinônimos/fuzzy: indisponíveis (cache de capability)")
            except Exception as exc:  # noqa: BLE001
                kind = classify(exc)
                out.fallbacks.append(f"rankFusion {kind}: {type(exc).__name__}")
                articles = None
                if rich:
                    try:  # sem sinônimos e fuzzy: se ISTO funciona, o problema era a cláusula extra
                        articles = await fused(rerank=None, basic=True)
                        _capabilities.record_success("rank_fusion")
                        if kind == "unsupported":
                            _capabilities.record_failure("kb_synonyms", exc)
                        if articles:
                            out.articles, out.strategy = articles, "rankFusion_basico"
                    except Exception as exc2:  # noqa: BLE001
                        out.fallbacks.append(f"rankFusion_basico: {type(exc2).__name__}")
                        _cap_failed("rank_fusion", exc2)
                else:
                    _cap_failed("rank_fusion", exc)
        elif not out.articles:
            out.fallbacks.append("rankFusion: indisponível (cache de capability)")
    if not out.articles and not store.memory:
        out.articles, out.strategy = await _app_rrf(store, message, cfg, category, out.fallbacks)
    if not out.articles:
        articles = await store.find_many("kb_articles", {"category": category} if category else {}, limit=100)
        rank = local_rank or (lambda items, _m, _f: items)
        lexical = rank(articles, message, ("title", "content"))
        semantic = rank(articles, message, ("category", "title"))
        for item in lexical + semantic:
            item["_id"] = item.get("article_id")
        out.articles = ac.app_rrf({"lexical": lexical, "vector": semantic}, limit=KB_LIMIT,
                                  score_field="rrf_score")
        out.strategy = "rrf_local"
        if want_rerank and store.memory:
            out.rerank["status"] = "unavailable"
            out.fallbacks.append("rerank: sem Atlas (DEMO_MODE)")
    # invariantes: no máximo KB_LIMIT, sem artigo repetido (fuzz pegou dublê devolvendo 8)
    unique, seen = [], set()
    for item in out.articles:
        key = item.get("article_id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    out.articles = unique[:KB_LIMIT]
    for item in out.articles:
        item.pop("_id", None)
        if "rrf_score" not in item and isinstance(item.get("fusion_score"), (int, float)):
            item["rrf_score"] = item["fusion_score"]
    out.search_ms = round((perf_counter() - started) * 1000, 1)
    return out


async def _app_rrf(store, message: str, cfg: dict, category: str | None,
                   fallbacks: list[str]) -> tuple[list[dict], str]:
    async def execute(pipeline: list[dict]) -> list[dict]:
        cursor = await store._collection("kb_articles").aggregate(pipeline)
        return await cursor.to_list(None)
    branch = cfg["per_branch_limit"]
    vector, lexical = await asyncio.gather(
        execute(build_kb_vector_pipeline(message, limit=branch, category=category)),
        execute(build_kb_lexical_pipeline(message, limit=branch, category=category)),
        return_exceptions=True)
    if isinstance(vector, BaseException) and isinstance(lexical, BaseException):
        fallbacks.append(f"rrf_aplicacao: {type(vector).__name__}")
        return [], "rrf_local"
    legs = {}
    if not isinstance(vector, BaseException):
        legs["vector"] = vector
    else:
        fallbacks.append(f"vector: {type(vector).__name__}")
    if not isinstance(lexical, BaseException):
        legs["lexical"] = lexical
    else:
        fallbacks.append(f"lexical: {type(lexical).__name__}")
    for items in legs.values():
        for item in items:
            item["_id"] = item.get("article_id")
    fused = ac.app_rrf(legs, weights=cfg["weights"], limit=KB_LIMIT, score_field="rrf_score")
    return fused, ("rrf_aplicacao" if len(legs) == 2 else "vetor" if "vector" in legs else "rrf_aplicacao")
