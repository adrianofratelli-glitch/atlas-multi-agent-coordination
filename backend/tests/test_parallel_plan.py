"""Passos de leitura independentes do plano rodam juntos; escrita nunca entra no paralelo."""

import asyncio

from app import orchestration_graph, planner
from tests.test_agentic_core import ANA, _conv, _world, msg, text, use

FLAGSHIP = ("meu Fone Pulse X do pedido PED-1001 chegou com defeito; quero um fone parecido mais barato e já quero "
            "trocar. Depois veja a entrega.")


def test_planner_converte_depends_on_em_agentes_e_o_primeiro_abre_sem_dependencia():
    class LLM:
        client = True

        async def complete(self, **_):
            return ('{"steps": [{"agent": "support_agent", "task": "diagnosticar", "depends_on": []},'
                    '{"agent": "product_agent", "task": "recomendar", "depends_on": []},'
                    '{"agent": "order_agent", "task": "trocar", "depends_on": [1]},'
                    '{"agent": "logistics_agent", "task": "entrega"}]}'), {}

    plan, _ = asyncio.run(planner.plan_turn(
        llm=LLM(), budget=None, orchestrator_doc={"agent_key": "orchestrator"}, message=FLAGSHIP,
        first_agent="support_agent", available=["support_agent", "product_agent", "order_agent", "logistics_agent"],
        labels={}, max_steps=5))
    by = {step["agent"]: step["after"] for step in plan}
    assert by["support_agent"] == [] and by["product_agent"] == []
    assert by["order_agent"] == ["product_agent"]
    # sem depends_on: depende de todos os anteriores (sequencial, o seguro)
    assert set(by["logistics_agent"]) == {"support_agent", "product_agent", "order_agent"}


async def test_leituras_independentes_rodam_juntas_e_a_escrita_espera():
    plan = [{"agent": "support_agent", "task": "diagnosticar o defeito", "depends_on": []},
            {"agent": "product_agent", "task": "recomendar um fone mais barato", "depends_on": []},
            {"agent": "logistics_agent", "task": "consultar a entrega", "depends_on": []},
            {"agent": "order_agent", "task": "efetivar a troca", "depends_on": [1]}]
    store, llm, service = await _world({
        "support_agent": [msg(use("search_knowledge_base", query="fone com defeito")), msg(text("Diagnóstico feito."))],
        "product_agent": [msg(use("search_catalog", query="fone mais barato")), msg(text("Recomendo o Wave Lite."))],
        "logistics_agent": [msg(use("get_shipment", order_id="PED-1001")), msg(text("Entrega consultada."))],
        "order_agent": [msg(use("request_order_status_change", order_id="PED-1001", status="troca_solicitada")),
                        msg(text("Troca registrada."))],
    }, plan=plan, consolidation="Resposta consolidada.")
    out = await orchestration_graph.run_turn(service, FLAGSHIP, ANA, _conv())
    fanout = [e for e in out.timeline if e.category == "fanout"]
    assert fanout and set(fanout[0].result["agents"]) == {"support_agent", "product_agent", "logistics_agent"}
    assert "order_agent" not in fanout[0].result["agents"]          # escrita nunca no paralelo
    assert out.agent_path.index("order_agent") > out.agent_path.index("product_agent")
    assert set(out.agent_path) == {"support_agent", "product_agent", "logistics_agent", "order_agent"}
    assert (await store.find_one("orders", {"order_id": "PED-1001"}))["status"] == "troca_solicitada"
    board = await store.find_one("turn_blackboard", {"turn_id": out.turn_id})
    assert {e["agent"] for e in board["entries"]} == set(out.agent_path)
    handoffs = await store.find_many("agent_handoffs", {"conversation_id": out.conversation_id}, limit=20)
    pairs = [(h["from_agent"], h["to_agent"]) for h in handoffs]
    assert len(pairs) == len(set(pairs))                             # nenhum handoff duplicado


async def test_sem_plano_ou_com_dependencia_a_cadeia_continua_sequencial():
    plan = [{"agent": "support_agent", "task": "diagnosticar", "depends_on": []},
            {"agent": "product_agent", "task": "recomendar", "depends_on": [0]},
            {"agent": "logistics_agent", "task": "entrega", "depends_on": [1]}]
    _, _, service = await _world({
        "support_agent": [msg(text("Diagnóstico."))],
        "product_agent": [msg(text("Recomendação."))],
        "logistics_agent": [msg(text("Entrega."))],
    }, plan=plan, consolidation="ok")
    out = await orchestration_graph.run_turn(service, FLAGSHIP, ANA, _conv())
    assert not [e for e in out.timeline if e.category == "fanout"]
    assert out.agent_path == ["support_agent", "product_agent", "logistics_agent"]
