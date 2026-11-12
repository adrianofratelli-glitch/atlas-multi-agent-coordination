"""Orquestração do turno multi-agente como um StateGraph do LangGraph.

`OrchestrationService.run_turn` era uma função linear de ~200 linhas com
múltiplos `return` antecipados (bloqueado pelo guardrail, fanout, fora de
escopo, cache hit, cadeia completa de handoff). Isto reorganiza o MESMO
comportamento em nós de grafo com checkpoint nativo (`MongoDBSaver`,
`thread_id=<customer_key>:<conversation_id>`), sem tocar a lógica de roteamento, guardrail,
cascata semântica, orçamento ou segurança — que continuam em
`router.py`/`guardrails.py`/`cascade.py`/`budget.py`, chamadas pelos nós
exatamente como antes.

A instância de `OrchestrationService` (que carrega `store`/`llm`/orçamento
global) e os métodos que já existiam nela (`_route_with_llm`, `_run_fanout`,
`_update_conversation`, `_persist_trace`, `_response`, `_usage`) são
reaproveitados via `config["configurable"]["service"]` — não fazem parte do
estado do grafo porque carregam um client HTTP e uma conexão de banco, que
não fazem sentido serializados num checkpoint.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from time import perf_counter
from typing import Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph
from pymongo import MongoClient as SyncMongoClient

from . import chaos, metrics as metrics_module, observability, resilience
from .budget import BudgetExceeded, TurnBudget, estimate_tokens
from .cascade import (GLOBAL_CACHE_INTENTS, cascade_long_term_context, cascade_lookup,
                       cascade_store_episode, cascade_store_short_term, cascade_store_turn)
from .database import utcnow
from .guardrails import check_input, check_output, needs_security_review
from .guidance import (blocked_reply, build_suggestions, customer_snapshot, greeting_reply,
                        is_capabilities_question, is_greeting, is_meta_question, is_thanks,
                        looks_like_own_pii, meta_reply, out_of_scope_reply, pii_block_reply,
                        thanks_reply)
from .memory import active_budget, extract_and_store, looks_like_instruction
from . import web_search
from .models import ChatResponse, TimelineEvent
from .router import (RouteDecision, cheap_route, deterministic_orchestrator, detect_fanout,
                      has_catalog_anchor, has_domain_signal, has_weak_signal,
                      out_of_scope_sentences)
from .security import mask_pii

logger = logging.getLogger("multiagent.orchestration_graph")

metrics = metrics_module.metrics


class TurnState(TypedDict, total=False):
    message: str
    customer: dict
    conversation_id: str
    requested_conversation_id: Optional[str]
    masked: str
    timeline: list
    registry: dict
    budget: TurnBudget
    conversation: Optional[dict]
    quick_decision: Optional[RouteDecision]
    scope_verdict: Optional[dict]
    no_domain_signal: bool
    suspicious: bool
    pre_route: Optional[str]
    canned_route: bool
    weak_product_route: bool
    written_facts: list
    fanout_targets: Optional[list]
    decision: RouteDecision
    scope_reason: Optional[str]
    chat_verdict: bool
    target: str
    route_source: str
    cascade: object
    output: object
    started: float
    # Chaves "privadas" repassadas entre nós — precisam estar declaradas aqui:
    # uma chave que um nó retorna e que não é campo do TypedDict é DESCARTADA
    # em silêncio pelo StateGraph, não vira erro na hora do `return` (já
    # derrubou dois cenários na PoV irmã singleagent antes de virar regra
    # fixa aqui — ver backend/agent_graph.py daquela PoV).
    _guardrail_blocked: bool
    _own_pii: bool
    # Config efetiva da busca web para a área do cliente (tool_registry), ou None = desligada (ADR-005).
    web_cfg: Optional[dict]
    # Cadeia de agentes, um hop por super-step (ADR-011/012).
    tail_start: int
    first_hint: str
    current: str
    hop: int
    visit_counts: dict
    handoff_path: list
    responses: list
    tail_notes: list
    board: list
    handoff_chain: list
    web_tainted: bool
    loop_visits: list
    next_agent: Optional[str]
    next_task: Optional[str]
    next_task_in: Optional[str]
    pending_action: Optional[dict]
    turn_id: str
    detected: dict
    hitl_policies: list
    touched: dict
    plan: list
    resumed: bool
    final_response: str


def _svc(config):
    return config["configurable"]["service"]


def _no_id(doc):
    """Remove `_id` (bson.ObjectId) de um documento do Mongo antes dele entrar no
    estado do grafo. O checkpointer serializa o estado inteiro a cada super-step, e
    ObjectId não é serializável — um documento cru vindo de `find_one`/`find_many`
    quebrava o `MongoDBSaver.put()` bem no fim do turno: o nó já tinha terminado e
    gravado no banco de verdade, mas a exceção do checkpoint subia por cima disso,
    o handler global caía no caminho degradado e devolvia um `conversation_id` novo
    e diferente do que a conversa REAL usou — a resposta ao cliente e o documento
    gravado divergiam (visto primeiro pelo cenário de caos `crash_resume`)."""
    if doc is None:
        return None
    return {key: value for key, value in doc.items() if key != "_id"}


async def n_ingest(state: TurnState, config) -> dict:
    svc = _svc(config)
    started = perf_counter()
    requested_conversation_id = state.get("requested_conversation_id")
    conversation_id = state["conversation_id"]
    masked = mask_pii(state["message"])
    timeline: list = []

    agents, rules, memory, conversation, web_cfg, model_config = await asyncio.gather(
        svc.store.find_many("agent_registry", {"active": True}, brain=True, limit=20),
        svc.store.find_many("routing_rules", {}, brain=True, limit=100),
        svc.store.find_many("customer_memory", {"customer_key": state["customer"]["customer_key"], "active": True}, limit=5),
        svc.store.find_one("agent_conversations", {"conversation_id": conversation_id, "customer_key": state["customer"]["customer_key"]}),
        web_search.get_config(svc.store, state["customer"]["area"]),
        svc.store.find_one("model_config", {"key": "default"}, brain=True),
    )
    if requested_conversation_id and conversation is None:
        conversation_id = f"conv-{uuid.uuid4().hex[:12]}"
    conversation = _no_id(conversation)
    registry = {agent["agent_key"]: _no_id(agent) for agent in agents}
    per_agent = {key: int(value["max_turn_tokens"]) for key, value in registry.items()}
    # Orçamento global é dado (`<brain>.model_config.global_turn_tokens`, editável sem deploy); o env é o default.
    global_budget = int((model_config or {}).get("global_turn_tokens") or svc.global_budget)
    budget = TurnBudget(global_budget, per_agent)

    quick_decision = cheap_route(masked, rules)
    scope_verdict = None
    # Rota de produto apoiada só no verbo genérico ("recomenda"), sem âncora de catálogo: vale para a regra seedada
    # (`cheap_route`) E para o palpite determinístico do orquestrador ("recomendações de restaurantes", "um bom
    # thriller") — nos dois casos o classificador de escopo pode recusar com 0 tokens.
    product_guess = quick_decision is None and deterministic_orchestrator(masked).target_agent == "product_agent"
    weak_product_route = ((quick_decision is not None and quick_decision.target_agent == "product_agent") or product_guess) \
        and not has_catalog_anchor(masked)
    from .orchestration import reaches_scope_classifier
    from . import scope_classifier
    if (quick_decision is None and reaches_scope_classifier(masked)) or weak_product_route:
        try:
            scope_verdict = await scope_classifier.classify(svc.store, masked)
        except Exception:  # noqa: BLE001 — classificador de escopo nunca derruba o turno
            scope_verdict = None
        if scope_verdict is not None and (scope_verdict.get("method") != "vector" or scope_verdict.get("error")):
            scope_verdict = None
    if scope_verdict is not None:
        no_domain_signal = scope_verdict["scope"] in ("out", "chat")
    else:
        no_domain_signal = (quick_decision is None and not has_domain_signal(masked) and not has_weak_signal(masked)
                             and deterministic_orchestrator(masked).source == "fallback")
    suspicious = looks_like_instruction(masked) or needs_security_review(masked)

    pre_route, canned_route = None, False
    orchestrator_doc = registry.get("orchestrator")
    if (scope_verdict is not None and scope_verdict["scope"] == "unsure" and quick_decision is None and not suspicious
            and not has_domain_signal(masked) and orchestrator_doc and svc.llm.client):
        pre_route = await svc._route_with_llm(masked, orchestrator_doc, [key for key in _runner_keys() if key in registry], budget)
        canned_route = (pre_route or "").strip().lower().startswith(("nenhum", "conversa"))

    return {
        "conversation_id": conversation_id, "masked": masked, "timeline": timeline,
        "registry": registry, "budget": budget, "conversation": conversation,
        "quick_decision": quick_decision, "scope_verdict": scope_verdict,
        "no_domain_signal": no_domain_signal, "suspicious": suspicious,
        "pre_route": pre_route, "canned_route": canned_route,
        "weak_product_route": weak_product_route, "started": started, "web_cfg": web_cfg,
    }


def _runner_keys():
    from .agents import RUNNERS
    return RUNNERS


async def n_guardrail(state: TurnState, config) -> dict:
    svc = _svc(config)
    budget = state["budget"]
    skip_guardrail_llm = (state["quick_decision"] is not None or state["no_domain_signal"]
                           or state["canned_route"]) and not state["suspicious"]
    guardrail = await check_input(svc.store, state["masked"], state["customer"], llm=svc.llm, budget=budget,
                                   agent_doc=state["registry"].get("orchestrator"), skip_semantic=skip_guardrail_llm)
    timeline = state["timeline"]
    guardrail_title = "Guardrail de entrada"
    if guardrail.blocked and guardrail.reason == "semantic_llm":
        guardrail_title += " (classificado pelo modelo)"
    elif guardrail.uncertain:
        guardrail_title += " (dúvida do modelo — abstenção, seguiu com o turno, fila de revisão)"
    timeline.append(TimelineEvent(category="guardrail", title=guardrail_title, collection="guardrail_denylist",
                                   op="read", filter={"area": state["customer"]["area"]},
                                   result={"blocked": guardrail.blocked, "score": guardrail.score,
                                           "reason": guardrail.reason, "uncertain": guardrail.uncertain,
                                           **({"scored_clause": guardrail.clause} if guardrail.clause else {})}))
    return {"timeline": timeline, "budget": budget, "_guardrail_blocked": guardrail.blocked,
            "_own_pii": looks_like_own_pii(state["masked"])}


def _route_after_guardrail(state: TurnState) -> str:
    return "blocked" if state.get("_guardrail_blocked") else "memory_extract"


async def n_blocked(state: TurnState, config) -> dict:
    svc = _svc(config)
    timeline, budget = state["timeline"], state["budget"]
    await metrics.increment("guardrails.blocked")
    snapshot = await customer_snapshot(svc.store, state["customer"])
    response = (
        pii_block_reply(snapshot) if state.get("_own_pii")
        else blocked_reply(snapshot, base="Não posso atender essa solicitação porque ela viola a política de segurança.")
    )
    trace_url = await svc._persist_trace(state["conversation_id"], state["customer"], state["masked"], response,
                                          timeline, "guardrail", svc._usage(budget),
                                          (perf_counter() - state["started"]) * 1000, llm_calls=budget.llm_calls)
    await _record_collection_metrics(timeline)
    output = svc._response(budget, conversation_id=state["conversation_id"], response=response, active_agent="guardrail",
                            route_source="fallback", cache_hit=False, timeline=timeline, usage=svc._usage(budget),
                            suggestions=build_suggestions(snapshot), langfuse_trace_url=trace_url)
    return {"output": output}


async def _record_collection_metrics(timeline: list) -> None:
    for event in timeline:
        if event.collection and event.op and not event.replayed:
            await metrics.increment(f"collection.{event.collection}.{event.op}")


async def n_memory_extract(state: TurnState, config) -> dict:
    svc = _svc(config)
    budget = state["budget"]
    written_facts = await extract_and_store(svc.store, state["customer"]["customer_key"], state["masked"],
                                             llm=svc.llm, budget=budget, agent_doc=state["registry"].get("orchestrator"))
    timeline = state["timeline"]
    if written_facts:
        timeline.append(TimelineEvent(category="memory", title="Fato extraído do turno e persistido (LLM + supersessão)",
                                       collection="customer_memory", op="write",
                                       filter={"customer_key": state["customer"]["customer_key"]}, result=written_facts))
    return {"written_facts": written_facts, "timeline": timeline, "budget": budget}


async def n_fanout_check(state: TurnState, config) -> dict:
    svc = _svc(config)
    rules = await svc.store.find_many("routing_rules", {}, brain=True, limit=100)
    fanout_targets = detect_fanout(state["masked"], rules)
    from .agents import _requested_status
    if fanout_targets and _requested_status(state["masked"]) == "reembolsado":
        # Reembolso passa pela cadeia, onde existe a pausa para aprovação humana (o fan-out não pausa).
        fanout_targets = None
    if fanout_targets and all(target in state["registry"] for target in fanout_targets):
        return {"fanout_targets": fanout_targets}
    return {"fanout_targets": None}


def _route_after_fanout_check(state: TurnState) -> str:
    return "fanout" if state.get("fanout_targets") else "decide"


async def n_fanout(state: TurnState, config) -> dict:
    svc = _svc(config)
    output = await svc._run_fanout(state["fanout_targets"], state["masked"], state["customer"], state["registry"],
                                    state["budget"], state["conversation_id"], state["conversation"],
                                    state["timeline"], state["started"])
    return {"output": output}


async def n_decide(state: TurnState, config) -> dict:
    svc = _svc(config)
    masked = state["masked"]
    decision = state["quick_decision"]
    scope_reason, chat_verdict = None, False
    if state["weak_product_route"] and state["scope_verdict"] is not None and state["scope_verdict"]["scope"] == "out":
        decision = RouteDecision("fora_de_escopo", None, "fallback", 0.0)
        scope_reason = "classificador_de_escopo"
    if decision is None:
        decision = deterministic_orchestrator(masked)
        orchestrator = state["registry"].get("orchestrator")
        llm_ok = bool(orchestrator and svc.llm.client)
        scope_verdict = state["scope_verdict"]
        if decision.source == "fallback" and not has_domain_signal(masked):
            if scope_verdict is not None:
                refuse = scope_verdict["scope"] in ("out", "chat") or (scope_verdict["scope"] == "unsure" and not llm_ok)
                reason = "classificador_de_escopo"
                chat_verdict = scope_verdict["scope"] == "chat"
            else:
                refuse = not (has_weak_signal(masked) and llm_ok)
                reason = "sem_sinal_de_dominio" if not has_weak_signal(masked) else "sinal_fraco_sem_classificador"
            if refuse:
                decision = RouteDecision("fora_de_escopo", None, "fallback", 0.0)
                scope_reason = reason
        if decision.source == "fallback" and decision.target_agent is not None and llm_ok:
            allowed = [key for key in _runner_keys() if key in state["registry"]]
            llm_route = state["pre_route"] if state["pre_route"] is not None else await svc._route_with_llm(
                masked, orchestrator, allowed, state["budget"])
            first_line = (llm_route or "").strip().splitlines()[0].strip().lower() if llm_route else ""
            matched = next((key for key in allowed if key.lower() == first_line), None) or next(
                (key for key in allowed if key in (llm_route or "")), None)
            if first_line.startswith("conversa"):
                decision = RouteDecision("fora_de_escopo", None, "fallback", 0.0)
                scope_reason, chat_verdict = "classificador_conversa", True
            elif first_line.startswith("nenhum") or (llm_route or "").strip().lower()[:20].startswith("nenhum"):
                decision = RouteDecision("fora_de_escopo", None, "fallback", 0.0)
                scope_reason = "classificador_nenhum"
            elif matched:
                decision = RouteDecision("classificacao_llm", matched, "orchestrator", 0.9)
            elif not has_domain_signal(masked):
                decision = RouteDecision("fora_de_escopo", None, "fallback", 0.0)
                scope_reason = "classificador_indisponivel"
    return {"decision": decision, "scope_reason": scope_reason, "chat_verdict": chat_verdict}


def _route_after_decide(state: TurnState) -> str:
    decision = state["decision"]
    if decision.target_agent is None and decision.source == "fallback":
        return "out_of_scope"
    return "cache_lookup"


async def n_out_of_scope(state: TurnState, config) -> dict:
    svc = _svc(config)
    masked, customer, timeline = state["masked"], state["customer"], state["timeline"]
    snapshot = await customer_snapshot(svc.store, customer)
    if is_greeting(masked) or is_capabilities_question(masked):
        response, titulo = greeting_reply(snapshot, customer=customer), "Abertura de conversa — orquestrador apresenta o que existe para o cliente"
    elif is_thanks(masked):
        response, titulo = thanks_reply(snapshot, customer=customer), "Encerramento cordial — conversa segue aberta"
    elif is_meta_question(masked):
        response, titulo = meta_reply(snapshot, customer=customer), "Pergunta sobre o próprio atendimento — resposta honesta sobre a arquitetura"
    elif state["chat_verdict"]:
        response, titulo = greeting_reply(snapshot, customer=customer), "Conversa cordial — orquestrador apresenta o que existe para o cliente"
    else:
        response, titulo = out_of_scope_reply(snapshot, customer=customer), "Fora de escopo — orquestrador orienta com os dados reais do cliente"
    if titulo.startswith("Fora de escopo"):
        timeline.append(TimelineEvent(category="guardrail", title="Guardrail de escopo: pergunta fora do domínio da loja",
                                       result={"blocked": False, "out_of_scope": True,
                                               "reason": state["scope_reason"] or "sem_sinal_de_dominio"}))
    timeline.append(TimelineEvent(category="agent", title=titulo, agent="orchestrator",
                                   collection="orders + invoices + loyalty_accounts + shipments", op="read",
                                   filter={"owner_customer_key": customer["customer_key"]},
                                   result={"sugestoes": [item["topic"] for item in build_suggestions(snapshot)]}))
    await metrics.increment("routing.out_of_scope")
    budget = state["budget"]
    trace_url = await svc._persist_trace(state["conversation_id"], customer, masked, response, timeline, "orchestrator",
                                          svc._usage(budget), (perf_counter() - state["started"]) * 1000, llm_calls=budget.llm_calls)
    await _record_collection_metrics(timeline)
    output = svc._response(budget, conversation_id=state["conversation_id"], response=response, active_agent="orchestrator",
                            route_source="fallback", cache_hit=False, timeline=timeline, usage=svc._usage(budget),
                            suggestions=build_suggestions(snapshot), langfuse_trace_url=trace_url)
    return {"output": output}


TOPIC_BY_AGENT = {
    "order_agent": "order", "billing_agent": "invoice", "logistics_agent": "shipment",
    "loyalty_agent": "loyalty", "product_agent": "product", "warranty_agent": "order",
}


async def _next_steps(store, customer, *, covered):
    try:
        snapshot = await customer_snapshot(store, customer)
        return build_suggestions(snapshot, exclude=covered)
    except Exception:  # noqa: BLE001 — enfeite útil, nunca caminho crítico
        return []


async def n_cache_lookup(state: TurnState, config) -> dict:
    svc = _svc(config)
    decision, customer, registry = state["decision"], state["customer"], state["registry"]
    target = decision.target_agent or "order_agent"
    route_source = decision.source
    from .agents import RUNNERS
    from .orchestration import FALLBACK_AGENTS
    if target not in registry:
        target = FALLBACK_AGENTS.get(target, "order_agent")
        if target not in registry:
            target = next((key for key in RUNNERS if key in registry), "order_agent")
        route_source = "fallback"
    timeline = state["timeline"]
    timeline.append(TimelineEvent(category="agent", title="Roteamento inicial", agent=target,
                                   collection="multiagent_brain.routing_rules" if decision.source == "rules" else "multiagent_brain.agent_registry",
                                   op="read", filter={"intent": decision.intent},
                                   result={"target_agent": target, "source": route_source, "confidence": decision.confidence}))
    if target == "product_agent" and await active_budget(svc.store, customer["customer_key"]) is not None:
        from .cascade import CascadeResult
        cascade = CascadeResult(hit=False, personal_reason="orcamento")
    else:
        cascade = await cascade_lookup(svc.store, target=target, area=customer["area"], customer_key=customer["customer_key"],
                                        session_id=state["conversation_id"], message=state["masked"])
    return {"target": target, "route_source": route_source, "cascade": cascade, "timeline": timeline}


def _route_after_cache(state: TurnState) -> str:
    return "cache_hit" if state["cascade"].hit else "handoff_chain"


async def n_cache_hit(state: TurnState, config) -> dict:
    svc = _svc(config)
    cascade, target, timeline = state["cascade"], state["target"], state["timeline"]
    customer, budget = state["customer"], state["budget"]
    await metrics.increment(f"agent.{target}.cache_hits")
    await metrics.increment(f"cache.hits.{cascade.fonte}")
    await metrics.increment("tokens.economizados", cascade.tokens_economizados)
    timeline.append(TimelineEvent(category="cache", title=f"Cascata semântica: HIT ({cascade.fonte})", agent=target,
                                   collection="short_term_memory" if cascade.fonte == "curto_prazo" else "semantic_cache",
                                   op="vectorSearch", filter={"session_id": state["conversation_id"], "agent": target},
                                   result={"hit": True, "fonte": cascade.fonte, "score": cascade.score,
                                           "classifier_score": (cascade.classifier or {}).get("score")}))
    response = cascade.answer or ""
    cached_active_agent = cascade.active_agent or target
    cached_timeline = timeline + [TimelineEvent(**{**event, "replayed": True}) for event in cascade.timeline]
    await cascade_store_short_term(svc.store, target=target, area=customer["area"], customer_key=customer["customer_key"],
                                    session_id=state["conversation_id"], message=state["masked"], answer=response,
                                    timeline=cascade.timeline or [], active_agent=cached_active_agent)
    await svc._update_conversation(state["conversation_id"], customer, state["masked"], response, cached_active_agent, [], cached_timeline)
    trace_url = await svc._persist_trace(state["conversation_id"], customer, state["masked"], response, cached_timeline,
                                          cached_active_agent, svc._usage(budget), (perf_counter() - state["started"]) * 1000,
                                          llm_calls=budget.llm_calls)
    await _record_collection_metrics(cached_timeline)
    output = svc._response(budget, conversation_id=state["conversation_id"], response=response, active_agent=cached_active_agent,
                            route_source=state["route_source"], cache_hit=True, cache_source=cascade.fonte,
                            tokens_economizados=cascade.tokens_economizados, timeline=cached_timeline, usage=svc._usage(budget),
                            suggestions=await _next_steps(svc.store, customer, covered={TOPIC_BY_AGENT.get(cached_active_agent, "")}),
                            langfuse_trace_url=trace_url)
    return {"output": output}


async def _confirmed_out_of_scope(store, sentences: list[str]) -> list[str]:
    """A lista de palavras propõe as frases "alheias"; o embedding (ADR-004) confirma. Só sai como "isso foge" a frase
    com veredito `out` DECISIVO: continuação curta do mesmo assunto ("quantos dias faltam?", "quando chega?") mede
    `in`/`unsure` e era anexada como fora de escopo logo depois de a resposta tê-la respondido. Sem veredito real
    (DEMO_MODE, índice ausente, erro) vale a lista de palavras, como antes."""
    from . import scope_classifier
    confirmed = []
    for sentence in sentences:
        try:
            verdict = await scope_classifier.classify(store, sentence)
        except Exception:  # noqa: BLE001 — classificador nunca derruba o turno
            verdict = None
        real = verdict is not None and verdict.get("method") == "vector" and not verdict.get("error")
        if not real or verdict["scope"] == "out":
            confirmed.append(sentence)
    return confirmed


async def n_chain_prepare(state: TurnState, config) -> dict:
    """Início do caminho caro (MISS de cache): contexto de longo prazo, quadro do turno e estado da cadeia."""
    svc = _svc(config)
    from . import blackboard, hitl
    from .router import matched_agents
    customer, conversation = state["customer"], state["conversation"]
    masked, conversation_id = state["masked"], state["conversation_id"]
    target, cascade, timeline = state["target"], state["cascade"], state["timeline"]
    timeline.append(TimelineEvent(category="cache", title="Cascata semântica: MISS (curto prazo + cache global)", agent=target,
                                   collection="short_term_memory", op="vectorSearch",
                                   filter={"session_id": conversation_id, "agent": target},
                                   result={"hit": False, **({"personal": cascade.personal_reason, "classifier": cascade.classifier}
                                                             if cascade.personal_reason else {})}))
    long_term = await cascade_long_term_context(svc.store, customer_key=customer["customer_key"], message=masked)
    if long_term:
        timeline.append(TimelineEvent(category="memory", title="Memória de longo prazo recuperada (contexto pro prompt)", agent=target,
                                       collection="long_term_memory", op="vectorSearch",
                                       filter={"customer_key": customer["customer_key"]}, result={"count": len(long_term)}))
    tail_start = len(timeline)
    long_term_hint = (
        " Contexto de longo prazo sobre este cliente (memória semântica/episódica, não é resposta pronta, "
        "use só como pano de fundo): " + " | ".join(str(item.get("text", "")) for item in long_term)
    ) if long_term else ""
    recent_turns = (conversation or {}).get("turns", [])[-6:]
    history_hint = (
        " Histórico real desta conversa até agora, na ordem em que aconteceu (se o cliente perguntar "
        "o que ele já disse/perguntou antes, responda com base nisso, nunca diga que não tem registro): "
        + " | ".join(f"{item['role']}: {item['content']}" for item in recent_turns)
    ) if recent_turns else ""
    state["budget"].reserve(target, estimate_tokens(masked))
    rules = await svc.store.find_many("routing_rules", {}, brain=True, limit=100)
    detected = {key: words for key, words in matched_agents(masked, rules).items() if key in state["registry"]}
    turn_id = blackboard.new_turn_id()
    await blackboard.open_board(svc.store, turn_id=turn_id, conversation_id=conversation_id,
                                customer_key=customer["customer_key"], message=masked, detected=detected)
    timeline.append(TimelineEvent(category="coordination", title="Quadro do turno aberto (blackboard)", agent="orchestrator",
                                   collection="turn_blackboard", op="write", filter={"turn_id": turn_id},
                                   result={"detected_specialties": detected}))
    from . import planner
    from .agent_loop import AGENT_LABELS, tool_calling_enabled
    from .agents import RUNNERS
    from .orchestration import MAX_HOPS
    plan: list[dict] = []
    if tool_calling_enabled() and planner.should_plan(masked, detected):
        plan, plan_event = await planner.plan_turn(
            llm=svc.llm, budget=state["budget"], orchestrator_doc=state["registry"].get("orchestrator"),
            message=masked, first_agent=target, available=[key for key in RUNNERS if key in state["registry"]],
            labels=AGENT_LABELS, max_steps=MAX_HOPS)
        if plan_event:
            timeline.append(plan_event)
            await blackboard.set_status(svc.store, turn_id=turn_id, customer_key=customer["customer_key"],
                                        status="open", extra={"plan": plan})
    return {
        "timeline": timeline, "tail_start": tail_start, "first_hint": history_hint + long_term_hint,
        "current": target, "hop": 0, "visit_counts": {target: 1}, "handoff_path": [target],
        "responses": [], "tail_notes": [], "board": [], "handoff_chain": [], "web_tainted": False,
        "loop_visits": [], "next_agent": None, "next_task": None, "pending_action": None,
        "turn_id": turn_id, "detected": detected, "hitl_policies": await hitl.load_policies(svc.store),
        "touched": {}, "plan": plan,
        "next_task_in": (plan[0]["task"] if plan and plan[0]["agent"] == target else None),
    }


def _handoff_targets(state: TurnState, current: str) -> list[str]:
    from .agents import RUNNERS
    from .orchestration import ALLOWED_REVISITS, MAX_HOPS, MAX_VISITS_PER_AGENT
    if state["hop"] >= MAX_HOPS - 1:
        return []
    visits = state["visit_counts"]
    return [key for key in RUNNERS if key in state["registry"] and key != current and (
        visits.get(key, 0) == 0 or ((current, key) in ALLOWED_REVISITS and visits.get(key, 0) < MAX_VISITS_PER_AGENT))]


async def n_agent_hop(state: TurnState, config) -> dict:
    """UM hop da cadeia: um agente (tool calling, ou runner determinístico como fallback), sua entrada no
    quadro e a validação do próximo destino. Cada hop é um super-step com checkpoint no Atlas."""
    svc = _svc(config)
    from . import agent_loop, blackboard, reviews
    from .agents import RUNNERS
    from .orchestration import (ALLOWED_REVISITS, FALLBACK_AGENTS, MAX_HOPS, MAX_VISITS_PER_AGENT,
                                 WRITE_EFFECT_AGENTS, _supervisor_state)
    customer, registry, budget = state["customer"], state["registry"], state["budget"]
    masked, conversation_id, decision = state["masked"], state["conversation_id"], state["decision"]
    timeline, responses, tail = state["timeline"], list(state["responses"]), list(state["tail_notes"])
    current, hop = state["current"], state["hop"]
    crash_at = config["configurable"].get("crash_before_hop")
    if crash_at is not None and hop == crash_at and not config["configurable"].get("_crashed"):
        config["configurable"]["_crashed"] = True
        raise SimulatedCrash(f"queda simulada antes do hop {hop}")
    out: dict = {"next_agent": None, "next_task": None, "pending_action": None}
    runner = RUNNERS.get(current)
    if not runner:
        return out
    if state["web_tainted"] and current in WRITE_EFFECT_AGENTS:
        await metrics.increment("web.taint_blocked")
        tail.append("Para eu seguir com a próxima etapa, confirme o pedido numa nova mensagem — neste atendimento "
                    "usei uma fonte externa (web) e, por segurança, não altero nada no mesmo turno.")
        timeline.append(TimelineEvent(category="handoff", title="Etapa com escrita adiada: turno usou busca web",
                                       agent=current, result={"blocked_agent": current, "reason": "web_taint"},
                                       reason="web_taint"))
        return {**out, "tail_notes": tail, "timeline": timeline}
    if current in WRITE_EFFECT_AGENTS and hop > 0:
        live_agent = await svc.store.find_one("agent_registry", {"agent_key": current}, brain=True)
        if not live_agent or not live_agent.get("active", False):
            tail.append(f"O agente de destino ({current}) foi desativado durante o atendimento; "
                        "mantive a orientação já disponível sem processar esta etapa.")
            return {**out, "tail_notes": tail}
    supervisor = _supervisor_state(conversation_id)
    guard = supervisor["guard"]
    for agent_key, intent in state["loop_visits"]:
        guard.visit(agent_key, intent)
    loop_visits = [*state["loop_visits"], [current, decision.intent]]
    if supervisor["graceful"] and guard.visit(current, decision.intent):
        await metrics.increment("supervisor.loop_guard")
        tail.append(resilience.HUMAN_HANDOFF_REPLY)
        timeline.append(TimelineEvent(category="handoff", title="Supervisor interrompeu: laço detectado", agent=current,
                                       result={"loop_on": list(guard.tripped_on or ()), "escalated_to": "humano"},
                                       reason="loop_guard"))
        return {**out, "tail_notes": tail, "timeline": timeline, "loop_visits": loop_visits}
    await metrics.increment(f"agent.{current}.turns")
    touched = dict(state.get("touched") or {})
    conversation = state["conversation"] or {}
    turn_context = {
        "conversation_id": conversation_id,
        "active_order_id": touched.get("order_id") or conversation.get("active_order_id"),
        "active_invoice_id": touched.get("invoice_id") or conversation.get("active_invoice_id"),
        "handoff_path": list(state["handoff_path"]), "visit_counts": dict(state["visit_counts"]),
        "returning_from": state["handoff_path"][-2] if len(state["handoff_path"]) > 1 else None,
        "web_cfg": state.get("web_cfg"),
    }
    board_summary = blackboard.summary_for_prompt(state["board"])
    hint = (state["first_hint"] if hop == 0 else "") + (("\n" + board_summary) if board_summary else "")
    targets = _handoff_targets(state, current)
    agent_doc = registry.get(current)
    with observability.span("agent", agent=current, conversation_id=conversation_id, hop=hop, intent=decision.intent):
        async def _run_agent():
            await chaos.hook("agent", name=current)
            calls_before = len(budget.llm_calls)
            result = await agent_loop.run_tool_agent(
                store=svc.store, llm=svc.llm, budget=budget, agent_key=current, agent_doc=agent_doc,
                message=masked, customer=customer, conversation_id=conversation_id, turn_context=turn_context,
                task=state.get("next_task_in"), board_summary=board_summary, detected=state["detected"],
                handoff_targets=targets, hitl_policies=state["hitl_policies"],
                extra_hint=state["first_hint"] if hop == 0 else "", tainted=state["web_tainted"])
            if result is None:
                # Provedor falhou no tool calling (erro/breaker): o template determinístico responde sem pagar
                # o retry de novo na síntese. Sem tentativa (sem modelo, flag desligada): runner normal.
                provider_failed = any(call.get("status") in ("error", "circuit_open")
                                      for call in budget.llm_calls[calls_before:])
                result = await runner(svc.store, masked, customer, None if provider_failed else svc.llm,
                                      budget, agent_doc, hint, turn_context)
            return result

        call = _run_agent()
        if not supervisor["graceful"]:
            result = await call
        else:
            try:
                result = await resilience.run_with_timeout(call, supervisor["timeout"])
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 — degradação graciosa (padrão)
                await metrics.increment(f"agent.{current}.failures")
                tail.append(resilience.degraded_reply(current))
                timeline.append(TimelineEvent(category="agent", title="Agente degradado (falha contida pelo supervisor)",
                                               agent=current, result={"error_type": type(exc).__name__,
                                                                      "timeout_seconds": supervisor["timeout"]}))
                return {**out, "tail_notes": tail, "timeline": timeline, "loop_visits": loop_visits}
    timeline.append(result.event)
    timeline.extend(result.extra_events)
    responses.append(result.response)
    for event in [result.event, *result.extra_events]:
        if isinstance(event.result, dict) and event.result.get("order_id"):
            touched.setdefault("order_id", event.result["order_id"])
        if isinstance(event.result, dict) and event.result.get("invoice_id"):
            touched.setdefault("invoice_id", event.result["invoice_id"])
    touched.update({k: v for k, v in (result.touched or {}).items() if v})
    web_tainted = state["web_tainted"] or result.tainted or any(
        event.op == "external" for event in [result.event, *result.extra_events])
    budget.reserve(current, estimate_tokens(result.response))
    actions = result.actions
    if result.mode == "deterministic":
        # Runner determinístico não registra ações: deriva do que a timeline mostra que ele fez.
        actions = [{"tool": f"{event.collection}.{event.op}", "effect": "write" if event.op == "write" else "read",
                    "summary": event.title} for event in [result.event, *result.extra_events]
                   if event.collection and event.op in ("read", "write", "vectorSearch", "hybridSearch", "graphLookup")]
    entry = blackboard.entry_for(hop, current, state.get("next_task_in"), result.mode, actions,
                                 result.response, result.handoff_to)
    board = [*state["board"], entry]
    await blackboard.append_entry(svc.store, turn_id=state["turn_id"], customer_key=customer["customer_key"], entry=entry)
    timeline.append(TimelineEvent(category="coordination", agent=current, collection="turn_blackboard", op="write",
                                   title=f"Quadro do turno: entrada de {current}", filter={"turn_id": state["turn_id"]},
                                   result={"hop": hop, "mode": result.mode,
                                           "writes": [a.get("tool") for a in result.actions if a.get("effect") == "write"]}))
    update = {**out, "timeline": timeline, "responses": responses, "tail_notes": tail, "board": board,
              "web_tainted": web_tainted, "loop_visits": loop_visits, "touched": touched}
    if result.pending_action:
        pending = {**result.pending_action, "hop": hop, "handoff_to": result.handoff_to,
                   "handoff_reason": result.handoff_reason, "task_for_next": result.task_for_next}
        review = await reviews.open_review(
            svc.store, agent=current, action=f"hitl_{pending['tool']}",
            subject_id=str(pending["args"].get("order_id") or pending["args"].get("reward") or customer["customer_key"]),
            customer_key=customer["customer_key"], conversation_id=conversation_id,
            recommended_action="approve", reasoning=pending.get("reason") or "política de aprovação",
            risk_factors=[pending.get("policy_id") or "hitl"],
            evidence={"tool": pending["tool"], "args": pending["args"], "facts": pending.get("facts"),
                      "thread_id": checkpoint_thread_id(customer, conversation_id), "turn_id": state["turn_id"],
                      "kind": "hitl_interrupt"})
        pending["review_id"] = (review or {}).get("review_id")
        timeline.append(TimelineEvent(category="agent", agent=current, collection="pending_reviews", op="write",
                                       title="Escrita pausada: aguarda aprovação humana (interrupt no checkpoint)",
                                       filter={"review_id": pending["review_id"]},
                                       result={"tool": pending["tool"], "args": pending["args"],
                                               "policy": pending.get("policy_id"), "review_id": pending["review_id"]},
                                       reason=pending.get("reason")))
        await blackboard.set_status(svc.store, turn_id=state["turn_id"], customer_key=customer["customer_key"],
                                    status="awaiting_approval", extra={"review_id": pending["review_id"]})
        return {**update, "pending_action": pending, "timeline": timeline}
    if hop == MAX_HOPS - 1:
        return update
    handoff_to, reason, task = result.handoff_to, result.handoff_reason, result.task_for_next
    if not handoff_to:
        # Cobertura garantida pelo servidor: o agente terminou sem transferir, mas o plano tem passo pendente.
        step = next((s for s in state.get("plan") or [] if s["agent"] in registry
                     and state["visit_counts"].get(s["agent"], 0) == 0 and s["agent"] != current), None)
        if not step:
            return update
        handoff_to, reason, task = step["agent"], f"plano do turno: {step['task']}", step["task"]
    destination = handoff_to
    if destination not in registry:
        destination = FALLBACK_AGENTS.get(destination, state["target"])
    visit_counts = dict(state["visit_counts"])
    is_revisit = visit_counts.get(destination, 0) > 0
    revisit_allowed = ((current, destination) in ALLOWED_REVISITS and visit_counts.get(destination, 0) < MAX_VISITS_PER_AGENT)
    if destination == current or (is_revisit and not revisit_allowed):
        tail.append("O agente de destino está desativado ou já atuou neste turno; mantive a orientação disponível sem criar um handoff circular.")
        return {**update, "tail_notes": tail}
    # A tarefa do plano (visão do pedido inteiro, feita pelo orquestrador) vale mais que a da transferência.
    planned = next((s["task"] for s in state.get("plan") or [] if s["agent"] == destination and s.get("task")), None)
    task = planned or task
    return await _record_handoff(svc, state, update, current, destination, reason,
                                 task or reason, is_revisit, visit_counts)


async def _record_handoff(svc, state, update, current, destination, reason, task, is_revisit, visit_counts):
    customer, conversation_id, timeline = state["customer"], state["conversation_id"], update["timeline"]
    handoff = {"conversation_id": conversation_id, "customer_key": customer["customer_key"], "from_agent": current,
               "to_agent": destination, "reason": reason, "at": utcnow()}
    if task:
        handoff["task"] = task[:300]
    with observability.span("handoff", **{"handoff.from": current, "handoff.to": destination,
                                           "conversation_id": conversation_id, "handoff.reason": reason or ""}):
        await svc.store.insert_one("agent_handoffs", handoff)
    timeline.append(TimelineEvent(category="handoff", title="Retorno controlado" if is_revisit else "Handoff explícito",
                                   agent=current, collection="agent_handoffs", op="write", filter={"conversation_id": conversation_id},
                                   result={"to_agent": destination, "revisit": is_revisit, "task": task}, reason=reason))
    await metrics.increment(f"agent.{current}.handoffs")
    if is_revisit:
        await metrics.increment("coordination.revisits")
    tail = list(update.get("tail_notes", []))
    if chaos.enabled():
        try:
            await chaos.hook("handoff", name=f"{current}->{destination}", phase="between_handoffs")
        except Exception as exc:  # noqa: BLE001
            if not resilience.graceful_degradation():
                raise
            tail.append(resilience.degraded_reply(destination))
            timeline.append(TimelineEvent(category="handoff", title="Handoff degradado (falha contida pelo supervisor)",
                                           agent=current, result={"error_type": type(exc).__name__, "to_agent": destination}))
            return {**update, "tail_notes": tail, "timeline": timeline}
    visit_counts[destination] = visit_counts.get(destination, 0) + 1
    return {**update, "timeline": timeline, "tail_notes": tail, "visit_counts": visit_counts,
            "handoff_chain": [*state["handoff_chain"], handoff], "handoff_path": [*state["handoff_path"], destination],
            "next_agent": destination, "next_task": task, "hop": state["hop"] + 1}


def _route_after_hop(state: TurnState) -> str:
    if state.get("pending_action"):
        return "approval"
    if state.get("next_agent"):
        return "advance"
    return "consolidate"


async def n_advance(state: TurnState, config) -> dict:
    """Passa o bastão: o destino validado vira o agente corrente, com a tarefa que recebeu."""
    from .blackboard import safe_task
    return {"current": state["next_agent"], "next_task_in": safe_task(state.get("next_task")), "next_agent": None}


async def n_approval(state: TurnState, config) -> dict:
    """Pausa real (LangGraph `interrupt`): o turno para aqui, com o estado no checkpoint do Atlas.

    Nada antes do `interrupt()` tem efeito colateral — na retomada este nó roda de novo do início. A
    escrita aprovada roda depois dele, uma única vez, pelo MESMO executor da tool (mesmos portões)."""
    from langgraph.types import interrupt
    from . import blackboard, tools as tools_module
    pending = state["pending_action"]
    decision = interrupt({"review_id": pending.get("review_id"), "tool": pending["tool"], "args": pending["args"],
                          "agent": pending["agent"], "reason": pending.get("reason")})
    svc = _svc(config)
    customer, timeline = state["customer"], state["timeline"]
    approved = isinstance(decision, dict) and decision.get("approved")
    responses = list(state["responses"])
    if approved:
        ctx = tools_module.ToolContext(
            store=svc.store, customer=customer, message=state["masked"], conversation_id=state["conversation_id"],
            agent_key=pending["agent"], agent_doc=state["registry"].get(pending["agent"]),
            turn_context={"conversation_id": state["conversation_id"],
                          "active_order_id": (state.get("touched") or {}).get("order_id"),
                          "web_cfg": state.get("web_cfg")},
            hitl_policies=state["hitl_policies"], approved_action={"tool": pending["tool"]})
        result = await tools_module.execute(ctx, pending["tool"], pending["args"])
        timeline.extend(ctx.events)
        summary = (result.content or {}).get("resumo") if isinstance(result.content, dict) else None
        text = (f"Aprovado por um analista ({decision.get('resolved_by', 'analista')}). "
                + (summary or "A operação foi concluída."))
        actions = ctx.actions
    else:
        note = (decision or {}).get("note") if isinstance(decision, dict) else ""
        text = ("Um analista revisou e não aprovou esta operação" + (f": {note}" if note else ".")
                + " Nada foi alterado.")
        actions = []
    timeline.append(TimelineEvent(category="agent", agent=pending["agent"], collection="pending_reviews", op="read",
                                   title="Turno retomado do checkpoint após decisão humana",
                                   filter={"review_id": pending.get("review_id")},
                                   result={"approved": bool(approved), "tool": pending["tool"]}))
    if responses:
        responses[-1] = text
    entry = blackboard.entry_for(pending["hop"], pending["agent"], "decisão humana", "human_in_the_loop", actions, text)
    await blackboard.append_entry(svc.store, turn_id=state["turn_id"], customer_key=customer["customer_key"], entry=entry)
    board = [*state["board"][:-1], {**state["board"][-1], "answer": text, "actions": entry["actions"]}] if state["board"] else [entry]
    update = {"responses": responses, "board": board, "timeline": timeline, "pending_action": None,
              "resumed": True, "next_agent": None}
    if approved and pending.get("handoff_to") and pending["handoff_to"] in state["registry"]:
        visit_counts = dict(state["visit_counts"])
        if visit_counts.get(pending["handoff_to"], 0) == 0:
            return await _record_handoff(svc, state, {**update, "tail_notes": state["tail_notes"]}, pending["agent"],
                                         pending["handoff_to"], pending.get("handoff_reason"),
                                         pending.get("task_for_next"), False, visit_counts)
    return update


def _route_after_approval(state: TurnState) -> str:
    return "advance" if state.get("next_agent") else "consolidate"


async def n_consolidate(state: TurnState, config) -> dict:
    svc = _svc(config)
    from . import blackboard, consolidate as consolidate_module
    entries = state["board"]
    response, event = await consolidate_module.consolidate(
        llm=svc.llm, budget=state["budget"], orchestrator_doc=state["registry"].get("orchestrator"),
        message=state["masked"], entries=entries, tail=state["tail_notes"])
    if not entries and not response:
        response = "\n\n".join(state["responses"] + state["tail_notes"])
    timeline = state["timeline"]
    timeline.append(event)
    await blackboard.set_status(svc.store, turn_id=state["turn_id"], customer_key=state["customer"]["customer_key"],
                                status="consolidated", extra={"final_response": response[:4000]})
    return {"final_response": response or "Não foi possível concluir o atendimento com segurança.", "timeline": timeline}


async def n_finalize(state: TurnState, config) -> dict:
    """Guardrail de saída, cascata de cache, memória, conversa e trace — o fim do caminho caro."""
    svc = _svc(config)
    customer, budget, masked = state["customer"], state["budget"], state["masked"]
    conversation_id, decision, target = state["conversation_id"], state["decision"], state["target"]
    timeline, current = state["timeline"], state["current"]
    response = state["final_response"]
    left_out = await _confirmed_out_of_scope(svc.store, out_of_scope_sentences(masked))
    if left_out and state["responses"]:
        response += "\n\n" + " ".join(f"Sobre “{sentence}”: isso foge do que eu resolvo por aqui, então segui só com o restante." for sentence in left_out)
    output_guardrail = await check_output(svc.store, response, customer)
    timeline.append(TimelineEvent(category="guardrail", title="Guardrail de saída", result={"blocked": output_guardrail.blocked}))
    if output_guardrail.blocked:
        response = "A resposta foi retida pela política de segurança."
    turn_tail = timeline[state["tail_start"]:]
    handoff_chain = state["handoff_chain"]
    cache_eligible = (
        decision.intent in GLOBAL_CACHE_INTENTS and not state["written_facts"] and not left_out
        and all(event.category != "memory" for event in turn_tail) and not handoff_chain and current == target
        # Escrita de coordenação (quadro do turno) não é efeito de negócio: não tira a resposta do cache.
        and all(event.op not in ("write", "external") for event in turn_tail if event.category != "coordination")
        and not state.get("resumed")
    )
    await cascade_store_turn(svc.store, target=target, area=customer["area"], customer_key=customer["customer_key"],
                              session_id=conversation_id, intent=decision.intent, message=masked, answer=response,
                              timeline=[event.model_dump(mode="json") for event in turn_tail], active_agent=current,
                              cache_eligible=cache_eligible)
    await cascade_store_episode(svc.store, customer_key=customer["customer_key"], intent=decision.intent, agent=current)
    timeline.append(TimelineEvent(category="memory", title="Episódio gravado em memória de longo prazo", agent=current,
                                   collection="long_term_memory", op="write", filter={"customer_key": customer["customer_key"]}, result={}))
    await svc._update_conversation(conversation_id, customer, None if state.get("resumed") else masked, response, current,
                                   handoff_chain, timeline)
    usage = {**budget.used_by_agent, "total": budget.total_used, "cache_read": budget.cache_read_tokens, "cache_write": budget.cache_write_tokens}
    await metrics.increment("tokens.total", budget.total_used)
    await metrics.increment("cache.misses")
    trace_url = await svc._persist_trace(conversation_id, customer, masked, response, timeline, current, usage,
                                          (perf_counter() - state["started"]) * 1000, llm_calls=budget.llm_calls)
    await _record_collection_metrics(timeline)
    suggestions = await _next_steps(svc.store, customer, covered={TOPIC_BY_AGENT.get(current, "")})
    output = svc._response(budget, conversation_id=conversation_id, response=response, active_agent=current,
                            route_source=state["route_source"], cache_hit=False, cache_source=None, tokens_economizados=0,
                            timeline=timeline, usage=usage, suggestions=suggestions, langfuse_trace_url=trace_url,
                            turn_id=state["turn_id"], agent_path=list(state["handoff_path"]))
    return {"output": output}


class SimulatedCrash(RuntimeError):
    """Queda injetada pela demo de retomada (`POST /api/demo/crash-resume`)."""


_GRAPH = None
_CHECKPOINT_CLIENT: SyncMongoClient | None = None


CHECKPOINT_TTL_SECONDS = 86400  # mesmo TTL de agent_conversations (database.py)


def _build_graph(settings):
    builder = StateGraph(TurnState)
    builder.add_node("ingest", n_ingest)
    builder.add_node("guardrail", n_guardrail)
    builder.add_node("blocked", n_blocked)
    builder.add_node("memory_extract", n_memory_extract)
    builder.add_node("fanout_check", n_fanout_check)
    builder.add_node("fanout", n_fanout)
    builder.add_node("decide", n_decide)
    builder.add_node("out_of_scope", n_out_of_scope)
    builder.add_node("cache_lookup", n_cache_lookup)
    builder.add_node("cache_hit", n_cache_hit)
    builder.add_node("chain_prepare", n_chain_prepare)
    builder.add_node("agent_hop", n_agent_hop)
    builder.add_node("advance", n_advance)
    builder.add_node("approval", n_approval)
    builder.add_node("consolidate", n_consolidate)
    builder.add_node("finalize", n_finalize)

    builder.set_entry_point("ingest")
    builder.add_edge("ingest", "guardrail")
    builder.add_conditional_edges("guardrail", _route_after_guardrail, {"blocked": "blocked", "memory_extract": "memory_extract"})
    builder.add_edge("memory_extract", "fanout_check")
    builder.add_conditional_edges("fanout_check", _route_after_fanout_check, {"fanout": "fanout", "decide": "decide"})
    builder.add_conditional_edges("decide", _route_after_decide, {"out_of_scope": "out_of_scope", "cache_lookup": "cache_lookup"})
    builder.add_conditional_edges("cache_lookup", _route_after_cache, {"cache_hit": "cache_hit", "handoff_chain": "chain_prepare"})
    builder.add_edge("chain_prepare", "agent_hop")
    builder.add_conditional_edges("agent_hop", _route_after_hop,
                                  {"approval": "approval", "advance": "advance", "consolidate": "consolidate"})
    builder.add_edge("advance", "agent_hop")
    builder.add_conditional_edges("approval", _route_after_approval, {"advance": "advance", "consolidate": "consolidate"})
    builder.add_edge("consolidate", "finalize")
    for terminal in ("blocked", "fanout", "out_of_scope", "cache_hit", "finalize"):
        builder.add_edge(terminal, END)

    global _CHECKPOINT_CLIENT
    if settings.demo_mode or not settings.mongodb_uri:
        # DEMO_MODE roda sem Atlas de verdade (DataStore vira o backend em memória —
        # ver app/database.py); o checkpointer segue o mesmo toggle, senão a suíte
        # offline e o CI travam tentando abrir um MongoClient contra uma URI vazia.
        checkpointer = MemorySaver()
    else:
        _CHECKPOINT_CLIENT = SyncMongoClient(settings.mongodb_uri)
        # Tipos próprios da app que acabam dentro do estado do grafo (RouteDecision,
        # CascadeResult, TurnBudget, TimelineEvent, ChatResponse) — sem isso o
        # serializador ainda funciona hoje (cai para pickle com um aviso), mas já
        # avisa que uma versão futura do LangGraph vai bloquear o que não estiver
        # nesta lista. Documentos crus do Mongo (customer/registry/conversation)
        # NÃO entram aqui: `_no_id` os limpa antes, porque ObjectId não tem esse
        # fallback — quebra o `put()` do checkpoint na hora, depois do nó já ter
        # gravado no banco de verdade (achado pelo cenário de caos `crash_resume`).
        # (módulo, classe): o LangGraph atual só aceita pares. Com nomes de módulo soltos tudo era bloqueado
        # e voltava como dict na retomada (achado ao retomar um interrupt do HITL contra o Atlas; o
        # MemorySaver do DEMO_MODE não serializa e escondia o bug).
        serde = JsonPlusSerializer(allowed_msgpack_modules=[
            ("app.router", "RouteDecision"), ("app.cascade", "CascadeResult"), ("app.budget", "TurnBudget"),
            ("app.models", "TimelineEvent"), ("app.models", "ChatResponse"), ("app.models", "Suggestion"),
        ])
        # TTL igual ao de `agent_conversations` (1 dia): sem ele os checkpoints viviam para sempre
        # depois que a conversa expirava (medido na demo: 977 checkpoints e 4.498 writes, 0 conversas).
        checkpointer = MongoDBSaver(
            _CHECKPOINT_CLIENT, db_name=settings.mongodb_db,
            checkpoint_collection_name="langgraph_checkpoints",
            writes_collection_name="langgraph_checkpoint_writes",
            serde=serde, ttl=CHECKPOINT_TTL_SECONDS,
        )
    return builder.compile(checkpointer=checkpointer)


def get_graph(settings):
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph(settings)
    return _GRAPH


def checkpoint_thread_id(customer: dict, conversation_id: str) -> str:
    return f"{customer['customer_key']}:{conversation_id}"


async def run_turn(service, message: str, customer: dict, conversation_id: str | None,
                   *, crash_before_hop: int | None = None) -> ChatResponse:
    requested_conversation_id = conversation_id
    resolved_conversation_id = requested_conversation_id or f"conv-{uuid.uuid4().hex[:12]}"
    graph = get_graph(service.store.settings)
    initial: TurnState = {
        "message": message, "customer": _no_id(customer),
        "conversation_id": resolved_conversation_id,
        "requested_conversation_id": requested_conversation_id,
    }
    # thread_id NAMESPACED pelo cliente: com `thread_id=conversation_id` puro, quem enviasse o
    # conversation_id de OUTRO cliente carregava o checkpoint dele no próprio turno e gravava um
    # checkpoint novo na thread alheia (o n_ingest troca o id da conversa, mas o checkpointer já
    # tinha resolvido a thread). Com o prefixo, id alheio cai numa thread vazia do próprio cliente.
    config = {"configurable": {"thread_id": checkpoint_thread_id(customer, resolved_conversation_id),
                               "service": service, "crash_before_hop": crash_before_hop}}
    final_state = await graph.ainvoke(initial, config=config)
    if final_state.get("__interrupt__"):
        return await _paused_output(service, final_state)
    return final_state["output"]


async def _paused_output(service, state: dict) -> ChatResponse:
    """Turno parado no nó `approval`: responde ao cliente que há uma aprovação pendente e grava a conversa.
    O estado inteiro (quadro, caminho, orçamento) fica no checkpoint até a decisão humana."""
    customer, budget, timeline = state["customer"], state["budget"], state["timeline"]
    pending = state["pending_action"]
    from .consolidate import deterministic
    response = deterministic(state["board"], state["tail_notes"])
    await service._update_conversation(state["conversation_id"], customer, state["masked"], response,
                                       state["current"], state["handoff_chain"], timeline)
    trace_url = await service._persist_trace(state["conversation_id"], customer, state["masked"], response, timeline,
                                             state["current"], service._usage(budget),
                                             (perf_counter() - state["started"]) * 1000, llm_calls=budget.llm_calls)
    await metrics.increment("hitl.paused")
    return service._response(budget, conversation_id=state["conversation_id"], response=response,
                             active_agent=state["current"], route_source=state["route_source"], cache_hit=False,
                             timeline=timeline, usage=service._usage(budget), langfuse_trace_url=trace_url,
                             turn_id=state["turn_id"], agent_path=list(state["handoff_path"]),
                             awaiting_approval={"review_id": pending.get("review_id"), "tool": pending["tool"],
                                                "args": pending["args"], "agent": pending["agent"],
                                                "reason": pending.get("reason")})


async def resume_turn(service, customer: dict, conversation_id: str, decision: dict) -> ChatResponse | None:
    """Retoma o turno pausado no checkpoint (mesmo thread) com a decisão humana."""
    from langgraph.types import Command
    graph = get_graph(service.store.settings)
    config = {"configurable": {"thread_id": checkpoint_thread_id(customer, conversation_id), "service": service}}
    snapshot = await graph.aget_state(config)
    if not snapshot.interrupts:
        return None
    final_state = await graph.ainvoke(Command(resume=decision), config=config)
    if final_state.get("__interrupt__"):
        return await _paused_output(service, final_state)
    return final_state.get("output")


async def resume_after_crash(service, customer: dict, conversation_id: str) -> ChatResponse | None:
    """Retoma do último super-step gravado (`ainvoke(None)`): hops já concluídos NÃO rodam de novo."""
    graph = get_graph(service.store.settings)
    config = {"configurable": {"thread_id": checkpoint_thread_id(customer, conversation_id), "service": service}}
    final_state = await graph.ainvoke(None, config=config)
    if final_state.get("__interrupt__"):
        return await _paused_output(service, final_state)
    return final_state.get("output")
