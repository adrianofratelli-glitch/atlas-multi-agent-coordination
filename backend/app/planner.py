"""Plano do turno (padrão orchestrator-workers): o orquestrador decompõe a mensagem em tarefas.

Medido ao vivo (2026-11-12): sem plano, a cadeia dependia de cada especialista lembrar de transferir;
"depois veja a entrega" ficava sem dono quando o agente de pedidos encerrava. Com o plano gravado no
quadro do turno (`turn_blackboard.plan`), o servidor garante cobertura: quando um agente termina sem
transferir, o próximo passo do plano que ainda não rodou recebe a vez.

Regras que mantêm os invariantes:
- O primeiro agente continua sendo o da decisão determinística (`cheap_route`/prioridade): o plano
  nunca sobrescreve uma rota confiante; ele só ordena o que vem depois.
- Classificação é decisão: temperature 0. Saída é JSON validado contra o registry (agente inexistente,
  repetido ou além de MAX_HOPS é descartado). Falha/sem modelo: sem plano, cadeia como antes.
- Só roda quando a mensagem tem cara de pedido composto (2+ especialidades detectadas ou frase longa).
"""

from __future__ import annotations

import json
import re

from .models import TimelineEvent

PLANNER_RULES = (
    "Você é o orquestrador de um time de atendimento de varejo. Decomponha a mensagem do cliente em tarefas, "
    "uma por especialista, na ordem em que precisam acontecer (diagnóstico antes de recomendação; recomendação "
    "antes de efetivar troca; efetivar antes de confirmar entrega/fatura). Use só estes especialistas:\n{catalog}\n"
    "Responda SOMENTE um JSON: {{\"steps\": [{{\"agent\": \"<chave>\", \"task\": \"<o que este especialista deve "
    "resolver, em uma frase>\"}}]}}. No máximo {max_steps} passos; um especialista aparece uma vez. Inclua SÓ "
    "especialistas sem os quais o pedido fica sem resposta: nada de passo 'por garantia' (ex.: garantia só se o "
    "cliente perguntou de garantia/cobertura; suporte só se pediu diagnóstico ou ajuda técnica; confirmação "
    "de troca é do agente de pedidos, não um passo extra). Cada especialista a mais custa tempo e dinheiro."
)


def should_plan(message: str, detected: dict) -> bool:
    return len(detected) >= 2 or len(message.split()) >= 18


def _parse(text: str) -> list[dict]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    steps = data.get("steps") if isinstance(data, dict) else None
    return steps if isinstance(steps, list) else []


async def plan_turn(*, llm, budget, orchestrator_doc: dict | None, message: str, first_agent: str,
                    available: list[str], labels: dict[str, str], max_steps: int) -> tuple[list[dict], TimelineEvent | None]:
    if not (llm and getattr(llm, "client", None) and orchestrator_doc):
        return [], None
    catalog = "\n".join(f"- {key}: {labels.get(key, key)}" for key in available)
    context = PLANNER_RULES.format(catalog=catalog, max_steps=max_steps)
    try:
        text, _ = await llm.complete(agent={**orchestrator_doc, "temperature": 0, "max_output_tokens": 800},
                                     user_message=message, dynamic_context=context, budget=budget)
    except Exception:  # noqa: BLE001 — plano é otimização de cobertura, nunca derruba o turno
        text = None
    raw = _parse(text or "")
    steps, seen = [], set()
    for item in raw:
        agent = (item or {}).get("agent") if isinstance(item, dict) else None
        task = " ".join(str((item or {}).get("task") or "").split())[:200]
        if agent in available and agent not in seen:
            seen.add(agent)
            steps.append({"agent": agent, "task": task})
    if not steps:
        return [], None
    # A rota determinística abre a cadeia; o plano só ordena o resto.
    first = next((step for step in steps if step["agent"] == first_agent), {"agent": first_agent, "task": ""})
    ordered = [first] + [step for step in steps if step["agent"] != first_agent]
    ordered = ordered[:max_steps]
    return ordered, TimelineEvent(category="coordination", agent="orchestrator", collection="turn_blackboard",
                                  op="write", title=f"Plano do turno: {len(ordered)} tarefa(s) decompostas pelo orquestrador",
                                  result={"plan": ordered})
