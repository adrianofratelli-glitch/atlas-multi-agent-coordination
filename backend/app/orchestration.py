import asyncio
from time import perf_counter

from .agents import RUNNERS
from .economics import summarize_calls
from .budget import TurnBudget, estimate_tokens
from .cascade import (cascade_lookup, cascade_store_turn)
from .database import DataStore, utcnow
from . import observability, resilience, scope_classifier  # noqa: F401 — reexportado p/ monkeypatch nos testes
from .guardrails import check_output
from .langfuse_client import build_turn_trace
from .llm import LLMGateway
from .metrics import metrics
from .models import ChatResponse, TimelineEvent
from .router import (deterministic_orchestrator, has_domain_signal)
from .guidance import (build_suggestions, customer_snapshot, is_capabilities_question, is_greeting, is_meta_question, is_thanks)


ROUTER_PROMPT = (
    "Classifique a intenção do cliente e escolha o agente certo. Responda com UMA linha, só a chave, sem explicação.\n"
    "order_agent: status, rastreio, cancelamento, troca, devolução ou reembolso de um PEDIDO já feito, e pedido para ver o "
    "histórico de compras ou os dados cadastrais do PRÓPRIO cliente.\n"
    "product_agent: recomendação/comparação de PRODUTOS do catálogo (fones, monitores, teclados, mouses, carregadores, "
    "smartwatches etc.), preço e disponibilidade, mesmo sem usar a palavra 'produto'.\n"
    "support_agent: problema técnico/defeito num produto que o cliente TEM (de qualquer tipo, mesmo que a loja não o venda), "
    "e pedido para falar com atendente humano ou abrir chamado.\n"
    "billing_agent: fatura, cobrança, valor a pagar, vencimento, nota fiscal, contestação de cobrança.\n"
    "warranty_agent: garantia — se um produto está coberto, prazo, o que a garantia cobre.\n"
    "loyalty_agent: pontos, nível de fidelidade, resgate de pontos.\n"
    "logistics_agent: transportadora, previsão de entrega, código de rastreamento, reagendar entrega.\n"
    "conversa: SOMENTE cumprimento, agradecimento, despedida ou pergunta sobre quem é o assistente e o que ele faz. Pedido de poema, "
    "piada, receita, ou informação da loja (CNPJ, endereço, horário) NÃO é conversa: é 'nenhum'.\n"
    "nenhum: a mensagem não é sobre atendimento desta loja (assunto aleatório, teste, texto sem sentido) — responda exatamente "
    "'nenhum' nesse caso, NUNCA escolha um agente por eliminação.\n"
    "Chaves permitidas: "
)


def _supervisor_state(conversation_id: str) -> dict:
    """Estado do supervisor para ESTE turno: teto de passos, timeout e detector de loop.

    Degradação graciosa é o padrão; `SUPERVISOR_LEGACY_500=1` devolve o comportamento antigo.
    """
    return {"graceful": resilience.graceful_degradation(),
            "guard": resilience.LoopGuard(),
            "timeout": resilience.agent_timeout_seconds(),
            "conversation_id": conversation_id}


def reaches_scope_classifier(message: str) -> bool:
    """A mensagem chega ao classificador de escopo? Só quando NADA mais decidiu: sem palavra forte, sem rota determinística e sem
    ser saudação/agradecimento/meta óbvios. A calibração mede exatamente esta população — não itens que nunca chegariam a ele."""
    return (not has_domain_signal(message) and deterministic_orchestrator(message).source == "fallback"
            and not (is_greeting(message) or is_thanks(message) or is_meta_question(message) or is_capabilities_question(message)))


async def _record_collection_metrics(timeline: list[TimelineEvent]) -> None:
    """Contador cumulativo de toque por collection+operação — alimenta o painel 'Coleções' no front,
    prova visual de que MongoDB é o único data store por trás de leitura, escrita, vector e hybrid search."""
    for event in timeline:
        if event.collection and event.op and not event.replayed:
            await metrics.increment(f"collection.{event.collection}.{event.op}")


MAX_HOPS = 5
# Retornos só existem quando representam uma dependência de negócio explícita. O limite por agente
# mantém o grafo finito mesmo se uma configuração dinâmica introduzir um ciclo acidental.
ALLOWED_REVISITS = {("logistics_agent", "order_agent")}
MAX_VISITS_PER_AGENT = 2
FALLBACK_AGENTS = {
    "product_agent": "support_agent",
    "support_agent": "order_agent",
    "billing_agent": "order_agent",
    "order_agent": "support_agent",
    "warranty_agent": "support_agent",
    "loyalty_agent": "order_agent",
    "logistics_agent": "order_agent",
}


