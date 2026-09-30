"""Spine de decisão e auditoria — o registro imutável do que o sistema decidiu, e por quê.

Diferença deliberada em relação a `agent_traces`/`agent_handoffs`: aqueles são observabilidade
(existem para depurar e para a timeline da UI, e expiram por TTL em 30 dias). Estes aqui são
registro de conformidade:

- `agent_decisions` — um documento por ação com efeito no mundo (mudou status de pedido, gastou
  pontos, abriu chamado, remarcou entrega). Imutável: nunca sofre update. Corrigir uma decisão
  significa gravar OUTRA decisão que a supersede, com `supersedes` apontando para a anterior.
- `agent_audit_events` — trilha append-only referenciando `decision_id`. É onde entram os
  eventos que não são a decisão em si: escalonamento, override humano, tentativa negada.

Quem decidiu fica explícito em `decided_by` (`agent` ou `human`). Quando um humano sobrepõe o
agente, a recomendação original é preservada em `recommended_action` — é o que permite medir a
taxa de override do agente ao longo do tempo, em vez de perdê-la.
"""

import logging
import uuid
from typing import Any, Literal

from .database import DataStore, utcnow
from .metrics import metrics

logger = logging.getLogger(__name__)

DECISIONS_COLLECTION = "agent_decisions"
AUDIT_COLLECTION = "agent_audit_events"

DecidedBy = Literal["agent", "human"]
Severity = Literal["info", "warning", "critical"]


def new_decision_id() -> str:
    return f"DEC-{uuid.uuid4().hex[:12].upper()}"


def build_decision_doc(
    *,
    action: str,
    subject_id: str,
    customer_key: str,
    agent: str,
    conversation_id: str,
    reasoning: str,
    decided_by: DecidedBy = "agent",
    confidence: float | None = None,
    risk_factors: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    recommended_action: str | None = None,
    escalated: bool = False,
    supersedes: str | None = None,
) -> dict:
    """Monta o documento de decisão. Não grava — quem grava é `record_decision`."""
    return {
        "decision_id": new_decision_id(),
        "action": action,
        "subject_id": subject_id,
        "customer_key": customer_key,
        "agent": agent,
        "conversation_id": conversation_id,
        "decided_by": decided_by,
        "reasoning": reasoning,
        "confidence": confidence,
        "risk_factors": risk_factors or [],
        "payload": payload or {},
        # Só é preenchido quando um humano decidiu diferente do agente: guarda o que o agente
        # tinha recomendado, para a taxa de override ser mensurável depois.
        "recommended_action": recommended_action,
        "escalated": escalated,
        "supersedes": supersedes,
        "at": utcnow(),
    }


def build_audit_event(
    event_type: str,
    *,
    subject_id: str,
    customer_key: str,
    decision_id: str | None = None,
    conversation_id: str | None = None,
    severity: Severity = "info",
    event_data: dict[str, Any] | None = None,
) -> dict:
    return {
        "event_id": f"AUD-{uuid.uuid4().hex[:12].upper()}",
        "event_type": event_type,
        "subject_id": subject_id,
        "customer_key": customer_key,
        "decision_id": decision_id,
        "conversation_id": conversation_id,
        "severity": severity,
        "event_data": event_data or {},
        "at": utcnow(),
    }


async def record_decision(store: DataStore, decision: dict, *, audit_event_type: str | None = None,
                          severity: Severity = "info", event_data: dict[str, Any] | None = None,
                          session=None) -> dict | None:
    """Grava a decisão e, junto, o evento de auditoria correspondente.

    Os dois inserts vão juntos de propósito: uma decisão sem rastro na trilha, ou um evento de
    trilha sem a decisão que o originou, é exatamente o buraco que auditoria procura.

    **Com `session`** (o caso normal, ver `DataStore.transaction`), a escrita de negócio e este
    registro são uma coisa só: falhar aqui desfaz a mudança de status/saldo lá atrás. É o que
    fecha o furo de "mundo alterado, sem registro". A exceção é deixada subir para que a
    transação aborte — engoli-la aqui faria o commit acontecer com a decisão faltando.

    **Sem escopo atômico** (servidor standalone, ou chamada fora de transação), vale o
    melhor esforço: a ação de negócio já aconteceu e não dá para desfazê-la, então derrubar o
    turno do cliente só somaria uma tela de erro a um mundo já alterado. A falha vira log de
    erro + contador `decisions.write_failures`, e a função devolve `None`.
    """
    try:
        await store.insert_one(DECISIONS_COLLECTION, decision, session=session)
    except Exception:
        if session is not None and session.atomic:
            raise  # dá para desfazer: propagar É o rollback
        logger.exception("falha ao gravar decisão %s (%s em %s)",
                         decision.get("decision_id"), decision.get("action"), decision.get("subject_id"))
        await metrics.increment("decisions.write_failures")
        return None
    audit = build_audit_event(
        audit_event_type or f"decision.{decision['action']}",
        subject_id=decision["subject_id"],
        customer_key=decision["customer_key"],
        decision_id=decision["decision_id"],
        conversation_id=decision.get("conversation_id"),
        severity=severity,
        event_data={"decided_by": decision["decided_by"], "action": decision["action"],
                    **(event_data or {})},
    )
    try:
        await store.insert_one(AUDIT_COLLECTION, audit, session=session)
    except Exception:
        if session is not None and session.atomic:
            raise
        # A decisão ficou gravada e o evento não: é uma inconsistência menor que perder a
        # decisão, mas ainda assim precisa ser vista — nunca engolida em silêncio.
        logger.exception("decisão %s gravada sem o evento de auditoria correspondente",
                         decision.get("decision_id"))
        await metrics.increment("decisions.audit_write_failures")
    return decision


async def decision_trail(store: DataStore, customer_key: str, *, subject_id: str | None = None,
                         limit: int = 50) -> dict:
    """Trilha completa de um cliente (ou de um pedido): decisões + eventos, mais recentes primeiro."""
    query: dict[str, Any] = {"customer_key": customer_key}
    if subject_id:
        query["subject_id"] = subject_id
    decisions = await store.find_many(DECISIONS_COLLECTION, query, limit=limit, sort=[("at", -1)])
    events = await store.find_many(AUDIT_COLLECTION, query, limit=limit * 2, sort=[("at", -1)])
    def strip(items):
        return [{k: v for k, v in item.items() if k != "_id"} for item in items]

    return {"decisions": strip(decisions), "audit_events": strip(events)}
