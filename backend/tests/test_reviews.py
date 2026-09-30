import asyncio

import pytest

from app.config import Settings
from app.database import DataStore
from app.decisions import AUDIT_COLLECTION, DECISIONS_COLLECTION
from app.reviews import (HUMAN_REVIEWER, REVIEWS_COLLECTION, list_reviews, open_review,
                         override_rate, resolve_review)


async def _escalate(store: DataStore, **overrides) -> dict:
    base = dict(agent="warranty_agent", action="warranty_replacement", subject_id="PED-3001",
                customer_key="carla", conversation_id="conv-1",
                recommended_action="quality_analysis",
                reasoning="Smartwatch Fit falhou 4 vezes na mesma cadeia.",
                risk_factors=["defeito_recorrente"], evidence={"replacements": 3})
    return await open_review(store, **(base | overrides))


async def test_escalar_cria_pausa_e_ja_registra_decisao_de_escalonamento():
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)

    assert review["status"] == "pending"
    assert review["human_decision"] is None
    decisions = await store.find_many(DECISIONS_COLLECTION, {})
    assert decisions[0]["action"] == "warranty_replacement_escalated"
    assert decisions[0]["escalated"] is True
    events = await store.find_many(AUDIT_COLLECTION, {})
    assert events[0]["event_type"] == "escalated_to_human"
    assert events[0]["severity"] == "warning"


async def test_escalar_o_mesmo_caso_duas_vezes_nao_empilha_a_fila_do_analista():
    store = DataStore(Settings(demo_mode=True))
    first = await _escalate(store)
    second = await _escalate(store)

    assert first["review_id"] == second["review_id"]
    assert len(await list_reviews(store)) == 1


async def test_resolucao_grava_decisao_humana_e_devolve_o_caso_por_handoff():
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)

    resolved = await resolve_review(store, review["review_id"], human_decision="quality_analysis",
                                    resolved_by="ana.qualidade", note="Lote 2026-05 recolhido.")

    assert resolved["status"] == "resolved"
    assert resolved["overrode_agent"] is False  # humano confirmou a recomendação do agente
    final = await store.find_one(DECISIONS_COLLECTION, {"decision_id": resolved["decision_id"]})
    assert final["decided_by"] == "human"
    assert final["recommended_action"] == "quality_analysis"

    # O retorno ao agente é um handoff real: é o que acorda o Change Stream da UI do cliente.
    handoffs = await store.find_many("agent_handoffs", {})
    assert handoffs[0]["from_agent"] == HUMAN_REVIEWER
    assert handoffs[0]["to_agent"] == "warranty_agent"
    assert handoffs[0]["customer_key"] == "carla"


async def test_override_humano_e_marcado_e_entra_na_taxa():
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)
    resolved = await resolve_review(store, review["review_id"], human_decision="refund",
                                    resolved_by="ana.qualidade", note="Cliente prefere reembolso.")

    assert resolved["overrode_agent"] is True
    assert resolved["human_decision"] == "refund"
    events = await store.find_many(AUDIT_COLLECTION, {"event_type": "human_override"})
    assert len(events) == 1
    assert await override_rate(store) == {"resolved": 1, "overrides": 1, "override_rate": 1.0}


async def test_resolver_duas_vezes_o_mesmo_caso_nao_grava_segunda_decisao():
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)
    await resolve_review(store, review["review_id"], human_decision="refund", resolved_by="ana")

    assert await resolve_review(store, review["review_id"], human_decision="reject", resolved_by="bruno") is None
    finais = await store.find_many(DECISIONS_COLLECTION, {"decided_by": "human"})
    assert len(finais) == 1


async def test_taxa_de_override_ignora_casos_ainda_pendentes():
    store = DataStore(Settings(demo_mode=True))
    pendente = await _escalate(store, subject_id="PED-3001")
    outro = await _escalate(store, subject_id="PED-1002", customer_key="ana")
    await resolve_review(store, outro["review_id"], human_decision="quality_analysis", resolved_by="ana")

    assert (await override_rate(store))["resolved"] == 1
    assert len(await list_reviews(store, status="pending")) == 1
    assert (await list_reviews(store, status="pending"))[0]["review_id"] == pendente["review_id"]


async def _breaking_store() -> DataStore:
    store = DataStore(Settings(demo_mode=True))
    original = store.insert_one

    async def flaky(name, document, **kwargs):
        if name in {DECISIONS_COLLECTION, REVIEWS_COLLECTION}:
            raise RuntimeError("validador recusou o documento")
        return await original(name, document, **kwargs)

    store.insert_one = flaky
    return store


async def test_falha_ao_abrir_revisao_nao_derruba_o_turno_do_cliente():
    # A escrita de negócio já aconteceu quando chegamos aqui: deixar a exceção subir daria
    # ao cliente uma tela de erro DEPOIS da ação efetivada. Devolver None é o certo — o
    # agente responde sem prometer uma revisão que não existe.
    store = await _breaking_store()
    assert await _escalate(store) is None
    assert await list_reviews(store) == []


async def test_falha_ao_registrar_decisao_final_mantem_o_caso_pendente():
    # Caminho do analista: o oposto. Fechar a pausa sem registrar a decisão é o furo de
    # auditoria que estas collections existem para impedir — então falha alto e o caso fica.
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)
    original = store.insert_one

    async def flaky(name, document, **kwargs):
        if name == DECISIONS_COLLECTION:
            raise RuntimeError("validador recusou o documento")
        return await original(name, document, **kwargs)

    store.insert_one = flaky
    with pytest.raises(RuntimeError):
        await resolve_review(store, review["review_id"], human_decision="refund", resolved_by="ana")

    ainda_pendente = await list_reviews(store, status="pending")
    assert len(ainda_pendente) == 1
    assert ainda_pendente[0]["review_id"] == review["review_id"]
    assert ainda_pendente[0]["human_decision"] is None


async def test_dois_analistas_no_mesmo_caso_geram_uma_unica_decisao():
    # A reivindicação é atômica (update condicional em status="pending"): quem perde a
    # corrida sai sem efeito colateral, em vez de gravar uma segunda decisão humana.
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)

    resultados = await asyncio.gather(
        resolve_review(store, review["review_id"], human_decision="refund", resolved_by="ana"),
        resolve_review(store, review["review_id"], human_decision="reject", resolved_by="bruno"),
        return_exceptions=True,
    )
    vencedores = [item for item in resultados if isinstance(item, dict)]
    assert len(vencedores) == 1

    humanas = await store.find_many(DECISIONS_COLLECTION, {"decided_by": "human"})
    assert len(humanas) == 1
    assert await list_reviews(store, status="pending") == []


async def test_caso_reivindicado_mas_nao_registrado_volta_para_a_fila():
    # "resolving" preso para sempre seria invisível ao analista: pior que nunca ter sido
    # reivindicado. A falha de gravação devolve o caso a "pending".
    store = DataStore(Settings(demo_mode=True))
    review = await _escalate(store)
    original = store.insert_one

    async def flaky(name, document, **kwargs):
        if name == DECISIONS_COLLECTION:
            raise RuntimeError("indisponível")
        return await original(name, document, **kwargs)

    store.insert_one = flaky
    with pytest.raises(RuntimeError):
        await resolve_review(store, review["review_id"], human_decision="refund", resolved_by="ana")

    pendentes = await list_reviews(store, status="pending")
    assert len(pendentes) == 1 and pendentes[0]["status"] == "pending"
