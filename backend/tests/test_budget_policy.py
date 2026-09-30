"""Orçamento do cliente é campo estruturado: o SERVIDOR injeta o teto como pré-filtro do catálogo."""

from app.agents import build_product_pipeline, run_product_agent, search_products
from app.config import Settings
from app.database import DataStore, utcnow
from app.seed_data import PRODUCTS
from seed import PRODUCTS_VECTOR_INDEX_DEFINITION

CUSTOMER = {"customer_key": "ana", "area": "varejo"}


async def demo_store(budget: float | None = None) -> DataStore:
    store = DataStore(Settings(demo_mode=True))
    for product in PRODUCTS:
        await store.insert_one("products_catalog", dict(product))
    if budget is not None:
        await store.insert_one("customer_memory", {
            "customer_key": "ana", "fact": f"Cliente tem limite de R$ {budget:.0f}", "fact_norm": "x",
            "category": "preferencia", "active": True, "max_price_brl": budget, "created_at": utcnow()})
    return store


class FakeAtlas:
    """Store com Atlas: captura o pipeline do catálogo em vez de executá-lo."""

    memory = False

    def __init__(self, budget=None, fail=False):
        self.budget, self.fail, self.pipelines = budget, fail, []
        self.inner = None

    async def find_many(self, name, query=None, **kw):
        if name == "customer_memory":
            return [{"customer_key": "ana", "fact": "f", "active": True, "max_price_brl": self.budget,
                     "created_at": utcnow()}] if self.budget else []
        return await self.inner.find_many(name, query, **kw)

    def _collection(self, name):
        atlas = self

        class Coll:
            async def aggregate(self, pipeline):
                atlas.pipelines.append(pipeline)
                if atlas.fail:
                    raise RuntimeError("índice fora do ar")

                class Cursor:
                    async def to_list(self, _):
                        return []
                return Cursor()
        return Coll()


# ---- índice: o filtro só pode usar campos declarados como `filter` ----

def test_every_prefilter_field_is_declared_as_filter_in_the_index():
    declared = {f["path"] for f in PRODUCTS_VECTOR_INDEX_DEFINITION["fields"] if f["type"] == "filter"}
    assert "price" in declared  # conferido na definição do índice, não assumido
    stage = build_product_pipeline("fone", max_price=800, category="Áudio")[0]["$vectorSearch"]
    assert set(stage["filter"]) <= declared


# ---- caminho Atlas: teto no pré-filtro nativo ----

def test_ceiling_is_a_native_prefilter_inclusive_not_a_post_match():
    pipeline = build_product_pipeline("fone", max_price=800, category=None)
    assert pipeline[0]["$vectorSearch"]["filter"]["price"] == {"$lte": 800}
    assert not any("$match" in stage for stage in pipeline)


async def test_atlas_path_injects_memory_budget_the_message_never_mentions():
    atlas = FakeAtlas(budget=800)
    atlas.inner = await demo_store()
    await run_product_agent(atlas, "me recomenda um fone de ouvido", CUSTOMER)
    assert atlas.pipelines[0][0]["$vectorSearch"]["filter"]["price"] == {"$lte": 800}


async def test_atlas_path_without_budget_has_no_price_filter():
    atlas = FakeAtlas(budget=None)
    atlas.inner = await demo_store()
    await run_product_agent(atlas, "me recomenda um fone de ouvido", CUSTOMER)
    assert "price" not in atlas.pipelines[0][0]["$vectorSearch"]["filter"]


async def test_atlas_failure_falls_back_to_local_search_with_the_same_ceiling():
    atlas = FakeAtlas(budget=300, fail=True)
    atlas.inner = await demo_store()
    result = await run_product_agent(atlas, "me recomenda um fone de ouvido", CUSTOMER)
    assert result.event.result and all(item["price"] <= 300 for item in result.event.result)


# ---- DEMO_MODE (busca local, sem $vectorSearch) ----

async def test_demo_mode_never_returns_items_above_stored_budget():
    store = await demo_store(budget=300)
    result = await run_product_agent(store, "me recomenda um fone de ouvido", CUSTOMER)
    assert result.event.result and all(item["price"] <= 300 for item in result.event.result)
    assert result.event.filter["price"] == {"$lte": 300}
    assert "R$ 300.00" in result.response
    assert any(event.category == "memory" for event in result.extra_events)


async def test_budget_is_inclusive_of_the_exact_price():
    store = await demo_store()
    assert any(item["price"] == 349.90 for item in await search_products(store, "fone", 349.90, "Áudio"))


async def test_budget_is_a_hard_limit_no_silent_relaxation():
    """Nada cabe no orçamento: a resposta diz isso; nunca mostra item acima do teto sem o cliente pedir."""
    store = await demo_store(budget=50)
    result = await run_product_agent(store, "me recomenda um monitor", CUSTOMER)
    assert not result.event.result
    assert "orçamento" in result.response


async def test_no_category_fallback_also_respects_budget():
    store = await demo_store(budget=120)
    result = await run_product_agent(store, "o que vocês têm de bom para presentear alguém", CUSTOMER)
    assert all(item["price"] <= 120 for item in result.event.result)


async def test_explicit_ceiling_in_the_message_wins_over_stored_budget():
    store = await demo_store(budget=300)
    result = await run_product_agent(store, "quero um monitor até 1300 reais", CUSTOMER)
    assert result.event.filter["price"] == {"$lte": 1300.0}
    assert any(item["price"] > 300 for item in result.event.result)
    assert not any(event.category == "memory" for event in result.extra_events)


async def test_other_customers_budget_is_never_applied():
    store = await demo_store(budget=100)
    result = await run_product_agent(store, "me recomenda um monitor", {"customer_key": "bruno", "area": "varejo"})
    assert "price" not in result.event.filter


async def test_limit_stated_in_the_same_message_is_also_hard_when_it_matches_the_stored_budget():
    """"agora meu limite é R$ 300; me recomenda um monitor": o teto vem da mensagem E da memória (a extração roda
    antes do agente). Antes tratava como "teto explícito" e relaxava em silêncio, mostrando monitores de R$ 1.199."""
    store = await demo_store(budget=300)
    result = await run_product_agent(store, "agora meu limite é R$ 300; me recomenda um monitor", CUSTOMER)
    assert not result.event.result and "orçamento" in result.response


async def test_stricter_explicit_ceiling_than_stored_budget_is_hard_too():
    store = await demo_store(budget=1300)
    result = await run_product_agent(store, "quero um monitor até 300 reais", CUSTOMER)
    assert not result.event.result
