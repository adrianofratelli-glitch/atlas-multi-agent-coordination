"""Helpers de $rankFusion/$rerank/scoreDetails: pov-shared quando instalado, equivalente local senão.

O README promete que o repo funciona SEM pov-shared. Com ele (>= 0.3.0) usamos
`atlas_retrieval` (validação completa, ChangeStreamWorker); sem ele, este módulo expõe uma
implementação local com o MESMO contrato para o que o multiagente usa — fusão, RRF na
aplicação no formato do scoreDetails do servidor, parse defensivo, configs vivas com defaults
seguros, estágios de $rerank e cache de capability. `SHARED` diz qual caminho está ativo;
`tests/test_atlas_compat.py` cobre os dois.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

try:  # pov-shared >= 0.3.0
    import atlas_retrieval as _shared
    SHARED = True
except ImportError:  # pragma: no cover — coberto por test_atlas_compat (caminho local)
    _shared = None
    SHARED = False

RRF_K = 60
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")


def _clean_weights(weights: Mapping[str, Any] | None, legs: Iterable[str]) -> dict[str, float]:
    out = {}
    for leg in legs:
        value = (weights or {}).get(leg, 1.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value < 0:
            value = 1.0
        out[leg] = float(value)
    return out


def _int_in(value: Any, default: int, lo: int, hi: int) -> tuple[int, bool]:
    if value is None:
        return default, False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or int(value) != value:
        return default, True
    clamped = min(hi, max(lo, int(value)))
    return clamped, clamped != value


@dataclass(frozen=True)
class LocalFusionConfig:
    weights: dict[str, float]
    per_branch_limit: int = 20
    num_candidates: int = 100
    fuzzy_max_edits: int = 1
    score_details: bool = True

    @classmethod
    def from_doc(cls, doc: Any, *, legs: Iterable[str], min_branch: int = 1, max_branch: int = 200,
                 default_branch: int = 20):
        legs = tuple(legs)
        src = doc if isinstance(doc, Mapping) else {}
        warns = [] if doc is None or isinstance(doc, Mapping) else ["config não é objeto"]
        raw_w = src.get("weights") if isinstance(src.get("weights"), Mapping) else None
        weights = _clean_weights(raw_w, legs)
        if weights and all(v == 0 for v in weights.values()):
            weights, warns = {leg: 1.0 for leg in legs}, warns + ["todos os pesos zero"]
        branch, bad = _int_in(src.get("per_branch_limit"), min(max_branch, max(min_branch, default_branch)),
                              min_branch, max_branch)
        warns += ["per_branch_limit inválido"] if bad else []
        cands, bad = _int_in(src.get("num_candidates"), max(100, branch * 5), branch, 2000)
        warns += ["num_candidates inválido"] if bad else []
        fuzzy, bad = _int_in(src.get("fuzzy_max_edits"), 1, 0, 2)
        warns += ["fuzzy_max_edits inválido"] if bad else []
        details = src.get("score_details", True)
        if not isinstance(details, bool):
            details, warns = True, warns + ["score_details inválido"]
        return cls(weights, branch, cands, fuzzy, details), warns


@dataclass(frozen=True)
class LocalRerankConfig:
    enabled: bool = False
    model: str = "rerank-2.5-lite"
    pool: int = 20
    top_n: int = 5

    @classmethod
    def from_doc(cls, doc: Any):
        src = doc if isinstance(doc, Mapping) else {}
        warns = []
        enabled = src.get("enabled", False)
        if not isinstance(enabled, bool):
            enabled, warns = False, warns + ["enabled inválido"]
        model = src.get("model", cls.model)
        if not isinstance(model, str) or not _MODEL_RE.match(model):
            model, warns = cls.model, warns + ["modelo inválido"]
        pool, bad = _int_in(src.get("pool"), cls.pool, 1, 1000)
        top_n, bad2 = _int_in(src.get("top_n"), cls.top_n, 1, pool)
        warns += ["pool inválido"] * bad + ["top_n inválido"] * bad2
        return cls(enabled, model, pool, min(pool, top_n)), warns


def _local_build_rank_fusion(pipelines, *, weights=None, score_details=False) -> dict:
    stage: dict[str, Any] = {"input": {"pipelines": dict(pipelines)}}
    if weights is not None:
        stage["combination"] = {"weights": _clean_weights(weights, pipelines)}
    if score_details:
        stage["scoreDetails"] = True
    return {"$rankFusion": stage}


def _local_fusion_meta_stage(*, score_details: bool, score_field: str = "fusion_score",
                             details_field: str = "score_details") -> dict:
    fields: dict[str, Any] = {score_field: {"$meta": "score"}}
    if score_details:
        fields[details_field] = {"$meta": "scoreDetails"}
    return {"$addFields": fields}


def _local_app_rrf(rankings, *, key="_id", k=RRF_K, weights=None, limit=None,
                   score_field="fusion_score", details_field="score_details") -> list[dict]:
    w = _clean_weights(weights, rankings)
    scores, docs, ranks, order = {}, {}, {}, []
    for leg, ranking in rankings.items():
        seen, position = set(), 0
        for doc in ranking or []:
            if not isinstance(doc, dict) or key not in doc or str(doc[key]) in seen:
                continue
            hkey = str(doc[key])
            seen.add(hkey)
            position += 1
            if hkey not in docs:
                docs[hkey], scores[hkey] = doc, 0.0
                order.append(hkey)
            scores[hkey] += w[leg] / (k + position)
            ranks.setdefault(hkey, {})[leg] = position
    ordered = sorted(order, key=lambda h: (-scores[h], order.index(h)))[:limit]
    return [{**docs[h], score_field: scores[h], details_field: {"value": scores[h], "details": [
        {"inputPipelineName": leg, "rank": ranks[h][leg], "weight": w[leg]} if leg in ranks[h]
        else {"inputPipelineName": leg, "rank": "NA"} for leg in rankings]}} for h in ordered]


def _local_parse_score_details(raw: Any, *, k: int = RRF_K):
    try:
        details = raw.get("details") if isinstance(raw, Mapping) else None
        if not isinstance(details, list) or not details:
            return None
        legs = []
        for entry in details:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("inputPipelineName"), str) \
                    or not entry["inputPipelineName"]:
                return None
            rank = entry.get("rank")
            rank = int(rank) if isinstance(rank, (int, float)) and not isinstance(rank, bool) and rank >= 1 else None
            weight = entry.get("weight")
            weight = float(weight) if isinstance(weight, (int, float)) and not isinstance(weight, bool) \
                and math.isfinite(weight) else None
            legs.append({"name": entry["inputPipelineName"][:64], "rank": rank, "weight": weight,
                         "contribution": 0.0 if rank is None else (weight if weight is not None else 1.0) / (k + rank)})
        value = raw.get("value")
        return _LocalExplain(value if isinstance(value, (int, float)) and math.isfinite(value) else None, legs)
    except Exception:  # noqa: BLE001 — formato não garantido pela doc: nunca derruba
        return None


@dataclass(frozen=True)
class _LocalExplain:
    value: float | None
    legs: list

    def as_dict(self) -> dict:
        return {"value": self.value, "legs": [dict(leg, contribution=round(leg["contribution"], 6)) for leg in self.legs]}


def _local_build_rerank_stages(query, path, cfg, *, out_field="rerank_score") -> list[dict]:
    paths = [path] if isinstance(path, str) else list(path)
    text = query if isinstance(query, str) and query.strip() else " "
    return [{"$set": {p: {"$ifNull": [f"${p}", ""]} for p in paths}},
            {"$rerank": {"query": {"text": text[:4000]}, "path": paths[0] if len(paths) == 1 else paths,
                         "numDocsToRerank": cfg.pool, "model": cfg.model}},
            {"$addFields": {out_field: {"$meta": "score"}}},
            {"$limit": cfg.top_n}]


def _local_classify(exc: BaseException) -> str:
    name = type(exc).__name__
    if name in {"ExecutionTimeout", "NetworkTimeout", "TimeoutError", "CancelledError"} \
            or getattr(exc, "code", None) == 50:
        return "timeout"
    if name in {"AutoReconnect", "ConnectionFailure", "ServerSelectionTimeoutError", "NotPrimaryError"}:
        return "transient"
    return "unsupported"


class _LocalCapabilityCache:
    def __init__(self, ttl_seconds: float = 300.0, clock=time.monotonic):
        self.ttl, self._clock, self._down = ttl_seconds, clock, {}

    def usable(self, name: str) -> bool:
        at = self._down.get(name)
        if at is None:
            return True
        if self._clock() - at >= self.ttl:
            self._down.pop(name, None)
            return True
        return False

    def record_failure(self, name: str, exc: BaseException) -> str:
        kind = _local_classify(exc)
        if kind == "unsupported":
            self._down[name] = self._clock()
        return kind

    def record_success(self, name: str) -> None:
        self._down.pop(name, None)

    def reset(self) -> None:
        self._down.clear()


if SHARED:
    FusionConfig = _shared.FusionConfig
    RerankConfig = _shared.RerankConfig
    build_rank_fusion = _shared.build_rank_fusion
    fusion_meta_stage = _shared.fusion_meta_stage
    app_rrf = _shared.app_rrf
    parse_score_details = _shared.parse_score_details
    build_rerank_stages = _shared.build_rerank_stages
    CapabilityCache = _shared.CapabilityCache
    classify_stage_error = _shared.classify_stage_error
else:  # pragma: no cover — exercitado por test_atlas_compat com o pov-shared escondido
    FusionConfig = LocalFusionConfig
    RerankConfig = LocalRerankConfig
    build_rank_fusion = _local_build_rank_fusion
    fusion_meta_stage = _local_fusion_meta_stage
    app_rrf = _local_app_rrf
    parse_score_details = _local_parse_score_details
    build_rerank_stages = _local_build_rerank_stages
    CapabilityCache = _LocalCapabilityCache
    classify_stage_error = _local_classify


def explain_dict(raw: Any) -> dict | None:
    """scoreDetails → {value, legs[...]} ou None ("detalhe indisponível")."""
    parsed = parse_score_details(raw)
    return parsed.as_dict() if parsed else None
