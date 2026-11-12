"""A/B single-agent × multi-agent na MESMA pergunta (resposta ao "don't build multi-agents").

Os dois braços usam as MESMAS tools, os mesmos portões de escrita e o mesmo modelo; muda só a
arquitetura: um generalista com todas as tools num loop, contra o time (plano do orquestrador,
especialistas com tool calling, quadro do turno e consolidação). Escritas são SIMULADAS nos dois
braços — comparar nunca altera pedido, pontos ou entrega. O juiz LLM dá completude, coerência e
grounding; latência, tokens e custo vêm da contabilidade real das chamadas.
"""

from __future__ import annotations

import asyncio
from time import perf_counter

from . import agent_loop, blackboard, consolidate as consolidate_module, hitl, judge as judge_module, planner, web_search
from .budget import TurnBudget
from .economics import summarize_calls
from .orchestration import FALLBACK_AGENTS, MAX_HOPS
from .router import cheap_route, deterministic_orchestrator, matched_agents
from .security import mask_pii

GENERALIST = {
    "agent_key": "generalist_agent", "label": "Generalista (A/B)", "model": "claude-sonnet-5-5",
    "fallback_model": "claude-sonnet-5-5", "all_tools": True, "max_turn_tokens": 40000, "max_output_tokens": 1400,
    "persona": "Você é o único agente de atendimento da loja: resolve pedidos, faturas, garantia, fidelidade, "
               "entrega, catálogo e suporte técnico do titular autenticado.",
    "active": True,
}


def _arm_result(name, response, budget, started, events, agents, tools_called, judged=None):
    economics = summarize_calls(budget.llm_calls)
    return {"arm": name, "response": response, "latency_ms": round((perf_counter() - started) * 1000),
            "tokens": budget.total_used, "llm_calls": len(budget.llm_calls),
            "cost_usd": economics.get("estimated_cost_usd"), "agents": agents, "tools_called": tools_called,
            "events": [e.model_dump(mode="json") for e in events][:60], "judge": judged or {}}


async def _single(store, llm, customer, message, ctx_base, policies):
    started = perf_counter()
    budget = TurnBudget(GENERALIST["max_turn_tokens"], {"generalist_agent": GENERALIST["max_turn_tokens"]})
    result = await agent_loop.run_tool_agent(
        store=store, llm=llm, budget=budget, agent_key="generalist_agent", agent_doc=GENERALIST, message=message,
        customer=customer, conversation_id="ab-single", turn_context=dict(ctx_base), task=None, board_summary="",
        detected={}, handoff_targets=[], hitl_policies=policies, simulate_writes=True)
    if result is None:
        return {"arm": "single", "available": False}
    events = [result.event, *result.extra_events]
    return _arm_result("single", result.response, budget, started, events, ["generalist_agent"],
                       result.event.result.get("tools_called", []))


async def _multi(store, llm, customer, message, ctx_base, policies, registry, rules):
    started = perf_counter()
    per_agent = {key: int(doc.get("max_turn_tokens") or 12000) for key, doc in registry.items()}
    budget = TurnBudget(60000, per_agent)
    decision = cheap_route(message, rules) or deterministic_orchestrator(message)
    current = decision.target_agent if decision.target_agent in registry else FALLBACK_AGENTS.get(decision.target_agent or "", "order_agent")
    detected = {k: v for k, v in matched_agents(message, rules).items() if k in registry}
    available = [k for k in agent_loop.AGENT_LABELS if k in registry]
    plan = []
    if planner.should_plan(message, detected):
        plan, _ = await planner.plan_turn(llm=llm, budget=budget, orchestrator_doc=registry.get("orchestrator"),
                                          message=message, first_agent=current, available=available,
                                          labels=agent_loop.AGENT_LABELS, max_steps=MAX_HOPS)
    board, events, visited, tools_called, task = [], [], [], [], (plan[0]["task"] if plan else None)
    tainted = False
    for hop in range(MAX_HOPS):
        visited.append(current)
        targets = [k for k in available if k not in visited] if hop < MAX_HOPS - 1 else []
        result = await agent_loop.run_tool_agent(
            store=store, llm=llm, budget=budget, agent_key=current, agent_doc=registry.get(current), message=message,
            customer=customer, conversation_id="ab-multi", turn_context=dict(ctx_base), task=task,
            board_summary=blackboard.summary_for_prompt(board), detected=detected, handoff_targets=targets,
            hitl_policies=policies, simulate_writes=True, tainted=tainted)
        if result is None:
            break
        tainted = tainted or result.tainted
        events += [result.event, *result.extra_events]
        tools_called += result.event.result.get("tools_called", [])
        board.append(blackboard.entry_for(hop, current, task, result.mode, result.actions, result.response,
                                          result.handoff_to))
        nxt, task = result.handoff_to, result.task_for_next
        if not nxt:
            step = next((s for s in plan if s["agent"] not in visited), None)
            nxt, task = (step["agent"], step["task"]) if step else (None, None)
        if not nxt or nxt in visited:
            break
        current, task = nxt, blackboard.safe_task(task)
    if not board:
        return {"arm": "multi", "available": False}
    response, event = await consolidate_module.consolidate(
        llm=llm, budget=budget, orchestrator_doc=registry.get("orchestrator"), message=message, entries=board, tail=[])
    events.append(event)
    return _arm_result("multi", response, budget, started, events, visited, tools_called)


async def compare(store, llm, customer: dict, message: str) -> dict:
    masked = mask_pii(message)
    agents = await store.find_many("agent_registry", {"active": True}, brain=True, limit=20)
    registry = {a["agent_key"]: {k: v for k, v in a.items() if k != "_id"} for a in agents}
    rules = await store.find_many("routing_rules", {}, brain=True, limit=100)
    policies = await hitl.load_policies(store)
    conversation = await store.find_one("agent_conversations", {"customer_key": customer["customer_key"]})
    ctx_base = {"conversation_id": "ab", "active_order_id": (conversation or {}).get("active_order_id"),
                "web_cfg": await web_search.get_config(store, customer["area"])}
    single, multi = await asyncio.gather(
        _single(store, llm, customer, masked, ctx_base, policies),
        _multi(store, llm, customer, masked, ctx_base, policies, registry, rules))
    async def _judge(arm):
        if arm.get("available") is False:
            return arm
        arm["judge"] = await judge_module.judge(llm, masked, arm["response"],
                                                judge_module.evidence_from_timeline(arm["events"]))
        return arm
    single, multi = await asyncio.gather(_judge(single), _judge(multi))
    from .guardrails import check_output
    for arm in (single, multi):
        # Mesmo guardrail de saída do chat: nada sai do A/B sem passar por ele.
        if arm.get("available") is not False and (await check_output(store, arm["response"], customer)).blocked:
            arm["response"] = "A resposta foi retida pela política de segurança."
    return {"message": masked, "single": single, "multi": multi,
            "note": "Escritas simuladas nos dois braços; mesmas tools, mesmos portões e mesmo modelo."}
