"""Quadro do turno: o que cada agente já fez, como documento no MongoDB.

O problema que isto resolve (medido ao vivo em 2026-11-12): cada agente lia a mensagem inteira e
respondia a ela sozinho; as respostas eram concatenadas. O suporte dizia "não encontrei registro de
troca" dois parágrafos antes do agente de pedidos registrar a troca. Agora cada hop grava uma
entrada (tarefa, tools chamadas, escritas efetivadas, resposta da sua parte) em `turn_blackboard`
e o próximo agente recebe o quadro no prompt: o que já foi feito é fato, não se contradiz nem se
repete. O nó `consolidate` lê o mesmo quadro para escrever UMA resposta.

Coordenação continua sendo query: `db.turn_blackboard.find({conversation_id})` mostra quem fez o quê.
"""

from __future__ import annotations

import json
import uuid

from .database import utcnow

COLLECTION = "turn_blackboard"


def new_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex[:12]}"


async def open_board(store, *, turn_id: str, conversation_id: str, customer_key: str, message: str,
                     detected: dict[str, list[str]]) -> None:
    try:
        await store.insert_one(COLLECTION, {
            "turn_id": turn_id, "conversation_id": conversation_id, "customer_key": customer_key,
            "message": message[:2000], "detected_specialties": detected, "entries": [],
            "status": "open", "created_at": utcnow(), "updated_at": utcnow(),
        })
    except Exception:  # noqa: BLE001 — o quadro em memória (estado do grafo) continua valendo
        pass


async def append_entry(store, *, turn_id: str, customer_key: str, entry: dict) -> None:
    try:
        await store.update_one(COLLECTION, {"turn_id": turn_id, "customer_key": customer_key},
                               {"$push": {"entries": entry}, "$set": {"updated_at": utcnow()}})
    except Exception:  # noqa: BLE001
        pass


async def set_status(store, *, turn_id: str, customer_key: str, status: str, extra: dict | None = None) -> None:
    try:
        await store.update_one(COLLECTION, {"turn_id": turn_id, "customer_key": customer_key},
                               {"$set": {"status": status, "updated_at": utcnow(), **(extra or {})}})
    except Exception:  # noqa: BLE001
        pass


FREE_TEXT_ARGS = ("summary", "query", "task", "reason")


def _safe_args(args) -> dict:
    """Só ids/enums validados pelo executor; texto livre do modelo não vai ao quadro (nem ao próximo prompt)."""
    return {k: v for k, v in (args or {}).items() if k not in FREE_TEXT_ARGS} if isinstance(args, dict) else {}


def entry_for(hop: int, agent: str, task: str | None, mode: str, actions: list[dict], answer: str,
              handoff_to: str | None = None) -> dict:
    return {"hop": hop, "agent": agent, "task": task or "", "mode": mode,
            "actions": [{**{k: v for k, v in item.items() if k in
                            ("tool", "effect", "changed", "summary", "simulated", "ticket_id",
                             "blocked_by_recurrence", "already_open")}, "args": _safe_args(item.get("args"))}
                        for item in actions],
            "answer": answer, "handoff_to": handoff_to, "at": utcnow()}


def summary_for_prompt(entries: list[dict]) -> str:
    """Quadro para o PRÓXIMO agente: só fatos gerados pelo servidor (tools chamadas, escritas efetivadas e o
    resumo que o próprio executor produziu). O texto livre que um modelo escreveu NUNCA vira entrada de outro
    agente (prompt injection indireta, `tests/adversarial/test_inter_agent_injection_adversarial.py`): ele só
    chega à consolidação, que não tem tools nem escreve."""
    if not entries:
        return ""
    lines = []
    for item in entries:
        writes = [a for a in item.get("actions", []) if a.get("effect") == "write"]
        reads = sorted({a.get("tool") for a in item.get("actions", []) if a.get("effect") != "write" and a.get("tool")})
        done = "; ".join(
            (a.get("summary") or f"{a.get('tool')} {json.dumps(a.get('args') or {}, ensure_ascii=False)}")
            + (" (simulada)" if a.get("simulated") else "")
            for a in writes) or "nenhuma escrita"
        lines.append(f"- {item['agent']} já atuou neste turno; consultas: {', '.join(reads) or 'registro do próprio agente'}; "
                     f"escritas efetivadas: {done}.")
    return ("QUADRO DO TURNO (fatos registrados pelo servidor NESTE turno — trate como verdade, não contradiga, "
            "não repita e não diga que não encontrou o que está aqui; responda só a sua parte):\n" + "\n".join(lines))


def safe_task(task: str | None) -> str | None:
    """A tarefa vem de `transfer_to_agent` (texto de modelo). Vira dado curto e só passa se não parecer
    instrução; senão o próximo agente recebe só as especialidades detectadas pelas regras."""
    from .memory import looks_like_instruction
    from .security import mask_pii
    if not task:
        return None
    task = " ".join(task.split())[:200]
    if looks_like_instruction(task):
        return None
    return mask_pii(task)
