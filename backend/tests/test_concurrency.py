import asyncio

from app.agents import run_loyalty_agent
from app.config import Settings
from app.database import DataStore
from app.llm import LLMGateway
from app.orchestration import OrchestrationService


CUSTOMER = {"customer_key": "carla", "area": "varejo", "name": "Carla", "plan": "essencial"}


async def test_two_concurrent_turns_on_the_same_conversation_do_not_lose_a_turn():
    """Regressão do lost update: dois turnos concorrentes na MESMA conversation_id (double-click,
    retry de rede sobrepondo a request original em voo) não podem fazer um `replace_one` do
    documento inteiro apagar a mensagem do outro. `_update_conversation` agora aplica só o delta
    via `update_one`/`$push`, então os dois turnos devem sobreviver, em qualquer ordem."""
    settings = Settings(demo_mode=True)
    store = DataStore(settings)
    service = OrchestrationService(store, LLMGateway(settings), global_budget=6000)

    await asyncio.gather(
        service._update_conversation("conv-race", {"customer_key": "carla"}, "primeira mensagem", "primeira resposta", "order_agent", []),
        service._update_conversation("conv-race", {"customer_key": "carla"}, "segunda mensagem", "segunda resposta", "order_agent", []),
    )

    conversation = await store.find_one("agent_conversations", {"conversation_id": "conv-race", "customer_key": "carla"})
    contents = {turn["content"] for turn in conversation["turns"]}
    assert {"primeira mensagem", "segunda mensagem", "primeira resposta", "segunda resposta"} <= contents
    assert len(conversation["turns"]) == 4  # nenhum dos dois turnos concorrentes foi perdido


async def test_update_conversation_preserves_active_order_id_when_turn_does_not_touch_it():
    """$set só deve mencionar active_order_id/active_invoice_id quando o turno de fato produziu
    um valor novo — um turno que não toca pedido/fatura não pode apagar o que já estava lá."""
    settings = Settings(demo_mode=True)
    store = DataStore(settings)
    service = OrchestrationService(store, LLMGateway(settings), global_budget=6000)

    from app.models import TimelineEvent
    with_order = [TimelineEvent(category="agent", title="t", agent="order_agent", collection="orders", op="read", result={"order_id": "PED-9001"})]
    await service._update_conversation("conv-order", {"customer_key": "carla"}, "quero ver meu pedido", "resposta", "order_agent", [], with_order)

    conversation = await store.find_one("agent_conversations", {"conversation_id": "conv-order", "customer_key": "carla"})
    assert conversation["active_order_id"] == "PED-9001"

    await service._update_conversation("conv-order", {"customer_key": "carla"}, "oi", "resposta", "support_agent", [], [])
    conversation = await store.find_one("agent_conversations", {"conversation_id": "conv-order", "customer_key": "carla"})
    assert conversation["active_order_id"] == "PED-9001"  # preservado, não apagado pelo turno seguinte


async def test_loyalty_redemption_is_idempotent_under_concurrent_retry():
    """Regressão da falta de idempotência: dois resgates idênticos disparados quase juntos (retry
    de rede reprocessando a mesma intenção) só devem debitar os pontos uma vez."""
    settings = Settings(demo_mode=True)
    store = DataStore(settings)
    await store.insert_one("loyalty_accounts", {
        "customer_key": "carla", "points": 1000, "tier": "prata", "tier_benefits": [],
    })

    message = "quero resgatar frete gratis"
    results = await asyncio.gather(
        run_loyalty_agent(store, message, CUSTOMER),
        run_loyalty_agent(store, message, CUSTOMER),
    )

    account = await store.find_one("loyalty_accounts", {"customer_key": "carla"})
    assert account["points"] == 700  # 1000 - 300, uma única vez

    redemptions = await store.find_many("redemptions", {"customer_key": "carla"})
    assert len(redemptions) == 1

    # Um dos dois resultados confirma o resgate real; o outro reconhece a repetição sem debitar de novo.
    assert any("Resgate confirmado" in result.response for result in results)


async def test_loyalty_redemption_retry_returns_previous_result_without_double_debit():
    """Um segundo resgate idêntico, chegando DEPOIS do primeiro já ter terminado (não concorrente),
    também precisa ser reconhecido como repetição — não só o caso de corrida simultânea."""
    settings = Settings(demo_mode=True)
    store = DataStore(settings)
    await store.insert_one("loyalty_accounts", {
        "customer_key": "carla", "points": 1000, "tier": "prata", "tier_benefits": [],
    })

    message = "quero resgatar frete gratis"
    first = await run_loyalty_agent(store, message, CUSTOMER)
    second = await run_loyalty_agent(store, message, CUSTOMER)

    assert "Resgate confirmado" in first.response
    assert "não debitei de novo" in second.response or "idêntico" in second.response

    account = await store.find_one("loyalty_accounts", {"customer_key": "carla"})
    assert account["points"] == 700
    redemptions = await store.find_many("redemptions", {"customer_key": "carla"})
    assert len(redemptions) == 1
