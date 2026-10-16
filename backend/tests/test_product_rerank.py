"""B2: $rerank no catálogo do product_agent — pipeline, no-op na recusa, DEMO_MODE e seed."""

import asyncio

from app import agents, retrieval
from app.config import Settings
from app.database import DataStore
from app.seed_data import AGENTS

RR = {"enabled": True, "model": "rerank-2.5-lite", "pool": 8, "top_n": 4}


def test_rerank_keeps_server_filters_and_business_weighting():
    pipe = agents.build_product_rerank_pipeline("fone", 300.0, "Áudio", RR)
    ops = [next(iter(st)) for st in pipe]
    assert ops[0] == "$vectorSearch" and ops.index("$rerank") == 2
    assert pipe[0]["$vectorSearch"]["filter"] == {"active": True, "price": {"$lte": 300.0}, "category": "Áudio"}
    assert {"$addFields": {"relevance": "$rerank_score"}} in pipe
    assert any("weighted_score" in st.get("$addFields", {}) for st in pipe)
    assert pipe[-2] == {"$limit": 4}


class _Coll:
    def __init__(self, fail_rerank):
        self.fail_rerank, self.calls = fail_rerank, []

    async def aggregate(self, pipeline):
        has_rr = any("$rerank" in st for st in pipeline)
        self.calls.append(has_rr)
        if has_rr and self.fail_rerank:
            raise RuntimeError("Native Reranking is not enabled")
        rows = [{"sku": "SKU-007"}]

        class _C:
            async def to_list(self, n):
                return rows
        return _C()


class _Store:
    memory = False

    def __init__(self, fail):
        self.coll = _Coll(fail)

    def _collection(self, name):
        return self.coll


def run(store, agent_doc):
    retrieval._capabilities.reset()
    return asyncio.run(agents.search_products_ranked(store, "teclado mecânico", agent_doc=agent_doc))


def test_rerank_ran():
    items, info = run(_Store(False), {"retrieval": {"rerank": RR}})
    assert info["status"] == "ran" and items[0]["sku"] == "SKU-007"


def test_rerank_rejected_is_noop_and_remembered():
    store = _Store(True)
    items, info = run(store, {"retrieval": {"rerank": RR}})
    assert info["status"] == "unavailable" and items
    asyncio.run(agents.search_products_ranked(store, "x", agent_doc={"retrieval": {"rerank": RR}}))
    assert store.coll.calls == [True, False, False]   # segunda vez nem tenta o $rerank


def test_disabled_or_malformed_config_never_reranks():
    for doc in (None, {}, {"retrieval": {"rerank": {"enabled": "sim"}}}, {"retrieval": "x"}):
        store = _Store(False)
        _, info = run(store, doc)
        assert info["status"] == "off" and store.coll.calls == [False]


def test_demo_mode_reports_unavailable():
    store = DataStore(Settings(demo_mode=True))
    asyncio.run(store.connect())
    from seed import seed
    asyncio.run(seed(store, create_indexes=False))
    items, info = asyncio.run(agents.search_products_ranked(store, "teclado", agent_doc={"retrieval": {"rerank": RR}}))
    assert items and info["status"] == "unavailable"


def test_seeded_product_agent_has_rerank_on_and_valid():
    product = next(a for a in AGENTS if a["agent_key"] == "product_agent")
    cfg, warns = retrieval.rerank_config(product)
    assert cfg["enabled"] is True and warns == [] and not cfg["model"].startswith("rerank-3")
