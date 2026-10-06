"""Duplo clique REAL (Atlas, banco isolado `*_test`): duas requisições idênticas em voo ao mesmo tempo.

O DataStore em memória não intercala corrotinas nem emula WriteConflict, então só o driver real
prova que a escrita de dinheiro (débito de pontos) e o chamado humano acontecem uma vez só.
Roda só com LIVE=1; recusa o banco da demo (scripts/isolation.py).
"""

import asyncio
import os

import pytest

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("LIVE") != "1", reason="LIVE=1 para rodar contra o Atlas")]

CARLA = {"customer_key": "carla", "area": "varejo", "name": "Carla", "plan": "essencial"}
BRUNO = {"customer_key": "bruno", "area": "varejo", "name": "Bruno", "plan": "essencial"}


@pytest.fixture
async def test_store():
    from scripts.isolation import guard, test_settings
    from app.database import DataStore
    settings = guard(test_settings(), what="teste adversarial de duplo clique")
    assert settings.mongodb_db.endswith("_test")
    store = DataStore(settings)
    await store.connect()
    yield store
    await store.close()


async def test_double_click_redemption_debits_once(test_store):
    from app.agents import run_loyalty_agent
    store = test_store
    await store.delete_many("redemptions", {"customer_key": "carla"})
    await store.update_one("loyalty_accounts", {"customer_key": "carla"}, {"$set": {"points": 1000}}, upsert=True)
    message = "quero resgatar frete gratis"
    results = await asyncio.gather(*(run_loyalty_agent(store, message, CARLA) for _ in range(3)))
    account = await store.find_one("loyalty_accounts", {"customer_key": "carla"})
    redemptions = await store.find_many("redemptions", {"customer_key": "carla"})
    assert len(redemptions) == 1, [r.response for r in results]
    assert account["points"] == 1000 - redemptions[0]["points_spent"]


async def test_double_click_escalation_opens_one_ticket(test_store):
    from app.agents import run_support_agent
    store = test_store
    await store.delete_many("support_tickets", {"customer_key": "bruno"})
    context = {"conversation_id": "conv-dblclick-test"}
    message = "meu fone não liga, quero abrir chamado com um atendente"
    await asyncio.gather(*(run_support_agent(store, message, BRUNO, context=context) for _ in range(3)))
    tickets = await store.find_many("support_tickets", {"customer_key": "bruno"})
    assert len(tickets) == 1, [t["ticket_id"] for t in tickets]
