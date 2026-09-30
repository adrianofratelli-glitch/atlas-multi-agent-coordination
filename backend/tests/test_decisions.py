import pytest

from app.config import Settings
from app.database import DataStore
from app.decisions import (AUDIT_COLLECTION, DECISIONS_COLLECTION, build_decision_doc,
                           decision_trail, record_decision)


def _decision(**overrides) -> dict:
    base = dict(action="order_status_change", subject_id="PED-1001", customer_key="ana",
                agent="order_agent", conversation_id="conv-1",
                reasoning="Cliente pediu a troca explicitamente.")
    return build_decision_doc(**(base | overrides))


async def test_decisao_grava_par_decisao_mais_evento_de_auditoria():
    store = DataStore(Settings(demo_mode=True))
    decision = await record_decision(store, _decision())

    decisions = await store.find_many(DECISIONS_COLLECTION, {})
    events = await store.find_many(AUDIT_COLLECTION, {})
    assert len(decisions) == 1 and len(events) == 1
    # O evento aponta para a decisão: trilha sem a decisão que a originou é o buraco a evitar.
    assert events[0]["decision_id"] == decision["decision_id"]
    assert decision["decision_id"].startswith("DEC-")
    assert decision["decided_by"] == "agent"


async def test_trilha_e_filtrada_por_dono_e_por_pedido():
    store = DataStore(Settings(demo_mode=True))
    await record_decision(store, _decision())
    await record_decision(store, _decision(subject_id="PED-1002"))
    await record_decision(store, _decision(customer_key="bruno", subject_id="PED-2001"))

    trilha_ana = await decision_trail(store, "ana")
    assert len(trilha_ana["decisions"]) == 2
    assert {item["subject_id"] for item in trilha_ana["decisions"]} == {"PED-1001", "PED-1002"}

    # Nem informando o subject_id certo o Bruno alcança a decisão da Ana.
    vazio = await decision_trail(store, "bruno", subject_id="PED-1001")
    assert vazio["decisions"] == []

    por_pedido = await decision_trail(store, "ana", subject_id="PED-1001")
    assert len(por_pedido["decisions"]) == 1


async def test_override_humano_preserva_a_recomendacao_do_agente():
    store = DataStore(Settings(demo_mode=True))
    agente = await record_decision(store, _decision())
    humano = await record_decision(store, _decision(
        decided_by="human", action="order_status_change_reverted",
        reasoning="Analista de qualidade reverteu: defeito de lote, não troca.",
        recommended_action="order_status_change", escalated=True,
        supersedes=agente["decision_id"],
    ), audit_event_type="human_override", severity="warning")

    # A decisão anterior continua existindo intacta: correção é uma nova decisão, não um update.
    anteriores = await store.find_many(DECISIONS_COLLECTION, {"decision_id": agente["decision_id"]})
    assert len(anteriores) == 1 and anteriores[0]["decided_by"] == "agent"
    assert humano["supersedes"] == agente["decision_id"]
    assert humano["recommended_action"] == "order_status_change"


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_confianca_e_opcional_mas_fica_no_documento(confidence):
    assert _decision(confidence=confidence)["confidence"] == confidence


async def test_falha_de_gravacao_nao_propaga_para_o_turno_do_cliente():
    # A escrita de negócio já ocorreu antes desta chamada. Propagar a exceção entregaria
    # ao cliente um erro depois da ação efetivada: mundo alterado, sem registro, e tela de
    # erro. A falha vira log + contador, e o retorno None avisa quem precisa do decision_id.
    store = DataStore(Settings(demo_mode=True))

    async def boom(*args, **kwargs):
        raise RuntimeError("validador recusou o documento")

    store.insert_one = boom
    assert await record_decision(store, _decision()) is None


async def test_evento_de_auditoria_que_falha_nao_descarta_a_decisao_ja_gravada():
    store = DataStore(Settings(demo_mode=True))
    original = store.insert_one

    async def flaky(name, document, **kwargs):
        if name == AUDIT_COLLECTION:
            raise RuntimeError("indisponível")
        return await original(name, document, **kwargs)

    store.insert_one = flaky
    decision = await record_decision(store, _decision())
    assert decision is not None
    assert len(await store.find_many(DECISIONS_COLLECTION, {})) == 1


async def test_transacao_desfaz_a_escrita_de_negocio_quando_a_decisao_falha():
    # O ponto de toda a transação: se o registro não entra, a mudança de status não fica.
    # Em DEMO_MODE a atomicidade é emulada por snapshot, então este teste exercita
    # comportamento de verdade e não um no-op que passaria de qualquer jeito.
    store = DataStore(Settings(demo_mode=True))
    await store.insert_one("orders", {"order_id": "PED-1001", "owner_customer_key": "ana",
                                      "status": "entregue"})
    original = store.insert_one

    async def flaky(name, document, **kwargs):
        if name == DECISIONS_COLLECTION:
            raise RuntimeError("validador recusou o documento")
        return await original(name, document, **kwargs)

    with pytest.raises(RuntimeError):
        async with store.transaction() as tx:
            await store.update_one("orders", {"order_id": "PED-1001"},
                                   {"$set": {"status": "troca_solicitada"}}, session=tx)
            store.insert_one = flaky
            await record_decision(store, _decision(), session=tx)

    store.insert_one = original
    pedido = await store.find_one("orders", {"order_id": "PED-1001"})
    assert pedido["status"] == "entregue", "o rollback tem que devolver o status anterior"
    assert await store.find_many(DECISIONS_COLLECTION, {}) == []


async def test_transacao_bem_sucedida_persiste_as_duas_escritas():
    store = DataStore(Settings(demo_mode=True))
    await store.insert_one("orders", {"order_id": "PED-1001", "owner_customer_key": "ana",
                                      "status": "entregue"})
    async with store.transaction() as tx:
        await store.update_one("orders", {"order_id": "PED-1001"},
                               {"$set": {"status": "troca_solicitada"}}, session=tx)
        await record_decision(store, _decision(), session=tx)

    pedido = await store.find_one("orders", {"order_id": "PED-1001"})
    assert pedido["status"] == "troca_solicitada"
    assert len(await store.find_many(DECISIONS_COLLECTION, {})) == 1
