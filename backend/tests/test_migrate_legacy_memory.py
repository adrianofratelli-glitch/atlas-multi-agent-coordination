from app.config import Settings
from app.database import DataStore, utcnow
from app.memory import active_budget, active_facts
from migrate_legacy_memory import clear_legacy_price_cap, migrate


async def legacy_store():
    store = DataStore(Settings(demo_mode=True))
    for key, ftype, value in (("bruno", "price_sensitive", "Cliente sensível a preço"),
                              ("bruno", "product_complaint", "Cliente relatou defeito"),
                              ("ana", "price_sensitive", "Cliente sensível a preço")):
        await store.insert_one("customer_memory", {"customer_key": key, "fact_type": ftype, "value": value, "active": True, "created_at": utcnow()})
    await store.insert_one("customer_memory", {"customer_key": "carla", "fact": "novo", "fact_norm": "novo", "category": "preferencia", "active": True, "created_at": utcnow()})
    return store


async def test_dry_run_changes_nothing():
    store = await legacy_store()
    assert await migrate(store, apply=False) == 3
    assert await active_budget(store, "bruno") is None


async def test_apply_keeps_facts_without_imposing_a_budget_the_customer_never_stated():
    store = await legacy_store()
    assert await migrate(store, apply=True) == 3
    assert await active_budget(store, "bruno") is None
    assert await active_budget(store, "carla") is None
    docs = await store.find_many("customer_memory", {"customer_key": "bruno"})
    complaint = next(d for d in docs if d["fact_type"] == "product_complaint")
    assert complaint["fact"] == "Cliente relatou defeito" and "max_price_brl" not in complaint and complaint["value"]
    assert len(await active_facts(store, "bruno")) == 2


async def test_apply_is_idempotent():
    store = await legacy_store()
    await migrate(store, apply=True)
    assert await migrate(store, apply=True) == 0


async def test_clear_legacy_price_cap_undoes_the_earlier_migration_but_keeps_the_fact():
    store = await legacy_store()
    await migrate(store, apply=True)
    for d in await store.find_many("customer_memory", {"migrated_from": "price_sensitive"}):  # estado da migração antiga
        await store.update_one("customer_memory", {"_id": d["_id"]}, {"$set": {"max_price_brl": 350.0}})
    assert await active_budget(store, "bruno") == 350.0
    assert await clear_legacy_price_cap(store) == 2
    assert await active_budget(store, "bruno") is None and await active_budget(store, "ana") is None
    assert "Cliente sensível a preço" in await active_facts(store, "ana")
    assert await clear_legacy_price_cap(store) == 0


async def test_restore_demo_fixtures_puts_seed_balances_back():
    from restore_demo_fixtures import restore
    from seed import seed
    store = DataStore(Settings(demo_mode=True))
    await store.connect()
    await seed(store, create_indexes=False)
    await store.update_one("loyalty_accounts", {"customer_key": "carla"}, {"$set": {"points": 100}})
    assert await restore(store) == {"carla": 2600}
    assert (await store.find_one("loyalty_accounts", {"customer_key": "carla"}))["points"] == 2600
    assert await restore(store) == {}
