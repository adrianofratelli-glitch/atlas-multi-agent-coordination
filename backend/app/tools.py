"""Tools reais dos agentes especialistas (tool calling) — o LLM PROPÕE, o servidor DISPÕE.

Cada tool é um contrato tipado (nome, descrição, JSON Schema) com um executor em Python que reusa as
mesmas operações seguras dos runners determinísticos (`agents.py`). Invariantes preservados:

- O LLM nunca monta query: os executores reconstroem o filtro com o `customer_key` do JWT
  (`policies.py`); argumentos do modelo são só identificadores validados e enums fechados.
- Escrita é exceção: toda tool de escrita passa por três portões no servidor, nesta ordem —
  (1) taint de busca web no turno (ADR-005), (2) **intenção explícita na mensagem do cliente**
  (o modelo não consegue inventar uma escrita que o cliente não pediu) e (3) política de aprovação
  humana (`hitl_policies`, ADR-012), que pausa o grafo com `interrupt()` antes de escrever.
- `allowed_tools` do `agent_registry` filtra o que cada agente enxerga: desligar uma tool é um
  `update_one`, sem deploy.
- Em modo comparação (A/B), escritas são simuladas: nada muda no banco.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Awaitable, Callable

from . import observability
from .database import DataStore
from .models import TimelineEvent
from .policies import public_document
from .router import normalize


PED_RE = re.compile(r"^PED-\d{4,}$")
STATUS_VALUES = ("troca_solicitada", "reembolsado")
ESCALATION_TERMS = ("abrir chamado", "falar com humano", "escalar", "atendente", "chamado")
RESCHEDULE_TERMS = ("reagendar", "mudar a entrega", "outro dia", "adiar a entrega")


@dataclass
class ToolContext:
    store: DataStore
    customer: dict
    message: str
    conversation_id: str
    agent_key: str
    agent_doc: dict | None
    turn_context: dict
    hitl_policies: list[dict] = field(default_factory=list)
    simulate_writes: bool = False
    tainted: bool = False
    approved_action: dict | None = None   # ação já aprovada por humano (retomada do interrupt)
    hold_writes: bool = False             # há escrita aguardando aprovação neste turno: as demais esperam
    events: list[TimelineEvent] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    pending_action: dict | None = None
    touched: dict = field(default_factory=dict)  # order_id / invoice_id tocados (contexto da conversa)


@dataclass
class ToolResult:
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    agent: str
    effect: str          # "read" | "write" | "external"
    description: str
    input_schema: dict
    run: Callable[[ToolContext, dict], Awaitable[ToolResult]]

    def as_anthropic(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _order_id(args: dict, ctx: ToolContext) -> tuple[str | None, bool]:
    raw = (args.get("order_id") or "").strip().upper()
    if raw:
        if not PED_RE.match(raw):
            return None, True
        return raw, True
    return ctx.turn_context.get("active_order_id"), False


# Pedido x pergunta (revisões de 2026-11-12). Por cláusula (frases cortadas em . ; ! ? e cláusulas na vírgula):
# - pergunta de informação ("como faço para trocar?"), condicional/hipótese ("se eu trocar, perco a
#   garantia?", "caso eu peça reembolso, ...") e negação ("não quero trocar", "troca não") NÃO são pedido;
# - a última cláusula de uma frase terminada em "?" só é pedido se começar como pedido ("pode trocar?",
#   "dá pra reagendar?", "quero trocar o PED-1001?"); "quero entender, a troca é gratuita?" não é.
_QUESTION_START = re.compile(r"^(e )?(como|quanto|quantos|quanta|qual|quais|quando|onde|por que|porque|o que|"
                             r"sera que|existe|existem)\b")
_CONDITIONAL_START = re.compile(r"^(e )?(se|caso|supondo|imagina|imagine|na hipotese)\b")
_REQUEST_START = re.compile(r"^(por favor )?(voce |voces )?(pode|podem|poderia|poderiam|consegue|conseguem|"
                            r"da pra|da para|quero|queria|gostaria|preciso|solicito|favor)\b")
_ACTION_TERM = r"(tro(c|qu)\w*|reembols\w*|estorn\w*|resgat\w*|reagend\w*|abr(ir|a|e)\b|chamado|atendente)"
# Não é pedido: negação, cancelamento/desistência, relato do passado ou vontade de SABER, até ~25 caracteres
# antes do termo na mesma cláusula ("não troque", "quero cancelar a troca", "já pedi a troca", "quero saber
# se posso trocar"), ou negação logo depois dele ("troca não").
_NEGATED = re.compile(
    r"\b(nao|nunca|jamais|sem|nem)\b[^,]{0,25}?\b" + _ACTION_TERM
    + r"|\b(cancel\w*|desist\w*|desfaz\w*|anul\w*|suspend\w*)\b[^,]{0,25}?\b" + _ACTION_TERM
    + r"|\b(pedi|solicitei|fiz|abri|pedimos|solicitamos|ja tinha)\b[^,]{0,25}?\b" + _ACTION_TERM
    + r"|\b(saber|entender|pensando|pensei|duvida|interesse|curiosidade|perguntar)\b[^,]{0,25}?\b" + _ACTION_TERM
    + r"|\b(troca|reembolso|estorno|resgate|reagendamento|chamado|atendente)\s*,?\s*(nao|nunca)\b"
    # relato/estado do que já aconteceu ("o reembolso ainda não caiu", "a troca chegou quebrada")
    + r"|\b(troca|reembolso|estorno|resgate|reagendamento|chamado)\b[^,]{0,25}?\b(chegou|caiu|apareceu|demorou|"
      r"atrasou|que pedi|que solicitei|ja foi|foi feit[ao])\b"
    + r"|^(obrigad\w*|valeu|recebi|entendi|ok)\b")
# A janela de negação/relato termina num pedido explícito: "não funciona quero trocar" é pedido.
_REQUEST_BREAK = re.compile(r"(?<!nao )(?<!nunca )(?<!nem )\b(quero|preciso|gostaria|solicito)\b")
_POINTS_SWAP = re.compile(r"troc\w*\s+(os\s+|meus\s+|minhas\s+)?pontos")


def _clauses(message: str) -> list[tuple[str, bool]]:
    """(cláusula normalizada, NÃO é pedido?)."""
    out = []
    for sentence in re.findall(r"[^.;!?\n]+[.;!?]*", message.strip()):
        is_question = sentence.strip().endswith("?")
        parts = [normalize(part).strip(" ,.;!?") for part in sentence.split(",")]
        parts = [part for part in parts if part]
        for index, norm in enumerate(parts):
            tail = _REQUEST_BREAK.split(norm)[-1] if _REQUEST_BREAK.search(norm) else norm
            negated = _NEGATED.search(norm) and _NEGATED.search(tail) if tail != norm else _NEGATED.search(norm)
            not_request = bool(_QUESTION_START.match(norm) or _CONDITIONAL_START.match(norm) or negated)
            if is_question and index == len(parts) - 1 and not _REQUEST_START.match(norm):
                not_request = True
            out.append((norm, not_request))
    return out


def _wants(message: str, terms) -> bool:
    """Pedido de AÇÃO: algum termo numa cláusula que não é pergunta."""
    return any(not question and any(term in clause for term in terms) for clause, question in _clauses(message))


def _wants_status(message: str, status: str) -> bool:
    from .agents import _requested_status
    for clause, question in _clauses(message):
        if question or _POINTS_SWAP.search(clause):   # "trocar meus pontos" é fidelidade, não troca de pedido
            continue
        if _requested_status(clause) == status:
            return True
    return False


def requested_status(message: str) -> str | None:
    """Status pedido de verdade (1ª cláusula que é pedido): "troca não, quero reembolso" → reembolsado."""
    from .agents import _requested_status
    for clause, not_request in _clauses(message):
        if not not_request and not _POINTS_SWAP.search(clause):
            status = _requested_status(clause)
            if status:
                return status
    return None


def _cited_orders(message: str) -> set[str]:
    return set(re.findall(r"\bPED-\d{4,}\b", message.upper()))


# ----------------------------------------------------------------------------------------- reads

async def _get_order(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import load_order
    order_id, explicit = _order_id(args, ctx)
    if explicit and order_id is None:
        return ToolResult({"erro": "order_id inválido; formato PED-0000"}, True)
    started = perf_counter()
    clean, query = await load_order(ctx.store, ctx.customer, order_id, explicit)
    ctx.events.append(TimelineEvent(category="agent", title="Tool get_order — consulta segura de pedido",
                                    agent="order_agent", collection="orders", op="read", filter=query,
                                    result=clean or [], duration_ms=(perf_counter() - started) * 1000))
    if not clean:
        return ToolResult({"encontrado": False, "motivo": "nenhum pedido do cliente autenticado com esse id"})
    ctx.touched["order_id"] = clean["order_id"]
    return ToolResult(clean)


async def _get_invoice(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import run_billing_agent
    raw = (args.get("invoice_id") or "").strip().upper()
    if raw and not re.match(r"^FAT-\d{4,}$", raw):
        return ToolResult({"erro": "invoice_id inválido; formato FAT-0000"}, True)
    message = raw or ctx.message
    result = await run_billing_agent(ctx.store, message, ctx.customer, None, None, ctx.agent_doc, "",
                                     {**ctx.turn_context})
    result.event.title = "Tool get_invoice — leitura isolada de fatura"
    ctx.events.append(result.event)
    doc = result.event.result
    if not doc:
        return ToolResult({"encontrado": False, "orientacao": result.response})
    ctx.touched["invoice_id"] = doc.get("invoice_id")
    return ToolResult(doc)


async def _check_warranty(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import run_warranty_agent
    order_id, explicit = _order_id(args, ctx)
    if explicit and order_id is None:
        return ToolResult({"erro": "order_id inválido; formato PED-0000"}, True)
    # Mensagem real do cliente: o portão da revisão de qualidade (`wants_replacement`) olha o que ELE pediu.
    # No A/B (escritas simuladas) a consulta não pode abrir revisão de qualidade: só a cobertura.
    message = f"garantia {order_id or ''}".strip() if ctx.simulate_writes else ctx.message
    result = await run_warranty_agent(ctx.store, message, ctx.customer, None, None, ctx.agent_doc, "",
                                      {**ctx.turn_context, "active_order_id": order_id or ctx.turn_context.get("active_order_id")})
    result.event.title = "Tool check_warranty — cobertura calculada + $graphLookup de reposições"
    ctx.events.append(result.event)
    ctx.events.extend(result.extra_events)
    doc = result.event.result or {}
    if isinstance(doc, dict) and doc.get("order_id"):
        ctx.touched["order_id"] = doc["order_id"]
    return ToolResult(doc or {"encontrado": False, "orientacao": result.response})


async def _get_loyalty(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import REWARD_CATALOG
    started = perf_counter()
    query = {"customer_key": ctx.customer["customer_key"]}
    clean = public_document(await ctx.store.find_one("loyalty_accounts", query))
    catalog = [{"reward": key, "label": label, "points": cost} for key, (label, cost) in REWARD_CATALOG.items()]
    ctx.events.append(TimelineEvent(category="agent", title="Tool get_loyalty_account — saldo e tier",
                                    agent="loyalty_agent", collection="loyalty_accounts", op="read",
                                    filter=query, result={**clean, "tabela_de_recompensas": catalog} if clean else [],
                                    duration_ms=(perf_counter() - started) * 1000))
    if not clean:
        return ToolResult({"encontrado": False})
    return ToolResult({**clean, "tabela_de_recompensas": catalog})


async def _get_shipment(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import load_shipment
    order_id, explicit = _order_id(args, ctx)
    if explicit and order_id is None:
        return ToolResult({"erro": "order_id inválido; formato PED-0000"}, True)
    started = perf_counter()
    clean, query = await load_shipment(ctx.store, ctx.customer, order_id, explicit)
    ctx.events.append(TimelineEvent(category="agent", title="Tool get_shipment — rastreio e prazo (conta em Python)",
                                    agent="logistics_agent", collection="shipments", op="read", filter=query,
                                    result=clean or [], duration_ms=(perf_counter() - started) * 1000))
    if not clean:
        return ToolResult({"encontrado": False})
    ctx.touched["order_id"] = clean.get("order_id")
    return ToolResult(clean)


async def _search_catalog(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import run_product_agent
    query = (args.get("query") or "").strip()[:300] or ctx.message
    # A busca (pré-filtro de orçamento da memória, $vectorSearch, $rerank) roda no servidor; o modelo só
    # escolhe o TEXTO da busca. Orçamento ativo do cliente segue limite duro (invariante da memória).
    result = await run_product_agent(ctx.store, query, ctx.customer, None, None, ctx.agent_doc, "", ctx.turn_context)
    result.event.title = "Tool search_catalog — $vectorSearch ponderado (relevância + nota + estoque)"
    ctx.events.append(result.event)
    ctx.events.extend(result.extra_events)
    return ToolResult({"produtos": result.event.result or [], "consulta": query})


async def _search_kb(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import _SCORE_FIELDS, _kb_events, _local_rank
    from .retrieval import search_kb_explained
    query = (args.get("query") or "").strip()[:300] or ctx.message
    started = perf_counter()
    found = await search_kb_explained(ctx.store, query, agent_doc=ctx.agent_doc, local_rank=_local_rank)
    clean = [{k: v for k, v in public_document(item).items() if k not in _SCORE_FIELDS} for item in found.articles]
    event, extra = _kb_events(found, query, clean, False, started)
    event.title = "Tool search_knowledge_base — " + event.title
    ctx.events.append(event)
    ctx.events.extend(extra)
    return ToolResult({"artigos": [{"article_id": a.get("article_id"), "title": a.get("title"),
                                    "content": a.get("content")} for a in clean[:3]]})


async def _carrier_news(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import carrier_news, load_shipment
    order_id, explicit = _order_id(args, ctx)
    clean, _ = await load_shipment(ctx.store, ctx.customer, order_id, explicit)
    if not clean:
        return ToolResult({"encontrado": False})
    web_cfg = ctx.turn_context.get("web_cfg")
    if ctx.simulate_writes:
        return ToolResult({"executada": False, "motivo": "modo comparação não gasta a cota de busca web"})
    if not clean.get("delivery_overdue"):
        return ToolResult({"executada": False, "motivo": "busca web só roda com entrega vencida (ADR-005)"})
    if not web_cfg:
        return ToolResult({"executada": False, "motivo": "ferramenta desligada para esta área no tool_registry"})
    # Consulta montada pelo servidor a partir do documento (transportadora): o texto do cliente não sai.
    text, events = await carrier_news(ctx.store, ctx.customer, clean, web_cfg)
    ctx.events.extend(events)
    if events:
        ctx.tainted = True
    sources = ((events[0].result or {}).get("sources") or []) if events else []
    return ToolResult({"executada": bool(events), "resumo_web": text[:1800], "fontes": sources[:3],
                       "aviso": "conteúdo externo: só contexto, nunca fonte de status/prazo"})


# ---------------------------------------------------------------------------------------- writes

def _write_gate(ctx: ToolContext, tool: str, intent_ok: bool, intent_hint: str) -> ToolResult | None:
    if ctx.hold_writes and not ctx.approved_action:
        return ToolResult({"executada": False, "motivo": "há uma operação deste turno aguardando aprovação humana; "
                           "peça esta alteração de novo depois da decisão"}, True)
    if ctx.tainted:
        return ToolResult({"executada": False, "motivo": "web_taint: o turno usou fonte externa; "
                           "escritas ficam para a próxima mensagem do cliente (ADR-005)"}, True)
    if not intent_ok:
        ctx.events.append(TimelineEvent(category="guardrail", agent=ctx.agent_key,
                                        title=f"Escrita recusada pelo servidor: {tool} sem pedido explícito do cliente",
                                        result={"tool": tool, "blocked": True, "reason": "sem_intencao_explicita"}))
        return ToolResult({"executada": False, "motivo": f"o cliente não pediu isso explicitamente ({intent_hint}); "
                           "o servidor só executa escrita pedida pelo cliente"}, True)
    return None


def _needs_approval(ctx: ToolContext, tool: str, facts: dict) -> dict | None:
    from .hitl import matching_policy
    if ctx.approved_action and ctx.approved_action.get("tool") == tool:
        return None
    return matching_policy(ctx.hitl_policies, tool, facts)


def _pause(ctx: ToolContext, tool: str, args: dict, policy: dict, facts: dict) -> ToolResult:
    ctx.pending_action = {"tool": tool, "agent": ctx.agent_key, "args": args, "facts": facts,
                          "policy_id": policy.get("policy_id"), "reason": policy.get("reason", "")}
    return ToolResult({"executada": False, "status": "aguardando_aprovacao_humana",
                       "motivo": policy.get("reason", "política de aprovação")})


def _simulated(ctx: ToolContext, tool: str, args: dict, collection: str) -> ToolResult:
    ctx.events.append(TimelineEvent(category="agent", agent=ctx.agent_key, collection=collection, op="read",
                                    title=f"Tool {tool} — escrita SIMULADA (modo comparação, nada gravado)",
                                    result={"tool": tool, "args": args, "simulated": True}))
    ctx.actions.append({"tool": tool, "args": args, "effect": "write", "simulated": True})
    return ToolResult({"executada": True, "simulada": True})


async def _request_status_change(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import change_order_status, load_order
    status = args.get("status")
    if status not in STATUS_VALUES:
        return ToolResult({"erro": f"status deve ser um de {STATUS_VALUES}"}, True)
    gate = _write_gate(ctx, "request_order_status_change", _wants_status(ctx.message, status), "troca ou reembolso")
    if gate:
        return gate
    order_id, explicit = _order_id(args, ctx)
    if explicit and order_id is None:
        return ToolResult({"erro": "order_id inválido; formato PED-0000"}, True)
    cited = _cited_orders(ctx.message)
    if cited and (order_id or "") not in cited:
        # O alvo da escrita é o pedido que o CLIENTE citou, nunca outro escolhido pelo modelo.
        return ToolResult({"executada": False, "motivo": f"o cliente citou {sorted(cited)}; escrita só nesse(s) pedido(s)"}, True)
    clean, _ = await load_order(ctx.store, ctx.customer, order_id, explicit)
    if not clean:
        return ToolResult({"executada": False, "motivo": "pedido não encontrado para o cliente autenticado"}, True)
    facts = {"status": status, "order_id": clean["order_id"], "amount": clean.get("amount") or clean.get("price")}
    policy = _needs_approval(ctx, "request_order_status_change", facts)
    if policy:
        return _pause(ctx, "request_order_status_change", {**args, "order_id": clean["order_id"]}, policy, facts)
    if ctx.simulate_writes:
        return _simulated(ctx, "request_order_status_change", args, "orders")
    response, event, extra, blocked, changed = await change_order_status(
        ctx.store, ctx.customer, clean, status, ctx.conversation_id, perf_counter())
    event.title = "Tool request_order_status_change — " + event.title
    ctx.events.append(event)
    ctx.events.extend(extra)
    ctx.touched["order_id"] = clean["order_id"]
    ctx.actions.append({"tool": "request_order_status_change", "args": {"order_id": clean["order_id"], "status": status},
                        "effect": "write", "changed": changed, "blocked_by_recurrence": blocked, "summary": response})
    return ToolResult({"executada": not blocked, "alterou": changed, "bloqueado_por_recorrencia": blocked,
                       "status_atual": clean.get("status"), "resumo": response})


async def _redeem_reward(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import REWARD_CATALOG, redeem_reward
    reward = args.get("reward")
    if reward not in REWARD_CATALOG:
        return ToolResult({"erro": f"reward deve ser um de {sorted(REWARD_CATALOG)}"}, True)
    gate = _write_gate(ctx, "redeem_reward", _wants(ctx.message, (reward,)), f"resgate de '{reward}'")
    if gate:
        return gate
    query = {"customer_key": ctx.customer["customer_key"]}
    clean = public_document(await ctx.store.find_one("loyalty_accounts", query))
    if not clean:
        return ToolResult({"executada": False, "motivo": "sem conta de fidelidade"}, True)
    label, cost = REWARD_CATALOG[reward]
    facts = {"reward": reward, "points": cost, "balance": clean["points"]}
    policy = _needs_approval(ctx, "redeem_reward", facts)
    if policy:
        return _pause(ctx, "redeem_reward", args, policy, facts)
    if ctx.simulate_writes:
        return _simulated(ctx, "redeem_reward", args, "loyalty_accounts")
    response, event = await redeem_reward(ctx.store, ctx.customer, clean, query, reward, ctx.conversation_id, perf_counter())
    event.title = "Tool redeem_reward — " + event.title
    ctx.events.append(event)
    ctx.actions.append({"tool": "redeem_reward", "args": args, "effect": "write", "summary": response})
    return ToolResult({"resumo": response})


async def _request_reschedule(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import load_shipment, request_reschedule
    gate = _write_gate(ctx, "request_delivery_reschedule", _wants(ctx.message, RESCHEDULE_TERMS), "reagendamento")
    if gate:
        return gate
    order_id, explicit = _order_id(args, ctx)
    cited = _cited_orders(ctx.message)
    if cited and (order_id or "") not in cited:
        return ToolResult({"executada": False, "motivo": f"o cliente citou {sorted(cited)}; escrita só nesse(s) pedido(s)"}, True)
    clean, query = await load_shipment(ctx.store, ctx.customer, order_id, explicit)
    if not clean or clean.get("current_location") == "Entregue":
        return ToolResult({"executada": False, "motivo": "entrega inexistente ou já entregue"}, True)
    facts = {"order_id": clean.get("order_id")}
    policy = _needs_approval(ctx, "request_delivery_reschedule", facts)
    if policy:
        return _pause(ctx, "request_delivery_reschedule", args, policy, facts)
    if ctx.simulate_writes:
        return _simulated(ctx, "request_delivery_reschedule", args, "shipments")
    response, event = await request_reschedule(ctx.store, ctx.customer, clean, query, ctx.conversation_id, perf_counter())
    event.title = "Tool request_delivery_reschedule — " + event.title
    ctx.events.append(event)
    ctx.actions.append({"tool": "request_delivery_reschedule", "args": {"order_id": clean.get("order_id")},
                        "effect": "write", "summary": response})
    return ToolResult({"resumo": response})


async def _open_ticket(ctx: ToolContext, args: dict) -> ToolResult:
    from .agents import open_support_ticket
    gate = _write_gate(ctx, "open_support_ticket", _wants(ctx.message, ESCALATION_TERMS), "atendente/chamado")
    if gate:
        return gate
    facts = {}
    policy = _needs_approval(ctx, "open_support_ticket", facts)
    if policy:
        return _pause(ctx, "open_support_ticket", args, policy, facts)
    if ctx.simulate_writes:
        return _simulated(ctx, "open_support_ticket", args, "support_tickets")
    summary = (args.get("summary") or ctx.message)[:200]
    existing, ticket = await open_support_ticket(ctx.store, ctx.customer, summary, ctx.conversation_id, perf_counter())
    doc = public_document(existing or ticket)
    ctx.events.append(TimelineEvent(category="agent", agent="support_agent", collection="support_tickets",
                                    op="read" if existing else "write",
                                    title="Tool open_support_ticket — " + ("chamado já aberto (idempotência)" if existing else "chamado aberto"),
                                    result=doc))
    ctx.actions.append({"tool": "open_support_ticket", "effect": "write", "ticket_id": ticket["ticket_id"],
                        "already_open": bool(existing)})
    return ToolResult({"ticket_id": ticket["ticket_id"], "ja_existia": bool(existing)})


_ORDER_ARG = {"order_id": {"type": "string", "description": "Identificador PED-0000; omita para usar o pedido ativo da conversa"}}

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    ToolSpec("get_order", "order_agent", "read",
             "Lê um pedido do cliente autenticado (status, produto, timeline). O filtro de dono é aplicado pelo servidor.",
             {"type": "object", "properties": _ORDER_ARG}, _get_order),
    ToolSpec("request_order_status_change", "order_agent", "write",
             "Efetiva troca (troca_solicitada) ou reembolso (reembolsado) de um pedido. Só use se o cliente pediu "
             "isso explicitamente; o servidor recusa caso contrário e pode exigir aprovação humana.",
             {"type": "object", "properties": {**_ORDER_ARG, "status": {"type": "string", "enum": list(STATUS_VALUES)}},
              "required": ["status"]}, _request_status_change),
    ToolSpec("get_invoice", "billing_agent", "read",
             "Lê a fatura do cliente (valor, vencimento, status). Somente leitura: nunca conceda desconto.",
             {"type": "object", "properties": {"invoice_id": {"type": "string", "description": "FAT-0000; omita para a fatura ativa/mais recente"}}},
             _get_invoice),
    ToolSpec("check_warranty", "warranty_agent", "read",
             "Calcula a cobertura de garantia do pedido (prazo calculado no servidor) e a cadeia de reposições ($graphLookup).",
             {"type": "object", "properties": _ORDER_ARG}, _check_warranty),
    ToolSpec("get_loyalty_account", "loyalty_agent", "read",
             "Lê saldo de pontos, tier, benefícios e a tabela fechada de recompensas.",
             {"type": "object", "properties": {}}, _get_loyalty),
    ToolSpec("redeem_reward", "loyalty_agent", "write",
             "Resgata uma recompensa da tabela fechada (débito atômico de pontos). Só se o cliente pediu esse resgate.",
             {"type": "object", "properties": {"reward": {"type": "string", "enum": ["frete gratis", "cupom de desconto", "voucher"]}},
              "required": ["reward"]}, _redeem_reward),
    ToolSpec("get_shipment", "logistics_agent", "read",
             "Lê transportadora, rastreio, localização e previsão; informa se a entrega está vencida (conta no servidor).",
             {"type": "object", "properties": _ORDER_ARG}, _get_shipment),
    ToolSpec("request_delivery_reschedule", "logistics_agent", "write",
             "Registra pedido de reagendamento da entrega (só a sinalização). Só se o cliente pediu reagendar.",
             {"type": "object", "properties": _ORDER_ARG}, _request_reschedule),
    ToolSpec("search_carrier_news", "logistics_agent", "external",
             "Busca notícias públicas de atraso da transportadora. O servidor só executa com entrega vencida e "
             "ferramenta ligada; depois disso nenhuma escrita roda neste turno.",
             {"type": "object", "properties": _ORDER_ARG}, _carrier_news),
    ToolSpec("search_catalog", "product_agent", "read",
             "Busca semântica no catálogo ($vectorSearch + $rerank). Descreva o que procurar, com faixa de preço se houver "
             "(ex.: 'fone de ouvido até R$ 300').",
             {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, _search_catalog),
    ToolSpec("search_knowledge_base", "support_agent", "read",
             "Busca híbrida na base de conhecimento ($rankFusion BM25 + vetor) para diagnosticar um problema.",
             {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, _search_kb),
    ToolSpec("open_support_ticket", "support_agent", "write",
             "Abre chamado para atendimento humano. Só se o cliente pediu atendente/chamado explicitamente.",
             {"type": "object", "properties": {"summary": {"type": "string"}}}, _open_ticket),
]}

# `allowed_tools` antigos do seed (antes do tool calling) mapeiam para as tools reais.
LEGACY_TOOL_ALIASES = {
    "read_order": ["get_order"], "update_order_status": ["request_order_status_change"],
    "vector_search_products": ["search_catalog"], "hybrid_search_kb": ["search_knowledge_base", "open_support_ticket"],
    "read_invoice": ["get_invoice"], "read_warranty_policy": ["check_warranty"],
    "read_loyalty_account": ["get_loyalty_account", "redeem_reward"],
    "read_shipment": ["get_shipment", "request_delivery_reschedule", "search_carrier_news"],
}


def tools_for(agent_doc: dict | None, agent_key: str) -> list[ToolSpec]:
    """Tools que o registry libera para o agente. Sem `allowed_tools` no documento: todas as dele."""
    if (agent_doc or {}).get("all_tools"):
        # Agente generalista do A/B (single-agent): enxerga todas as tools, com os mesmos portões.
        return list(TOOLS.values())
    own = [spec for spec in TOOLS.values() if spec.agent == agent_key]
    allowed = (agent_doc or {}).get("allowed_tools")
    if allowed is None:
        return own
    names: set[str] = set()
    for item in allowed:
        names.update(LEGACY_TOOL_ALIASES.get(item, [item]))
    return [spec for spec in own if spec.name in names]


async def execute(ctx: ToolContext, name: str, args: dict, call_id: str = "") -> ToolResult:
    spec = TOOLS.get(name)
    if spec is None:
        return ToolResult({"erro": f"tool desconhecida: {name}"}, True)
    # Só as chaves do JSON Schema da tool passam (o modelo não injeta campo extra no executor nem no quadro).
    allowed_keys = set((spec.input_schema.get("properties") or {}).keys())
    args = {key: value for key, value in (args or {}).items() if key in allowed_keys}
    with observability.span(f"execute_tool {name}", **{
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.type": "function",
            "gen_ai.tool.call.id": call_id, "gen_ai.agent.name": ctx.agent_key}):
        try:
            result = await spec.run(ctx, args or {})
        except Exception as exc:  # noqa: BLE001 — falha de tool vira observação de erro para o modelo
            from . import metrics as metrics_module
            await metrics_module.metrics.increment(f"tool.{name}.failures")
            return ToolResult({"erro": f"falha ao executar {name}: {type(exc).__name__}"}, True)
    if spec.effect == "external" and not result.is_error:
        ctx.actions.append({"tool": name, "effect": "external"})
    if spec.effect == "read" and not result.is_error:
        import json as _json
        # Fatos lidos do BANCO (nunca conteúdo web, ADR-005) vão para a consolidação; o próximo agente
        # vê só o nome da tool (`blackboard.summary_for_prompt`).
        ctx.actions.append({"tool": name, "effect": "read",
                            "facts": _json.dumps(result.content, ensure_ascii=False, default=str)[:1500]})
    return result


def explicit_write_intents(message: str, tool_names: list[str]) -> list[str]:
    """Os MESMOS sinais que os portões de escrita usam, ditos ao modelo antes: o servidor sabe que o cliente
    pediu "troca"; o agente não pode esquecer de efetivar (medido: às vezes só consultava e transferia)."""
    from .agents import REWARD_CATALOG
    found = []
    status = requested_status(message)
    if status and "request_order_status_change" in tool_names:
        found.append(f"request_order_status_change com status '{status}'")
    reward = next((key for key in REWARD_CATALOG if _wants(message, (key,))), None)
    if reward and "redeem_reward" in tool_names:
        found.append(f"redeem_reward '{reward}'")
    if _wants(message, RESCHEDULE_TERMS) and "request_delivery_reschedule" in tool_names:
        found.append("request_delivery_reschedule")
    if _wants(message, ESCALATION_TERMS) and "open_support_ticket" in tool_names:
        found.append("open_support_ticket")
    return found
