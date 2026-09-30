"""Troca de produto como operação de domínio — o único caminho que efetiva `troca_solicitada`.

Por que isto existe como módulo, e não como um `if` dentro do agente: a regra ("não trocar
de novo um item que já falhou três vezes; isso é defeito de lote") precisa valer em TODO
caminho que leve a uma troca, e não apenas nos que alguém lembrou de instrumentar. A versão
anterior tinha a checagem escrita à mão dentro de dois agentes, e a frase mais natural do
cliente — "quero trocar o pedido PED-0000" — passava por um terceiro caminho que não a tinha.

O fechamento vem em duas partes, e uma sozinha não bastaria:

1. `policies.GUARDED_STATUSES` tira `troca_solicitada` do caminho genérico de escrita, então
   quem tentar por `safe_order_update` recebe uma negação que diz o que usar.
2. `apply_replacement` é o caminho guardado: consulta a cadeia, decide, e só então escreve —
   escrita e registro da decisão na mesma transação.

Isso não impede um `store.update_one` direto na collection; nada em Python impede. O que
muda é que o desvio deixa de ser acidental para ser deliberado e visível numa revisão.
"""

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .database import DataStore
from .decisions import build_decision_doc, record_decision
from .models import TimelineEvent
from .policies import guarded_order_update
from .reviews import open_review

REPLACEMENT_ACTION = "warranty_replacement"


@dataclass
class ReplacementOutcome:
    """Resultado da tentativa de troca, já pronto para virar resposta e timeline."""

    allowed: bool
    chain: dict[str, Any]
    events: list[TimelineEvent] = field(default_factory=list)
    review: dict[str, Any] | None = None
    changed: bool = False

    @property
    def blocked_by_recurrence(self) -> bool:
        return not self.allowed


def _chain_event(chain: dict[str, Any], *, agent: str, order_id: str, elapsed_ms: float) -> TimelineEvent:
    return TimelineEvent(
        category="agent",
        title=f"Cadeia de trocas via $graphLookup ({chain['replacements']} reposições)",
        agent=agent, collection="orders", op="graphLookup",
        filter={"order_id": order_id, "connectFrom": "replacement_order_id",
                "connectTo": "order_id"},
        result=chain, duration_ms=elapsed_ms,
    )


async def assess_replacement(store: DataStore, order_id: str, customer_key: str, *,
                             agent: str) -> tuple[dict[str, Any], TimelineEvent]:
    """Consulta a cadeia e devolve o veredito + o evento de timeline correspondente.

    Leitura pura: não escreve nada. É o que o `warranty_agent` usa para *explicar* a
    cobertura sem efetivar troca nenhuma.
    """
    from .agents import order_replacement_chain  # import tardio: evita ciclo agents ↔ replacement

    started = perf_counter()
    chain = await order_replacement_chain(store, order_id, customer_key)
    return chain, _chain_event(chain, agent=agent, order_id=order_id,
                               elapsed_ms=(perf_counter() - started) * 1000)


async def block_for_quality_review(store: DataStore, chain: dict[str, Any], *, agent: str,
                                   customer_key: str, conversation_id: str) -> dict[str, Any] | None:
    """Abre (ou reaproveita) a revisão humana do caso. Único lugar que monta esse texto."""
    return await open_review(
        store, agent=agent, action=REPLACEMENT_ACTION,
        subject_id=chain["root_order_id"], customer_key=customer_key,
        conversation_id=conversation_id, recommended_action="quality_analysis",
        reasoning=(f"{chain['product']} já exigiu {chain['replacements']} reposições na cadeia "
                   f"{' → '.join(chain['path'])} — padrão de defeito de lote, não uso indevido."),
        risk_factors=["defeito_recorrente", f"reposicoes:{chain['replacements']}"],
        evidence=chain,
    )


async def apply_replacement(store: DataStore, order: dict[str, Any], *, customer_key: str,
                            agent: str, conversation_id: str) -> ReplacementOutcome:
    """Efetiva a troca — ou a recusa, quando a cadeia mostra defeito recorrente.

    Único caminho que escreve `troca_solicitada`. Escrita e decisão vão na mesma transação:
    trocar o status sem registrar por quê é o furo que a trilha existe para impedir.
    """
    order_id = order["order_id"]
    chain, graph_event = await assess_replacement(store, order_id, customer_key, agent=agent)
    events = [graph_event]

    if chain["needs_quality_review"]:
        review = await block_for_quality_review(
            store, chain, agent=agent, customer_key=customer_key, conversation_id=conversation_id)
        if review:
            events.append(TimelineEvent(
                category="agent", title="Troca automática bloqueada: caso aguarda decisão humana",
                agent=agent, collection="pending_reviews", op="write",
                filter={"subject_id": order_id, "status": "pending"},
                result={"review_id": review["review_id"],
                        "recommended_action": review["recommended_action"]},
                reason=review["reasoning"]))
        return ReplacementOutcome(allowed=False, chain=chain, events=events, review=review)

    previous_status = order.get("status")
    if previous_status == "troca_solicitada":
        # Já estava trocado: nada a escrever, mas o veredito da cadeia continua valendo e o
        # chamador ainda precisa poder seguir para os handoffs de fatura/entrega.
        return ReplacementOutcome(allowed=True, chain=chain, events=events, changed=False)

    write_query, update = guarded_order_update(
        {"order_id": order_id, "status": "troca_solicitada"}, customer_key)
    write_started = perf_counter()
    async with store.transaction() as tx:
        await store.update_one("orders", write_query, update, session=tx)
        await record_decision(store, build_decision_doc(
            action="order_status_change", subject_id=order_id, customer_key=customer_key,
            agent=agent, conversation_id=conversation_id,
            reasoning=("Cliente pediu a troca e a cadeia de reposições não indica defeito "
                       f"recorrente ({chain['replacements']} reposição(ões) anterior(es))."),
            payload={"from": previous_status, "to": "troca_solicitada",
                     "product": order.get("product"), "chain_path": chain["path"]},
        ), session=tx)

    events.append(TimelineEvent(
        category="agent", title="Atualização segura de status do pedido",
        agent=agent, collection="orders", op="write", filter=write_query,
        result={"order_id": order_id, "status": "troca_solicitada"},
        duration_ms=(perf_counter() - write_started) * 1000))
    return ReplacementOutcome(allowed=True, chain=chain, events=events, changed=True)
