"""Escalonamento pausável — o caso para de avançar até um humano decidir.

O padrão vem do gate `requires_action` dos Managed Agents: quando o agente encontra um caso que
não deve resolver sozinho, ele não chuta nem inventa uma resposta segura — ele **para**, registra
o que recomendaria, e devolve o caso para um humano. Só que aqui a pausa é durável: mora em um
documento do MongoDB, não em uma conexão HTTP aberta. Uma requisição de chat não pode ficar
segurando o socket até um analista voltar do almoço.

O ciclo completo:

1. Agente cria `pending_reviews` com `status: "pending"` + a recomendação dele.
2. O turno termina normalmente — o cliente é avisado de que o caso está em análise.
3. Um analista resolve pelo endpoint admin.
4. A resolução grava a decisão final (`decided_by: "human"`), preserva o que o agente havia
   recomendado em `recommended_action`, e devolve o caso ao agente por um handoff real —
   que é o que faz o Change Stream já existente acordar a UI do cliente ao vivo.

`recommended_action` versus `human_decision` no mesmo documento é o que torna a taxa de override
do agente mensurável em vez de anedótica.
"""

import logging
import uuid
from typing import Any

from .database import DataStore, run_in_transaction_with_retry, utcnow
from .decisions import build_decision_doc, record_decision
from .metrics import metrics

logger = logging.getLogger(__name__)

REVIEWS_COLLECTION = "pending_reviews"
HUMAN_REVIEWER = "human_reviewer"


def new_review_id() -> str:
    return f"REV-{uuid.uuid4().hex[:10].upper()}"


async def open_review(
    store: DataStore,
    *,
    agent: str,
    action: str,
    subject_id: str,
    customer_key: str,
    conversation_id: str,
    recommended_action: str,
    reasoning: str,
    risk_factors: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict | None:
    """Abre a pausa. Idempotente por (subject_id, action): reabrir o mesmo caso não empilha
    duas revisões pendentes para o analista resolver duas vezes."""
    existing = await store.find_one(REVIEWS_COLLECTION, {
        "subject_id": subject_id, "action": action, "customer_key": customer_key, "status": "pending",
    })
    if existing:
        return {key: value for key, value in existing.items() if key != "_id"}

    review: dict[str, Any] = {
        "review_id": new_review_id(),
        "agent": agent,
        "action": action,
        "subject_id": subject_id,
        "customer_key": customer_key,
        "conversation_id": conversation_id,
        "recommended_action": recommended_action,
        "reasoning": reasoning,
        "risk_factors": risk_factors or [],
        "evidence": evidence or {},
        "status": "pending",
        "human_decision": None,
        "resolved_by": None,
        "resolved_at": None,
        "created_at": utcnow(),
    }
    # A pausa e a decisão de escalonamento entram juntas: uma revisão na fila do analista sem
    # a decisão que a originou é um caso órfão, e a decisão sem a revisão promete ao cliente
    # uma análise que ninguém vai ver.
    try:
        async with store.transaction() as tx:
            await store.insert_one(REVIEWS_COLLECTION, review, session=tx)
            await record_decision(store, build_decision_doc(
                action=f"{action}_escalated", subject_id=subject_id, customer_key=customer_key,
                agent=agent, conversation_id=conversation_id, reasoning=reasoning,
                risk_factors=risk_factors, recommended_action=recommended_action, escalated=True,
                payload={"review_id": review["review_id"], "evidence": evidence or {}},
            ), audit_event_type="escalated_to_human", severity="warning",
                event_data={"review_id": review["review_id"], "recommended_action": recommended_action},
                session=tx)
    except Exception:
        # Caminho do cliente: não conseguir abrir a pausa não pode virar erro na tela dele.
        # Devolvendo None, o agente responde sem PROMETER uma revisão que não existe — o que
        # seria pior que não escalar. A falha fica no log e no contador.
        logger.exception("falha ao abrir revisão para %s (%s)", subject_id, action)
        await metrics.increment("reviews.open_failures")
        return None
    return review


async def list_reviews(store: DataStore, *, customer_key: str | None = None,
                       status: str = "pending", limit: int = 50) -> list[dict]:
    query: dict[str, Any] = {"status": status}
    if customer_key:
        query["customer_key"] = customer_key
    items = await store.find_many(REVIEWS_COLLECTION, query, limit=limit, sort=[("created_at", -1)])
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]


