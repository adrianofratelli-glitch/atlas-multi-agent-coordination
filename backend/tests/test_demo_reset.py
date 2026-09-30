"""Reset da memória da demo: deixa o usuário no estado anterior e nunca toca em mais ninguém."""

from app.cascade import cascade_store_episode, cascade_store_turn
from app.config import Settings
from app.database import DataStore, utcnow
from app.demo_reset import reset_customer_memory
from app.memory import active_budget, active_facts

from tests.test_memory_extractor import AGENT, FakeLLM, budget, fact  # reaproveita o LLM simulado


async def extract(store, key, message, facts):
    from app.memory import extract_and_store
    return await extract_and_store(store, key, message, llm=FakeLLM({"facts": facts}), budget=budget(), agent_doc=AGENT)


async def seeded():
    store = DataStore(Settings(demo_mode=True))
    # estado "de fábrica" do usuário: fato legado migrado com teto de R$ 350
    await store.insert_one("customer_memory", {"_id": "legacy-1", "customer_key": "ana", "fact": "Cliente sensível a preço", "fact_norm": "s", "category": "preferencia", "active": True, "max_price_brl": 350.0, "migrated_from": "price_sensitive", "created_at": utcnow()})
    await store.insert_one("customer_memory", {"_id": "other-1", "customer_key": "bruno", "fact": "Cliente prefere e-mail", "fact_norm": "e", "category": "preferencia", "active": True, "created_at": utcnow()})
    return store


async def test_reset_removes_demo_writes_and_restores_what_they_superseded():
    store = await seeded()
    await extract(store, "ana", "meu limite é 500", [fact("Cliente tem limite de R$ 500", max_price=500)])
    assert await active_budget(store, "ana") == 500  # a demo substituiu o teto antigo
    counts = await reset_customer_memory(store, "ana")
    assert counts["facts_removed"] == 1 and counts["facts_restored"] == 1
    assert await active_budget(store, "ana") == 350.0  # estado de fábrica de volta
    assert await active_facts(store, "ana") == ["Cliente sensível a preço"]


async def test_reset_clears_short_term_episodes_and_customer_cache_but_not_global_or_others():
    store = await seeded()
    kw = dict(target="support_agent", area="varejo", session_id="s1", intent="suporte", message="como resetar", answer="a", timeline=[], active_agent="support_agent", cache_eligible=True)
    await cascade_store_turn(store, customer_key="ana", **kw)
    await cascade_store_turn(store, customer_key="bruno", **kw)
    await cascade_store_episode(store, customer_key="ana", intent="suporte", agent="support_agent")
    await store.insert_one("long_term_memory", {"customer_key": "ana", "text": "Pergunta: legado cru"})  # legado: fica
    await reset_customer_memory(store, "ana")
    assert not await store.find_many("short_term_memory", {"customer_key": "ana"})
    assert not await store.find_many("semantic_cache", {"customer_key": "ana"})
    assert not await store.find_many("long_term_memory", {"customer_key": "ana", "kind": "episode"})
    assert len(await store.find_many("long_term_memory", {"customer_key": "ana"})) == 1
    assert await store.find_many("short_term_memory", {"customer_key": "bruno"})
    assert await store.find_many("semantic_cache", {"scope": "global"})            # cache global aquecido intacto
    assert await active_facts(store, "bruno") == ["Cliente prefere e-mail"]


async def test_reset_is_idempotent_and_safe_on_a_clean_user():
    store = await seeded()
    assert (await reset_customer_memory(store, "diego"))["facts_removed"] == 0
    await reset_customer_memory(store, "ana")
    assert (await reset_customer_memory(store, "ana"))["facts_removed"] == 0
    assert await active_budget(store, "ana") == 350.0
