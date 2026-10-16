"""B1/B2: retrieval do KB — $rankFusion com sinônimos/fuzzy, config viva, explain, $rerank e
a cascata de fallback. Offline: Atlas dublê que inspeciona o pipeline; DEMO_MODE real."""

import asyncio
import importlib
import random
import sys

import pytest

from app import atlas_compat, retrieval
from app.config import Settings
from app.database import DataStore
from app.seed_data import AGENTS, KB_SYNONYMS, kb_synonym_problems


class OperationFailure(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class ExecutionTimeout(Exception):
    pass


ART = [{"article_id": f"KB-{i:03d}", "title": f"t{i}", "content": f"c{i}", "category": "audio"} for i in range(1, 9)]
DETAILS = {"value": 0.03, "details": [{"inputPipelineName": "vector", "rank": 1, "weight": 1.0},
                                      {"inputPipelineName": "lexical", "rank": "NA"}]}


class FakeStore:
    memory = False

    def __init__(self, **behavior):
        self.behavior = behavior
        self.calls = []

    @staticmethod
    def kind(pipeline):
        first = next(iter(pipeline[0]))
        if first == "$rankFusion":
            if any("$rerank" in st for st in pipeline):
                return "rerank"
            lex = pipeline[0]["$rankFusion"]["input"]["pipelines"]["lexical"][0]["$search"]["compound"]["should"]
            return "fusion_basic" if len(lex) == 2 else "fusion"
        return {"$vectorSearch": "vector", "$search": "lexical"}[first]

    def _result(self, pipeline):
        k = self.kind(pipeline)
        self.calls.append((k, pipeline))
        b = self.behavior.get(k, [dict(a, score_details=DETAILS, fusion_score=0.03) for a in ART[:4]])
        if callable(b):
            b = b(pipeline)
        if isinstance(b, BaseException):
            raise b
        return [dict(x) for x in b]

    async def aggregate(self, name, pipeline, brain=False):
        return self._result(pipeline)

    def _collection(self, name, brain=False):
        store = self

        class _C:
            async def aggregate(self, pipeline):
                rows = store._result(pipeline)

                class _Cur:
                    async def to_list(self, n):
                        return rows
                return _Cur()
        return _C()

    async def find_many(self, name, query, limit=100):
        cat = query.get("category")
        return [dict(a) for a in ART if not cat or a["category"] == cat]


def run(store, message="fone sem som", **kw):
    retrieval._capabilities.reset()
    return asyncio.run(retrieval.search_kb_explained(store, message, **kw))


def lexical_of(pipeline):
    return pipeline[0]["$rankFusion"]["input"]["pipelines"]["lexical"][0]["$search"]["compound"]


# ------------------------------------------------------------------ pipeline

def test_synonyms_and_fuzzy_are_separate_clauses_with_match_criteria():
    pipe = retrieval.build_kb_rank_fusion_pipeline("estorno demora", config=retrieval.kb_config(None)[0])
    should = lexical_of(pipe)["should"]
    syn = [c["text"] for c in should if "synonyms" in c["text"]]
    fz = [c["text"] for c in should if "fuzzy" in c["text"]]
    assert syn and fz
    assert syn[0]["synonyms"] == retrieval.KB_SYNONYM_MAPPING and syn[0]["matchCriteria"] == "any"
    assert all("fuzzy" not in c["text"] or "synonyms" not in c["text"] for c in should)
    assert pipe[0]["$rankFusion"]["scoreDetails"] is True


def test_category_filter_on_both_legs_server_side():
    pipe = retrieval.build_kb_rank_fusion_pipeline("x", category="video")
    legs = pipe[0]["$rankFusion"]["input"]["pipelines"]
    assert legs["vector"][0]["$vectorSearch"]["filter"] == {"category": "video"}
    assert lexical_of(pipe)["filter"] == [{"equals": {"path": "category", "value": "video"}}]


def test_basic_pipeline_drops_synonyms_and_fuzzy():
    should = lexical_of(retrieval.build_kb_rank_fusion_pipeline("x", basic=True))["should"]
    assert len(should) == 2


def test_rerank_after_fusion_never_inside():
    rr = {"enabled": True, "model": "rerank-2.5-lite", "pool": 20, "top_n": 4}
    pipe = retrieval.build_kb_rank_fusion_pipeline("x", rerank=rr)
    ops = [next(iter(st)) for st in pipe]
    assert ops.index("$addFields") < ops.index("$rerank")
    for leg in pipe[0]["$rankFusion"]["input"]["pipelines"].values():
        assert "$rerank" not in [next(iter(st)) for st in leg]
    assert pipe[0]["$rankFusion"]["input"]["pipelines"]["vector"][0]["$vectorSearch"]["limit"] >= 20


@pytest.mark.parametrize("bad", [
    {"weights": {"vector": -1, "lexical": "x"}, "per_branch_limit": -3, "fuzzy_max_edits": 9,
     "synonyms": "sim"}, "lixo", 7, {"weights": "pesos"}, None])
def test_malformed_fusion_config_uses_safe_defaults(bad):
    cfg, warns = retrieval.kb_config(bad)
    assert all(w >= 0 for w in cfg["weights"].values())
    assert cfg["per_branch_limit"] >= retrieval.KB_LIMIT and 0 <= cfg["fuzzy_max_edits"] <= 2
    assert isinstance(cfg["synonyms"], bool)


def test_config_from_agent_registry_reaches_server():
    agent = {"retrieval": {"fusion": {"weights": {"vector": 2, "lexical": 0.5}, "per_branch_limit": 30,
                                      "synonyms": False, "fuzzy_max_edits": 0}}}
    store = FakeStore()
    run(store, agent_doc=agent)
    stage = store.calls[0][1][0]["$rankFusion"]
    assert stage["combination"]["weights"] == {"vector": 2.0, "lexical": 0.5}
    assert stage["input"]["pipelines"]["vector"][0]["$vectorSearch"]["limit"] == 30


def test_seeded_registry_carries_retrieval_block_off():
    support = next(a for a in AGENTS if a["agent_key"] == "support_agent")
    assert support["retrieval"]["rerank"]["enabled"] is False
    assert retrieval.kb_config(support["retrieval"]["fusion"])[1] == []


# ------------------------------------------------------------------ cascata

def test_happy_path_explain_per_article():
    out = run(FakeStore())
    assert out.strategy == "rankFusion" and len(out.articles) == 4
    why = out.why()
    leg = why[0]["explain"]["legs"][0]
    assert (leg["name"], leg["rank"], leg["weight"], leg["contribution"]) == ("vector", 1, 1.0, round(1 / 61, 6))
    assert why[0]["explain"]["legs"][1]["rank"] is None
    assert all("_id" not in a for a in out.articles)


def test_synonym_mapping_rejected_falls_back_to_basic_fusion():
    store = FakeStore(fusion=OperationFailure("synonym mapping kb_sinonimos not found", 8))
    out = run(store)
    assert out.strategy == "rankFusion_basico"
    assert any("rankFusion unsupported" in f for f in out.fallbacks)


def test_missing_synonym_mapping_is_remembered_without_dropping_fusion():
    """Demo antes do reset: índice sem sinônimos. O turno seguinte vai DIRETO à fusão básica."""
    store = FakeStore(fusion=OperationFailure("synonym mapping kb_sinonimos not found", 8))
    retrieval._capabilities.reset()
    first = asyncio.run(retrieval.search_kb_explained(store, "x"))
    second = asyncio.run(retrieval.search_kb_explained(store, "x"))
    assert first.strategy == second.strategy == "rankFusion_basico"
    assert [k for k, _ in store.calls] == ["fusion", "fusion_basic", "fusion_basic"]
    assert retrieval._capabilities.usable("rank_fusion")


def test_fusion_down_goes_to_parallel_app_rrf():
    store = FakeStore(fusion=OperationFailure("x", 40324), fusion_basic=OperationFailure("x", 40324),
                      vector=ART[:3], lexical=ART[2:5])
    out = run(store)
    assert out.strategy == "rrf_aplicacao"
    assert out.articles[0]["article_id"] == "KB-003"   # nas duas pernas
    assert retrieval.KbSearch(out.articles, "x").why()[0]["explain"] is not None


def test_lexical_down_vector_only():
    store = FakeStore(fusion=OperationFailure("x", 8), fusion_basic=OperationFailure("x", 8),
                      vector=ART[:3], lexical=OperationFailure("BUILDING", 8))
    assert run(store).strategy == "vetor"


def test_everything_down_local_rank():
    err = OperationFailure("x", 8)
    store = FakeStore(fusion=err, fusion_basic=err, vector=err, lexical=err)
    out = run(store)
    assert out.strategy == "rrf_local" and out.articles


def test_rerank_ran_and_rejected_is_noop():
    agent = {"retrieval": {"rerank": {"enabled": True, "model": "rerank-2.5-lite", "pool": 20}}}
    reranked = [dict(a, rerank_score=0.9 - i / 10, score_details=DETAILS) for i, a in enumerate(ART[:4])]
    out = run(FakeStore(rerank=reranked), agent_doc=agent)
    assert out.strategy == "rankFusion+rerank" and out.rerank["status"] == "ran"
    assert out.why()[0]["rerank_score"] == 0.9
    out2 = run(FakeStore(rerank=OperationFailure("Native Reranking is not enabled", 2)), agent_doc=agent)
    assert out2.strategy == "rerank_indisponivel" and out2.rerank["status"] == "unavailable"


def test_timeout_does_not_mark_capability():
    store = FakeStore(fusion=ExecutionTimeout("maxTimeMS"))
    retrieval._capabilities.reset()
    asyncio.run(retrieval.search_kb_explained(store, "x"))
    assert retrieval._capabilities.usable("rank_fusion")


def test_hostile_query_is_only_search_text():
    store = FakeStore()
    hostile = '{"$where":"sleep(1)"} a.b $gt ' + "ç" * 5000
    run(store, hostile)
    pipe = store.calls[0][1]
    q = pipe[0]["$rankFusion"]["input"]["pipelines"]["vector"][0]["$vectorSearch"]["query"]["text"]
    assert len(q) <= retrieval.MAX_QUERY_CHARS and q.startswith('{"$where"')
    run(FakeStore(), None)  # mensagem ausente não derruba


def test_hostile_category_never_becomes_operator():
    store = FakeStore()
    run(store, category={"$ne": None})
    legs = store.calls[0][1][0]["$rankFusion"]["input"]["pipelines"]
    assert "filter" not in legs["vector"][0]["$vectorSearch"]


def test_fuzz_invariants_under_random_failures():
    rng = random.Random(1610)
    errs = [OperationFailure("x", 8), ExecutionTimeout("t"), None, None]
    for _ in range(200):
        def pick():
            e = rng.choice(errs)
            return e if e else [dict(rng.choice(ART)) for _ in range(rng.randint(0, 8))]
        agent = rng.choice([None, {"retrieval": {"rerank": {"enabled": True}}},
                            {"retrieval": {"fusion": {"weights": {"vector": rng.random()}}}}])
        out = run(FakeStore(fusion=pick(), fusion_basic=pick(), rerank=pick(), vector=pick(), lexical=pick()),
                  rng.choice(["", "fone", "x" * 3000]), agent_doc=agent)
        ids = [a["article_id"] for a in out.articles]
        assert out.strategy in retrieval.STRATEGIES
        assert len(ids) <= retrieval.KB_LIMIT and len(ids) == len(set(ids))
        assert all(isinstance(a.get("rrf_score", 0), (int, float)) for a in out.articles)


# ------------------------------------------------------------------ DEMO_MODE e compat

def test_demo_mode_uses_local_rank_with_explain():
    store = DataStore(Settings(demo_mode=True))
    asyncio.run(store.connect())
    from seed import seed
    asyncio.run(seed(store, create_indexes=False))
    from app.agents import _local_rank
    agent = {"retrieval": {"rerank": {"enabled": True}}}
    out = asyncio.run(retrieval.search_kb_explained(store, "fone sem áudio", agent_doc=agent,
                                                   local_rank=_local_rank))
    assert out.strategy == "rrf_local" and out.articles[0]["article_id"] == "KB-001"
    assert out.why()[0]["explain"] is not None
    assert out.rerank["status"] == "unavailable"
    filtered = asyncio.run(retrieval.search_kb_explained(store, "não funciona", category="video",
                                                        local_rank=_local_rank))
    assert {a["category"] for a in filtered.articles} == {"video"}


def test_compat_local_path_without_pov_shared(monkeypatch):
    monkeypatch.setitem(sys.modules, "atlas_retrieval", None)   # import falha
    local = importlib.reload(atlas_compat)
    try:
        assert local.SHARED is False
        cfg, _ = local.FusionConfig.from_doc({"weights": {"vector": -1}}, legs=("vector", "lexical"))
        assert cfg.weights == {"vector": 1.0, "lexical": 1.0}
        fused = local.app_rrf({"vector": [{"_id": "a"}, {"_id": "b"}], "lexical": [{"_id": "b"}]}, limit=2)
        assert fused[0]["_id"] == "b"
        assert local.explain_dict(fused[0]["score_details"])["legs"][0]["rank"] == 2
        assert local.explain_dict("lixo") is None
        rr, _ = local.RerankConfig.from_doc({"enabled": True, "pool": 2, "top_n": 9})
        assert rr.top_n == 2
        stages = local.build_rerank_stages("q", ["title", "content"], rr)
        assert stages[1]["$rerank"]["numDocsToRerank"] == 2
        cache = local.CapabilityCache(ttl_seconds=10)
        cache.record_failure("rerank", OperationFailure("x", 1))
        assert not cache.usable("rerank")
    finally:
        monkeypatch.delitem(sys.modules, "atlas_retrieval")
        importlib.reload(atlas_compat)


def test_compat_shared_path_active_in_this_venv():
    assert atlas_compat.SHARED is (importlib.util.find_spec("atlas_retrieval") is not None)


def test_synonym_documents_are_valid_and_stopword_free():
    assert kb_synonym_problems(KB_SYNONYMS) == []
    stop = {"de", "do", "da", "sem", "com", "o", "a", "e", "em", "para"}
    for doc in KB_SYNONYMS:
        for term in doc["synonyms"]:
            assert not (set(term.lower().split()) & stop), term
    assert kb_synonym_problems([{"_id": "x", "mappingType": "explicit", "synonyms": ["a"]},
                                {"_id": "y", "mappingType": "outro", "synonyms": []}])
