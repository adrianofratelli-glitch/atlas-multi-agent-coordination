from typing import TYPE_CHECKING

from app.agents import wants_replacement
from app.graph import (build_order_chain_pipeline, summarize_order_chain,
                       traverse_order_chain_in_memory)

if TYPE_CHECKING:
    from app.database import DataStore

ORDERS = [
    {"order_id": "PED-3001", "owner_customer_key": "carla", "product": "Smartwatch Fit", "replacement_order_id": "PED-3011"},
    {"order_id": "PED-3011", "owner_customer_key": "carla", "product": "Smartwatch Fit", "replacement_order_id": "PED-3021"},
    {"order_id": "PED-3021", "owner_customer_key": "carla", "product": "Smartwatch Fit", "replacement_order_id": "PED-3031"},
    {"order_id": "PED-3031", "owner_customer_key": "carla", "product": "Smartwatch Fit"},
    {"order_id": "PED-1002", "owner_customer_key": "ana", "product": "Teclado Air", "replacement_order_id": "PED-1012"},
    {"order_id": "PED-1012", "owner_customer_key": "ana", "product": "Teclado Air"},
]


def test_pipeline_filtra_dono_no_match_e_em_cada_salto():
    pipeline = build_order_chain_pipeline("PED-3001", "carla")
    assert pipeline[0]["$match"]["owner_customer_key"] == "carla"
    lookup = pipeline[1]["$graphLookup"]
    assert lookup["connectFromField"] == "replacement_order_id"
    assert lookup["connectToField"] == "order_id"
    # Sem restrictSearchWithMatch um salto poderia alcançar pedido de outro cliente.
    assert lookup["restrictSearchWithMatch"] == {"owner_customer_key": "carla"}


def test_cadeia_longa_do_mesmo_produto_marca_defeito_recorrente():
    chain = summarize_order_chain(traverse_order_chain_in_memory(ORDERS, "PED-3001", "carla"))
    assert chain["replacements"] == 3
    assert chain["path"] == ["PED-3001", "PED-3011", "PED-3021", "PED-3031"]
    assert chain["same_product_count"] == 4
    assert chain["recurring_defect"] is True
    assert chain["needs_quality_review"] is True


def test_troca_unica_nao_dispara_revisao_de_qualidade():
    chain = summarize_order_chain(traverse_order_chain_in_memory(ORDERS, "PED-1002", "ana"))
    assert chain["replacements"] == 1
    assert chain["recurring_defect"] is False


def test_travessia_nao_atravessa_fronteira_de_cliente():
    # Pedido da Carla consultado como se fosse da Ana: nada é alcançado.
    assert traverse_order_chain_in_memory(ORDERS, "PED-3001", "ana") is None


def test_pedido_sem_troca_tem_cadeia_vazia():
    chain = summarize_order_chain(traverse_order_chain_in_memory(ORDERS, "PED-3031", "carla"))
    assert chain["replacements"] == 0
    assert chain["path"] == ["PED-3031"]
    assert chain["recurring_defect"] is False


def test_pedido_inexistente_devolve_resumo_vazio_sem_none_no_caminho():
    chain = summarize_order_chain(None)
    assert chain["path"] == []
    assert chain["replacements"] == 0
    assert chain["needs_quality_review"] is False


def test_consulta_passiva_de_garantia_nao_abre_revisao_humana():
    # "ainda tem garantia?" é leitura: escalar aí criaria trabalho de analista a partir de
    # uma pergunta que não pediu nada. A cadeia continua sendo consultada e informada.
    assert wants_replacement("o Smartwatch Fit do pedido PED-3001 ainda está na garantia?") is False


def test_relato_de_nova_falha_ou_pedido_de_troca_abre_revisao():
    for message in (
        "o relógio quebrou de novo, quero trocar",
        "o produto apresentou defeito novamente",
        "quero trocar mais uma vez",
        "não funciona, quero reembolso",
    ):
        assert wants_replacement(message) is True, message


async def test_travessia_indisponivel_degrada_em_vez_de_quebrar_o_turno():
    # Se o $graphLookup falhar no Atlas (índice removido, versão antiga, rede), o turno não
    # pode morrer: a consulta cai para a travessia local sobre os mesmos documentos.
    from app.agents import order_replacement_chain
    from app.config import Settings
    from app.database import DataStore

    store = DataStore(Settings(demo_mode=False))
    store.memory = False

    async def aggregate_quebrado(*args, **kwargs):
        raise RuntimeError("$graphLookup indisponível")

    async def find_many(name, query=None, **kwargs):
        return [dict(item) for item in ORDERS if item["owner_customer_key"] == "carla"]

    store.aggregate = aggregate_quebrado
    store.find_many = find_many

    chain = await order_replacement_chain(store, "PED-3001", "carla")
    assert chain["replacements"] == 3
    assert chain["needs_quality_review"] is True


async def _store_com_pedidos() -> "DataStore":
    from app.config import Settings
    from app.database import DataStore
    store = DataStore(Settings(demo_mode=True))
    for order in ORDERS:
        await store.insert_one("orders", {**order, "product": order["product"], "status": "entregue",
                                          "timeline": [{"status": "criado", "at": "2026-05-02"}]})
    return store


async def test_order_agent_bloqueia_a_troca_quando_a_cadeia_e_recorrente():
    # "quero trocar o pedido PED-0000" nunca passa pelo warranty_agent. Se o gate morasse só
    # lá, a quarta reposição do mesmo item defeituoso seria efetivada pelo agente que
    # processa a troca. O gate tem que estar onde a escrita acontece.
    from app.agents import run_order_agent
    store = await _store_com_pedidos()

    result = await run_order_agent(store, "quero trocar o pedido PED-3001",
                                   {"customer_key": "carla", "area": "financeiro"},
                                   context={"conversation_id": "conv-1"})

    assert result.event.op == "graphLookup"
    assert "não vou processar mais uma troca automática" in result.response
    pedido = await store.find_one("orders", {"order_id": "PED-3001"})
    assert pedido["status"] == "entregue", "o status não pode ter sido alterado"
    assert len(await store.find_many("pending_reviews", {"status": "pending"})) == 1


async def test_order_agent_troca_normalmente_quando_nao_ha_defeito_recorrente():
    from app.agents import run_order_agent
    store = await _store_com_pedidos()

    result = await run_order_agent(store, "quero trocar o pedido PED-1002",
                                   {"customer_key": "ana", "area": "varejo"},
                                   context={"conversation_id": "conv-1"})

    pedido = await store.find_one("orders", {"order_id": "PED-1002"})
    assert pedido["status"] == "troca_solicitada"
    assert await store.find_many("pending_reviews", {}) == []
    assert result.event.op == "write"