# Agentes cujo runner pode escrever (resgate, mudança de status, reagendamento, chamado). O registry
# inteiro é montado uma única vez no início de run_turn; se um destes for desativado via
# PATCH /api/admin/agents enquanto um turno com handoffs em cadeia já está em andamento, a versão em
# memória carregada no início não vê a mudança. Para estes agentes, uma checagem extra direto no
# banco roda imediatamente antes de cada runner() no loop de handoff — os agentes só de leitura
# seguem confiando no registry do início do turno, que é onde o custo da consulta extra compensa.
WRITE_EFFECT_AGENTS = {"loyalty_agent", "order_agent", "logistics_agent", "support_agent"}


TOPIC_BY_AGENT = {
    # Tema que cada agente já cobriu no turno: sugerir de volta exatamente o que o
    # cliente acabou de perguntar é pior que não sugerir nada.
    "order_agent": "order",
    "billing_agent": "invoice",
    "logistics_agent": "shipment",
    "loyalty_agent": "loyalty",
    "product_agent": "product",
    "warranty_agent": "order",
}


async def _next_steps(store, customer: dict, *, covered: set[str]) -> list[dict]:
    """Próximos passos do turno, sempre ancorados em documento existente.

    Falha aqui nunca derruba a resposta que já foi produzida — a lista some e o
    turno segue normal.
    """
    try:
        snapshot = await customer_snapshot(store, customer)
        return build_suggestions(snapshot, exclude=covered)
    except Exception:  # noqa: BLE001 — enfeite útil, nunca caminho crítico
        return []


