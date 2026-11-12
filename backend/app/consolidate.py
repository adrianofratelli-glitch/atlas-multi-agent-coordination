"""Consolidação: uma resposta coerente a partir do quadro do turno (padrão orchestrator-workers).

Cada especialista escreveu só a sua parte; o orquestrador (persona do registry: "classifique, roteie e
consolide") junta as partes numa resposta única, na ordem do pedido do cliente, sem fato novo: o
quadro é a única fonte. Escrita efetivada é verdade e prevalece sobre qualquer frase que a contradiga.
Sem LLM ou com falha, a consolidação determinística junta as partes (o comportamento anterior).
"""

from __future__ import annotations

import json

from .models import TimelineEvent

CONSOLIDATE_RULES = (
    "Você é o orquestrador de um time de agentes de atendimento. Abaixo está o QUADRO DO TURNO: o que cada "
    "especialista fez e respondeu. Escreva UMA resposta final ao cliente, em português, que:\n"
    "- cubra todas as partes do pedido, na ordem em que o cliente pediu;\n"
    "- use SOMENTE fatos do quadro (nunca invente status, prazo, preço, produto, link ou número);\n"
    "- trate como verdade as escritas efetivadas; se alguma frase de um especialista contradiz uma escrita "
    "efetivada (ex.: 'não encontrei registro de troca' depois de a troca ter sido registrada), descarte a frase;\n"
    "- mantenha identificadores (PED-, FAT-, TCK-, REV-), valores, datas e as linhas de fontes/links;\n"
    "- se algo ficou pendente de aprovação humana ou foi adiado, diga isso claramente;\n"
    "- não mencione agentes, ferramentas, quadro ou sistema; sem saudação; use negrito só em status/valores; "
    "parágrafos curtos ou lista."
)


def deterministic(entries: list[dict], tail: list[str]) -> str:
    parts = [item["answer"].strip() for item in entries if item.get("answer", "").strip()]
    seen, unique = set(), []
    for part in parts + [t for t in tail if t]:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return "\n\n".join(unique)


async def consolidate(*, llm, budget, orchestrator_doc: dict | None, message: str, entries: list[dict],
                      tail: list[str]) -> tuple[str, TimelineEvent]:
    """Devolve (resposta, evento). `tail` = avisos do servidor (taint, laço, degradação) que entram no fim."""
    fallback = deterministic(entries, tail)
    if len([e for e in entries if e.get("answer")]) < 2 or not (llm and getattr(llm, "client", None) and orchestrator_doc):
        return fallback, TimelineEvent(category="coordination", agent="orchestrator",
                                       title="Consolidação determinística (um agente ou sem modelo)",
                                       collection="turn_blackboard", op="read",
                                       result={"mode": "deterministic", "entries": len(entries)})
    board = [{"agente": e["agent"], "tarefa": e.get("task"), "escritas": [a for a in e.get("actions", []) if a.get("effect") == "write"],
              "resposta_do_especialista": e.get("answer", "")} for e in entries]
    context = (CONSOLIDATE_RULES + "\n\nQUADRO DO TURNO:\n" + json.dumps(board, ensure_ascii=False, default=str)
               + ("\n\nAVISOS DO SISTEMA (inclua no fim, com suas palavras): " + " | ".join(t for t in tail if t) if any(tail) else ""))
    doc = {**orchestrator_doc, "max_output_tokens": max(900, int(orchestrator_doc.get("max_output_tokens") or 0))}
    try:
        text, _ = await llm.complete(agent=doc, user_message=f"Mensagem original do cliente: {message}",
                                     dynamic_context=context, budget=budget)
    except Exception:  # noqa: BLE001 — BudgetExceeded inclusive: a resposta das partes ainda serve
        text = None
    if not text:
        return fallback, TimelineEvent(category="coordination", agent="orchestrator",
                                       title="Consolidação determinística (modelo indisponível)",
                                       collection="turn_blackboard", op="read",
                                       result={"mode": "deterministic_fallback", "entries": len(entries)})
    return text.strip(), TimelineEvent(category="coordination", agent="orchestrator",
                                       title="Orquestrador consolidou o quadro do turno numa resposta única",
                                       collection="turn_blackboard", op="read",
                                       result={"mode": "llm", "entries": len(entries),
                                               "agents": [e["agent"] for e in entries]})
