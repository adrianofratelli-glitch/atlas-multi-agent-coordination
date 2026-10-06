"""Reset único da demo e guarda do banco da demo (seed.py / scripts/reset_demo.py)."""

import pytest

from app.config import Settings
from app.database import DataStore
from scripts.reset_demo import reset
from seed import refuse_demo_database, seed


def _atlas(db="multi_agent_poc", brain="multiagent_brain"):
    return Settings(_env_file=None, mongodb_uri="mongodb+srv://placeholder.example.mongodb.net", mongodb_db=db,
                    mongodb_brain_db=brain)


def test_seed_refuses_the_demo_database_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("ALLOW_DEMO_DB_WRITE", raising=False)
    with pytest.raises(SystemExit, match="RECUSADO"):
        refuse_demo_database(_atlas())
    with pytest.raises(SystemExit):  # só um dos dois bancos de teste não basta
        refuse_demo_database(_atlas(db="multi_agent_poc_test"))


def test_seed_accepts_test_databases_memory_mode_and_the_escape_hatch(monkeypatch):
    monkeypatch.delenv("ALLOW_DEMO_DB_WRITE", raising=False)
    refuse_demo_database(_atlas("multi_agent_poc_test", "multiagent_brain_test"))
    refuse_demo_database(Settings(_env_file=None, demo_mode=True))
    monkeypatch.setenv("ALLOW_DEMO_DB_WRITE", "1")
    refuse_demo_database(_atlas())


async def test_seed_keeps_the_measured_block_threshold():
    store = DataStore(Settings(_env_file=None, demo_mode=True))
    await store.connect()
    await seed(store, create_indexes=False)
    await store.update_one("guardrail_policies", {"area": "default"}, {"$set": {"vector_block_threshold": 0.8814}}, brain=True)
    await seed(store, create_indexes=False)
    policy = await store.find_one("guardrail_policies", {"area": "default"}, brain=True)
    assert policy["vector_block_threshold"] == 0.8814


async def test_reset_removes_rehearsal_writes_and_keeps_seed_data():
    store = DataStore(Settings(_env_file=None, demo_mode=True))
    await store.connect()
    await seed(store, create_indexes=False)
    seeded = len(await store.find_many("guardrail_denylist", {}, limit=1000))
    await store.insert_one("support_tickets", {"ticket_id": "TCK-X", "customer_key": "bruno"})
    await store.insert_one("redemptions", {"redemption_id": "RDM-X", "customer_key": "carla"})
    await store.insert_one("guardrail_denylist", {"phrase": "aprendida", "phrase_norm": "aprendida", "source": "semantic_llm", "active": True})
    await store.insert_one("customer_memory", {"customer_key": "ana", "source": "extractor", "fact": "x", "active": True})
    first = await reset(store, wait_indexes=False)
    again = await reset(store, wait_indexes=False)  # idempotente
    assert first and again
    assert not await store.find_many("support_tickets", {})
    assert not await store.find_many("redemptions", {})
    assert not await store.find_many("customer_memory", {"source": "extractor"})
    assert len(await store.find_many("guardrail_denylist", {}, limit=1000)) == seeded
    assert len(await store.find_many("customers", {})) == 4
