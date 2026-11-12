"""Aprovação humana antes de escrita sensível (ADR-012) — política é dado, pausa é checkpoint.

`<brain>.hitl_policies` diz quais chamadas de tool exigem um humano. Quando uma bate, a tool não
escreve: o agente devolve `pending_action`, o nó `approval` do StateGraph chama `interrupt()` e o
turno fica parado no `langgraph_checkpoints` do Atlas — sem conexão HTTP aberta, sobrevive a restart.
A decisão do analista (`POST /api/admin/reviews/{id}/resolve`) retoma o MESMO thread com
`Command(resume=...)`: a escrita aprovada roda uma única vez, e o resto da cadeia continua.
"""

from __future__ import annotations

from typing import Any

DEFAULT_POLICIES = [
    {
        "policy_id": "HITL-REEMBOLSO",
        "tool": "request_order_status_change",
        "when": {"status": "reembolsado"},
        "reason": "Reembolso devolve dinheiro ao cliente: um analista confirma antes da escrita.",
        "active": True,
    },
    {
        "policy_id": "HITL-RESGATE-ALTO",
        "tool": "redeem_reward",
        "when": {"points_gte": 1000},
        "reason": "Resgate de 1.000 pontos ou mais passa por aprovação.",
        "active": True,
    },
]


def matching_policy(policies: list[dict], tool: str, facts: dict[str, Any]) -> dict | None:
    for policy in policies or []:
        if not policy.get("active", True) or policy.get("tool") != tool:
            continue
        if all(_matches(key, expected, facts) for key, expected in (policy.get("when") or {}).items()):
            return policy
    return None


def _matches(key: str, expected: Any, facts: dict[str, Any]) -> bool:
    if key.endswith("_gte"):
        value = facts.get(key[:-4])
        return isinstance(value, (int, float)) and value >= expected
    return facts.get(key) == expected


async def load_policies(store) -> list[dict]:
    try:
        rows = await store.find_many("hitl_policies", {"active": True}, brain=True, limit=50)
    except Exception:  # noqa: BLE001 — sem a collection, vale a política padrão (falha fechada)
        rows = []
    return [{k: v for k, v in row.items() if k != "_id"} for row in rows] or list(DEFAULT_POLICIES)