class OrchestrationService:
    def __init__(self, store: DataStore, llm: LLMGateway, global_budget: int):
        self.store = store
        self.llm = llm
        self.global_budget = global_budget

    async def _route_with_llm(self, message: str, orchestrator: dict, allowed: list[str], budget) -> str | None:
        """Uma linha: a chave do agente, `conversa` ou `nenhum`. Classificar é decisão, não criação: temperature 0."""
        with observability.span("routing", **{"routing.candidates": len(allowed)}) as current:
            text, _ = await self.llm.complete(
                agent={**orchestrator, "temperature": 0}, user_message=message,
                dynamic_context=ROUTER_PROMPT + ", ".join(allowed) + ", conversa, nenhum", budget=budget)
            current.set_attribute("routing.decision", (text or "").strip().splitlines()[0][:40] if text else "sem_resposta")
        return text

    @staticmethod
    def _usage(budget):
        return {**budget.used_by_agent, "total": budget.total_used,
                "cache_read": budget.cache_read_tokens, "cache_write": budget.cache_write_tokens}

    @staticmethod
    def _response(budget, **kwargs):
        return ChatResponse(**kwargs, llm_calls=budget.llm_calls, economics=summarize_calls(budget.llm_calls))

    async def run_turn(self, message: str, customer: dict, conversation_id: str | None) -> ChatResponse:
        """Orquestração do turno completo — implementada como StateGraph do LangGraph
        em `orchestration_graph.py` (checkpoint nativo, `thread_id=<customer_key>:<conversation_id>`).
        Este método só delega; import local evita ciclo (orchestration_graph importa
        este módulo para reaproveitar constantes/funções compartilhadas)."""
        from . import orchestration_graph
        return await orchestration_graph.run_turn(self, message, customer, conversation_id)


    async def _run_fanout(self, targets: list[str], masked: str, customer: dict, registry: dict, budget: TurnBudget, conversation_id: str, conversation: dict | None, timeline: list[TimelineEvent], started: float) -> ChatResponse:
        """Pattern Parallel Fan-Out/Synthesis: agentes independentes rodam ao mesmo tempo (asyncio.gather), não
        em cadeia — cobre pedidos compostos tipo 'status do pedido e quanto devo' sem pagar 2 turnos de latência."""
        fanout_key = "+".join(targets)
        cascade = await cascade_lookup(self.store, target=fanout_key, area=customer["area"], customer_key=customer["customer_key"], session_id=conversation_id, message=masked)
        if cascade.hit:
            await metrics.increment(f"cache.hits.{cascade.fonte}")
            timeline.append(TimelineEvent(category="cache", title=f"Cascata semântica: HIT ({cascade.fonte})", collection="short_term_memory" if cascade.fonte == "curto_prazo" else "semantic_cache", op="vectorSearch", filter={"session_id": conversation_id, "agent": fanout_key}, result={"hit": True, "fonte": cascade.fonte, "score": cascade.score, "classifier_score": (cascade.classifier or {}).get("score")}))
            response = cascade.answer or ""
            cached_active_agent = cascade.active_agent or fanout_key
            cached_timeline = timeline + [TimelineEvent(**{**event, "replayed": True}) for event in cascade.timeline]
            await self._update_conversation(conversation_id, customer, masked, response, cached_active_agent, [], cached_timeline)
            trace_url = await self._persist_trace(conversation_id, customer, masked, response, cached_timeline, cached_active_agent, self._usage(budget), (perf_counter() - started) * 1000, llm_calls=budget.llm_calls)
            await _record_collection_metrics(cached_timeline)
            return self._response(budget, conversation_id=conversation_id, response=response, active_agent=cached_active_agent, route_source="fanout", cache_hit=True, cache_source=cascade.fonte, tokens_economizados=cascade.tokens_economizados, timeline=cached_timeline, usage=self._usage(budget), suggestions=await _next_steps(self.store, customer, covered={"order", "invoice"}), langfuse_trace_url=trace_url)
        timeline.append(TimelineEvent(category="cache", title="Cascata semântica: MISS (curto prazo + cache global)", collection="short_term_memory", op="vectorSearch", filter={"session_id": conversation_id, "agent": fanout_key}, result={"hit": False, **({"personal": cascade.personal_reason, "classifier": cascade.classifier} if cascade.personal_reason else {})}))
        tail_start = len(timeline)
        timeline.append(TimelineEvent(category="fanout", title="Despacho paralelo", collection="multiagent_brain.routing_rules", op="read", filter={"targets": targets}, result={"agents": targets}))
        for target in targets:
            budget.reserve(target, estimate_tokens(masked))
            await metrics.increment(f"agent.{target}.turns")
        area_labels = {"order_agent": "pedido/entrega", "billing_agent": "fatura/pagamento"}
        turn_context = {"conversation_id": conversation_id, "active_order_id": (conversation or {}).get("active_order_id"), "active_invoice_id": (conversation or {}).get("active_invoice_id")}
        results = await asyncio.gather(*[
            RUNNERS[target](
                self.store, masked, customer, self.llm, budget, registry.get(target),
                f" REGRA OBRIGATÓRIA: esta pergunta tem 2 partes e outro agente já está respondendo a outra em "
                f"paralelo. Sua resposta deve conter SOMENTE o assunto '{area_labels.get(target, target)}'. "
                f"Comece direto pela resposta sobre {area_labels.get(target, target)}. NÃO escreva nenhuma frase "
                f"sobre o outro assunto, nem para dizer que não tem acesso — apague esse pensamento, apenas não "
                f"mencione o outro tema em nenhuma hipótese.",
                turn_context,
            )
            for target in targets
        ])
        for target, result in zip(targets, results):
            timeline.append(result.event)
            timeline.extend(result.extra_events)
            budget.reserve(target, estimate_tokens(result.response))
        response = "\n\n".join(result.response for result in results)
        output_guardrail = await check_output(self.store, response, customer)
        timeline.append(TimelineEvent(category="guardrail", title="Guardrail de saída", result={"blocked": output_guardrail.blocked}))
        if output_guardrail.blocked:
            response = "A resposta foi retida pela política de segurança."
        current = fanout_key
        await cascade_store_turn(self.store, target=fanout_key, area=customer["area"], customer_key=customer["customer_key"], session_id=conversation_id, intent=None, message=masked, answer=response, timeline=[event.model_dump(mode="json") for event in timeline[tail_start:]], active_agent=current)
        await self._update_conversation(conversation_id, customer, masked, response, current, [], timeline)
        usage = {**budget.used_by_agent, "total": budget.total_used, "cache_read": budget.cache_read_tokens, "cache_write": budget.cache_write_tokens}
        await metrics.increment("tokens.total", budget.total_used)
        await metrics.increment("fanout.turns")
        await metrics.increment("cache.misses")
        trace_url = await self._persist_trace(conversation_id, customer, masked, response, timeline, current, usage, (perf_counter() - started) * 1000, llm_calls=budget.llm_calls)
        await _record_collection_metrics(timeline)
        suggestions = await _next_steps(self.store, customer, covered={"order", "invoice"})
        return self._response(budget, conversation_id=conversation_id, response=response, active_agent=current, route_source="fanout", cache_hit=False, cache_source=None, tokens_economizados=0, timeline=timeline, usage=usage, suggestions=suggestions, langfuse_trace_url=trace_url)

    async def _update_conversation(self, conversation_id: str, customer: dict, message: str, response: str, active_agent: str, handoffs: list[dict], timeline: list[TimelineEvent] | None = None) -> None:
        """Aplica só o DELTA deste turno via `update_one` atômico — nunca reescreve o documento inteiro.

        Antes disto, o turno lia `agent_conversations` uma única vez no início de `run_turn` e, no
        fim, fazia `replace_one` do documento inteiro reconstruído em memória. Dois turnos
        concorrentes na MESMA `conversation_id` (double-click do usuário, retry de rede sobrepondo a
        request original em voo sob timeout do LLM) liam o mesmo estado inicial; o `replace_one` que
        terminasse por último sobrescrevia o documento inteiro e apagava a mensagem do turno que
        terminou primeiro — um lost update clássico. `$push`/`$each`/`$slice` fazem o histórico
        crescer por append no servidor, então a ordem de chegada dos dois turnos não importa: os dois
        acabam presentes, na ordem em que cada um efetivamente terminou.
        """
        now = utcnow()
        turn_entries = [{"role": "user", "content": message, "at": now}, {"role": "assistant", "content": response, "at": now}]
        handoff_entries = [{key: value for key, value in item.items() if key != "conversation_id"} for item in handoffs]

        # "pedido/fatura ativo": último order_id/invoice_id que um agente de fato tocou NESTE turno — é
        # o que order_agent/billing_agent/warranty_agent/logistics_agent usam como contexto no PRÓXIMO
        # turno quando a mensagem não cita um PED-/FAT- explícito. Só entra no $set quando este turno
        # de fato produziu um valor: sem isso, dois turnos concorrentes (um que toca pedido, outro que
        # não) poderiam fazer o que não tocou nada sobrescrever o campo com um valor antigo por engano
        # — aqui ele simplesmente não menciona o campo, e o servidor preserva o que já estava lá.
        set_fields: dict = {"active_agent": active_agent, "updated_at": now}
        for event in timeline or []:
            if isinstance(event.result, dict) and event.result.get("order_id"):
                set_fields["active_order_id"] = event.result["order_id"]
            if isinstance(event.result, dict) and event.result.get("invoice_id"):
                set_fields["active_invoice_id"] = event.result["invoice_id"]

        update: dict = {
            "$setOnInsert": {"conversation_id": conversation_id, "customer_key": customer["customer_key"]},
            "$set": set_fields,
            # -20: mesmo teto de antes (20 mensagens / handoffs), agora aplicado pelo próprio
            # servidor a cada append, nunca por um recorte feito em memória sobre um snapshot velho.
            "$push": {"turns": {"$each": turn_entries, "$slice": -20}},
        }
        if handoff_entries:
            update["$push"]["handoff_chain"] = {"$each": handoff_entries, "$slice": -20}

        await self.store.update_one(
            "agent_conversations",
            {"conversation_id": conversation_id, "customer_key": customer["customer_key"]},
            update,
            upsert=True,
        )

    async def _persist_trace(self, conversation_id: str, customer: dict, message: str, response: str, timeline: list[TimelineEvent], active_agent: str, usage: dict, duration_ms: float = 0, llm_calls: list[dict] | None = None) -> str | None:
        await self.store.insert_one("agent_traces", {"conversation_id": conversation_id, "customer_key": customer["customer_key"], "area": customer["area"], "message": message, "response": response, "active_agent": active_agent, "timeline": [event.model_dump(mode="python") for event in timeline], "usage": usage, "llm_calls": llm_calls or [], "economics": summarize_calls(llm_calls or []), "duration_ms": round(duration_ms, 2), "at": utcnow()})
        # Uma trace Langfuse por turno cobrindo a timeline inteira (roteamento, cache, cada hop de
        # agente, handoffs, guardrails) — não só a decisão de cache isolada. Best-effort: uma falha
        # aqui nunca derruba a resposta já persistida acima.
        try:
            return build_turn_trace(conversation_id=conversation_id, customer_key=customer["customer_key"], message=message, response=response, timeline=timeline, active_agent=active_agent, usage=usage, llm_calls=llm_calls, settings=self.store.settings)
        except Exception:  # noqa: BLE001
            return None