async def resolve_review(store: DataStore, review_id: str, *, human_decision: str,
                         resolved_by: str, note: str = "") -> dict | None:
    """Resolve a revisão, repetindo (via `run_in_transaction_with_retry`) enquanto o MongoDB
    sinalizar conflito transitório.

    Repetir é o tratamento que o servidor pede em `TransientTransactionError` — e aqui ele
    converge sozinho: na segunda tentativa o caso já está `resolved`, o `find_one` por
    `status: "pending"` não acha nada, e o perdedor da corrida recebe o `None` que vira 404.
    """
    review = await store.find_one(REVIEWS_COLLECTION, {"review_id": review_id, "status": "pending"})
    if not review:
        return None

    overridden = human_decision != review["recommended_action"]

    async def _body(tx) -> bool:
        # A reivindicação continua sendo um update condicional em status="pending", mesmo
        # dentro da transação: é ele que resolve a corrida entre dois analistas: quem não
        # modificar documento nenhum perdeu, e sai sem efeito colateral. A transação cuida
        # de outra coisa — que ninguém veja o caso meio-fechado.
        claimed = await store.update_one(
            REVIEWS_COLLECTION,
            {"review_id": review_id, "status": "pending"},
            {"$set": {"status": "resolved", "human_decision": human_decision,
                      "resolved_by": resolved_by, "resolved_at": utcnow(), "note": note,
                      "overrode_agent": overridden}},
            session=tx,
        )
        if not claimed:
            return False

        decision = await record_decision(store, build_decision_doc(
            action=human_decision,
            subject_id=review["subject_id"],
            customer_key=review["customer_key"],
            agent=review["agent"],
            conversation_id=review.get("conversation_id", ""),
            decided_by="human",
            reasoning=note or f"Revisão humana do caso {review_id}.",
            risk_factors=review.get("risk_factors"),
            recommended_action=review["recommended_action"],
            escalated=True,
            payload={"review_id": review_id, "resolved_by": resolved_by, "overrode_agent": overridden},
        ), audit_event_type="human_override" if overridden else "human_confirmation",
            severity="warning" if overridden else "info",
            event_data={"review_id": review_id, "human_decision": human_decision,
                        "recommended_action": review["recommended_action"]},
            session=tx)

        if decision is None:
            # Só acontece fora de transação (standalone): ali o `raise` abaixo é o que
            # impede a revisão de ficar marcada como resolvida sem decisão registrada.
            raise RuntimeError(
                f"decisão final de {review_id} não pôde ser registrada; a revisão segue pendente"
            )

        await store.update_one(REVIEWS_COLLECTION, {"review_id": review_id},
                               {"$set": {"decision_id": decision["decision_id"]}}, session=tx)

        # Devolver o caso ao agente É um handoff — do revisor humano de volta para quem
        # escalou. Gravá-lo em agent_handoffs faz o Change Stream de /api/events/stream
        # acordar a UI do cliente na hora, sem nenhum canal novo. Dentro da transação de
        # propósito: o cliente não pode ser avisado de uma resolução que ainda pode abortar.
        #
        # O validador de agent_handoffs exige conversation_id com no mínimo 4 caracteres: uma
        # revisão aberta fora de uma conversa (batch, backfill) não derruba a resolução.
        if len(review.get("conversation_id") or "") >= 4:
            await store.insert_one("agent_handoffs", {
                "conversation_id": review["conversation_id"],
                "customer_key": review["customer_key"],
                "from_agent": HUMAN_REVIEWER,
                "to_agent": review["agent"],
                "reason": f"revisão humana concluída: {human_decision}"
                          + (f" (agente havia recomendado {review['recommended_action']})" if overridden else ""),
                "at": utcnow(),
            }, session=tx)
        return True

    claimed = await run_in_transaction_with_retry(store, _body)
    if not claimed:
        return None

    resolved = await store.find_one(REVIEWS_COLLECTION, {"review_id": review_id})
    return {key: value for key, value in (resolved or {}).items() if key != "_id"}


async def override_rate(store: DataStore, *, limit: int = 500) -> dict:
    """Taxa de override: de todas as revisões resolvidas, em quantas o humano decidiu diferente
    do que o agente recomendou. É a métrica que justifica (ou não) afrouxar o gate depois."""
    resolved = await store.find_many(REVIEWS_COLLECTION, {"status": "resolved"}, limit=limit)
    total = len(resolved)
    overrides = sum(1 for item in resolved if item.get("overrode_agent"))
    return {"resolved": total, "overrides": overrides,
            "override_rate": round(overrides / total, 4) if total else 0.0}
