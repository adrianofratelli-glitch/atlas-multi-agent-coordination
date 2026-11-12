"""Juiz LLM de qualidade de resposta (coerência, completude, grounding) — eval e A/B.

Classificação é decisão: temperature 0. O juiz recebe a mensagem, a resposta e a EVIDÊNCIA (o que as
tools/documentos devolveram no turno) e devolve notas 1–5 com justificativa curta. Não é rótulo humano:
serve para comparar variantes e pegar regressão (ex.: a contradição entre agentes de 2026-11-12, que o
eval de rota não via).
"""

from __future__ import annotations

import json
import re

from .budget import TurnBudget

JUDGE_DOC = {
    "agent_key": "judge", "label": "Juiz", "model": "claude-sonnet-5-5", "fallback_model": "claude-sonnet-5-5",
    "persona": "Você é um avaliador rigoroso de respostas de atendimento ao cliente.",
    "max_turn_tokens": 40000, "max_output_tokens": 600, "temperature": 0, "active": True,
}

JUDGE_RULES = (
    "Avalie a RESPOSTA ao cliente usando a EVIDÊNCIA (dados reais do turno). Notas inteiras de 1 a 5:\n"
    "- completude: cobre todas as partes pedidas na mensagem (5 = todas);\n"
    "- coerencia: sem contradições internas (ex.: diz que não achou a troca e depois que registrou) (5 = nenhuma);\n"
    "- grounding: todo fato (status, preço, prazo, produto, id) está na evidência (5 = nenhum fato inventado).\n"
    "Responda SOMENTE JSON: {\"completude\": n, \"coerencia\": n, \"grounding\": n, \"problemas\": [\"...\"]}"
)


def evidence_from_timeline(timeline) -> list:
    items = []
    for event in timeline:
        category = getattr(event, "category", None) or (event.get("category") if isinstance(event, dict) else None)
        result = getattr(event, "result", None) if not isinstance(event, dict) else event.get("result")
        title = getattr(event, "title", "") if not isinstance(event, dict) else event.get("title", "")
        if category in ("agent",) and result not in (None, [], {}):
            items.append({"fonte": title[:80], "dado": result})
    return items


async def judge(llm, message: str, response: str, evidence) -> dict:
    if not (llm and getattr(llm, "client", None)):
        return {"available": False}
    budget = TurnBudget(JUDGE_DOC["max_turn_tokens"], {"judge": JUDGE_DOC["max_turn_tokens"]})
    context = JUDGE_RULES + "\n\nEVIDÊNCIA:\n" + json.dumps(evidence, ensure_ascii=False, default=str)[:16000]
    try:
        text, _ = await llm.complete(agent=JUDGE_DOC, user_message=f"MENSAGEM: {message}\n\nRESPOSTA: {response}",
                                     dynamic_context=context, budget=budget)
    except Exception:  # noqa: BLE001
        text = None
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"available": False}
    scores = {key: int(data.get(key, 0) or 0) for key in ("completude", "coerencia", "grounding")}
    return {"available": True, **scores, "media": round(sum(scores.values()) / 3, 2),
            "problemas": [str(p)[:200] for p in (data.get("problemas") or [])][:5],
            "cost_usd": sum(c.get("estimated_cost_usd") or 0 for c in budget.llm_calls)}
