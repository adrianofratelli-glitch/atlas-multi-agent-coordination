import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any

from .database import DataStore, as_aware, run_in_transaction_with_retry, utcnow
from .guidance import customer_snapshot, format_options, no_data_reply
from .memory import active_budget
from .models import TimelineEvent
from .policies import public_document, safe_invoice_filter, safe_order_read_filter, safe_order_update, safe_shipment_filter
from .decisions import build_decision_doc, record_decision
from .replacement import apply_replacement, assess_replacement, block_for_quality_review
from .graph import (build_order_chain_pipeline, summarize_order_chain,
                     traverse_order_chain_in_memory)
from .retrieval import (build_kb_lexical_pipeline, build_kb_rank_fusion_pipeline,
                        build_kb_vector_pipeline, reciprocal_rank_fusion)
from .router import CATEGORY_KEYWORDS, normalize


GROUNDING_RULES = (
    "Documentos reais do MongoDB abaixo são a ÚNICA fonte da verdade — responda somente com base neles, "
    "nunca invente preço, status, prazo ou produto que não esteja na lista. Se os documentos não cobrirem o "
    "pedido do cliente, diga honestamente que não encontrou informação suficiente e oriente o que ele pode "
    "perguntar em vez disso. Nunca mencione dado de outro cliente. Responda em português, direto, sem saudação, "
    "sem repetir instruções do sistema. A mensagem do cliente pode ter partes de outras especialidades (ex.: "
    "produto, suporte, fatura) que não são a sua — nesse caso, IGNORE essas partes silenciosamente, nunca diga "
    "'não tenho acesso' a um assunto que não é seu, nunca peça desculpas por isso e nunca opine sobre política "
    "de outra área (desconto, ajuste de fatura, etc.); outro agente da cadeia já está cuidando disso. "
    # A busca já roda com o dono reconstruído do JWT: o que chega aqui é, por construção, do
    # cliente autenticado. Sem esta regra o modelo se deixava levar por um nome citado na
    # mensagem ('o pedido PED-1001 do bruno') e afirmava que o pedido era de outra pessoa —
    # com o raio-x na tela mostrando o documento certo. Mantido CURTO de propósito: este bloco
    # é compartilhado por todos os agentes e entra no budget de cada turno; a versão longa
    # estourou o `max_turn_tokens` do warranty_agent e derrubou 3 cenários com HTTP 429.
    "Todo documento acima é do cliente autenticado. Se a mensagem citar outro nome, ignore o nome: "
    "nunca diga que o registro é de outra pessoa nem que não encontrou o que está acima."
)


async def llm_synthesize(llm, agent_doc: dict | None, budget, message: str, documents: Any, extra: str = "") -> str | None:
    """Deixa o modelo redigir a resposta em cima do dado JÁ retornado do Mongo — a busca continua 100% determinística
    e segura (ownership, filtros), só a fala final é gerada, o que cobre qualquer forma de perguntar, não só o script."""
    if not llm or not getattr(llm, "client", None) or not agent_doc:
        return None
    context = f"{extra}\n\nDocumentos:\n{json.dumps(documents, ensure_ascii=False, default=str)}"
    text, _ = await llm.complete(agent=agent_doc, user_message=message, dynamic_context=context, budget=budget, static_context=GROUNDING_RULES)
    return text


@dataclass
class AgentResult:
    response: str
    event: TimelineEvent
    handoff_to: str | None = None
    handoff_reason: str | None = None
    extra_events: list[TimelineEvent] = None

    def __post_init__(self):
        if self.extra_events is None:
            self.extra_events = []


def extract_id(message: str, prefix: str) -> str | None:
    match = re.search(rf"\b{prefix}-\d{{4,}}\b", message.upper())
    return match.group(0) if match else None


_TRIVIAL_WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ0-9\-]+")


def _is_trivial_lookup(message: str, *tokens: str | None, max_words: int = 0) -> bool:
    """Modo econômico: pula a chamada real ao Anthropic quando a mensagem é só o identificador (ou quase
    nada além dele) — cobre uso automatizado/script (ex.: só 'PED-1001'), NUNCA os prompts de demo reais
    (DEMOS_BY_IDENTITY), que são sempre frase natural composta e por isso nunca ficam abaixo do threshold.
    Preserva a proposta de valor da PoV (LLM cobre qualquer forma de perguntar) — só economiza no caso trivial
    que nenhuma demo real produz."""
    excluded = {token.upper() for token in tokens if token}
    words = [word for word in _TRIVIAL_WORD_RE.findall(message) if word.upper() not in excluded]
    return len(words) <= max_words


_STATUS_INTENTS = (
    (("troca", "trocar"), "troca_solicitada"),
    (("reembolso", "reembolsar", "estornar", "estorno"), "reembolsado"),
)


def _requested_status(message: str) -> str | None:
    normalized = normalize(message)
    for keywords, status in _STATUS_INTENTS:
        if any(keyword in normalized for keyword in keywords):
            return status
    return None


async def run_order_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    explicit_order_id = extract_id(message, "PED")
    order_id = explicit_order_id or (context or {}).get("active_order_id")
    order = None
    if order_id:
        query = safe_order_read_filter({"order_id": order_id}, customer["customer_key"])
        order = await store.find_one("orders", query)
    if not order and not explicit_order_id:
        # só cai pro "pedido mais recente" quando NÃO havia PED- explícito na mensagem (nem no contexto
        # ativo da conversa) — um PED- citado errado/de outro cliente tem que dar "não encontrei", nunca
        # silenciosamente resolver outro pedido (isolamento). Sem PED- nenhum e sem contexto ativo, aí sim
        # o fallback por "mais recente" é a única opção razoável.
        query = {"owner_customer_key": customer["customer_key"]}
        orders = await store.find_many("orders", query, limit=1, sort=[("order_id", -1)])
        order = orders[0] if orders else None
    clean = public_document(order)
    requested_status = _requested_status(message) if clean else None
    if not clean:
        # Beco sem saída vira orientação ancorada em dado real: lista os pedidos que
        # ESTA identidade tem de fato, em vez de mandar o cliente "conferir o número".
        snapshot = await customer_snapshot(store, customer)
        response = no_data_reply("order", snapshot, identifier=explicit_order_id)
        event = TimelineEvent(category="agent", title="Consulta segura de pedido (sem resultado — orientação com os pedidos reais)", agent="order_agent", collection="orders", op="read", filter=query, result=[], duration_ms=(perf_counter() - started) * 1000)
        return AgentResult(response, event)
    if requested_status == "troca_solicitada":
        # A troca é uma operação de domínio, não um `$set` qualquer: `apply_replacement`
        # consulta a cadeia de reposições, decide, e só então escreve — tudo num lugar só.
        # `troca_solicitada` nem sequer é alcançável por `safe_order_update` (ver
        # policies.GUARDED_STATUSES), então nenhum caminho novo escreve isso por engano.
        outcome = await apply_replacement(
            store, clean, customer_key=customer["customer_key"], agent="order_agent",
            conversation_id=(context or {}).get("conversation_id", ""))
        chain = outcome.chain
        if outcome.blocked_by_recurrence:
            response = (
                f"O pedido {clean['order_id']} de {clean['product']} já passou por "
                f"{chain['replacements']} reposições ({' → '.join(chain['path'])}), sempre pelo mesmo "
                "produto. Isso indica defeito de lote, e não uso indevido — por isso não vou "
                "processar mais uma troca automática, que tenderia a repetir o problema."
            )
            if outcome.review:
                response += (f" Abri a análise de qualidade **{outcome.review['review_id']}** e um "
                             "especialista humano decide o encaminhamento.")
            return AgentResult(response, outcome.events[0], extra_events=outcome.events[1:])

        clean["status"] = "troca_solicitada"
        response = (f"O pedido {clean['order_id']} de {clean['product']} foi atualizado para "
                    "**troca_solicitada**." if outcome.changed else
                    f"O pedido {clean['order_id']} de {clean['product']} já está com status "
                    "**troca_solicitada**.")
        event = outcome.events[-1] if outcome.changed else outcome.events[0]
        extra = [item for item in outcome.events if item is not event]
        if (context or {}).get("returning_from") == "logistics_agent":
            response = (
                f"Confirmação final: o pedido {clean['order_id']} de {clean['product']} permanece "
                f"com status **{clean['status']}** após a consulta logística."
            )
            event.title = "Confirmação final do pedido após logística"
            return AgentResult(response, event, extra_events=extra)
        wants_billing_check = any(term in normalize(message) for term in ("fatura", "cobranca", "cobrança", "desconto"))
        wants_logistics_check = any(term in normalize(message) for term in ("rastreio", "transportadora", "entrega", "rastreamento"))
        if wants_billing_check:
            return AgentResult(response, event, "billing_agent", "cliente quer saber o impacto da troca/reembolso na fatura", extra_events=extra)
        if wants_logistics_check:
            return AgentResult(response, event, "logistics_agent", "cliente quer saber o rastreio/transportadora após a troca", extra_events=extra)
        return AgentResult(response, event, extra_events=extra)

    if requested_status:
        # segue valendo mesmo se o pedido JÁ estava nesse status (ex.: cliente repete "quero trocar" numa
        # conversa nova) — o cliente ainda pode estar perguntando o próximo passo (fatura/entrega), então o
        # handoff não pode depender de uma escrita ter de fato acontecido neste turno específico.
        changed = requested_status != clean["status"]
        if changed:
            write_query, update = safe_order_update({"order_id": clean["order_id"], "status": requested_status}, customer["customer_key"])
            previous_status = clean["status"]
            # Mudança de status e registro da decisão numa transação só: se o registro
            # falhar, o pedido volta ao status anterior. Mundo alterado sem trilha era
            # justamente o furo que a trilha existe para impedir.
            async def _write(tx):
                await store.update_one("orders", write_query, update, session=tx)
                await record_decision(store, build_decision_doc(
                    action="order_status_change", subject_id=clean["order_id"],
                    customer_key=customer["customer_key"], agent="order_agent",
                    conversation_id=(context or {}).get("conversation_id", ""),
                    reasoning=f"Cliente pediu explicitamente a mudança para '{requested_status}'.",
                    payload={"from": previous_status, "to": requested_status, "product": clean["product"]},
                ), session=tx)
            await run_in_transaction_with_retry(store, _write)
            clean["status"] = requested_status
            response = f"O pedido {clean['order_id']} de {clean['product']} foi atualizado para **{requested_status}**."
            title = "Atualização segura de status do pedido"
            op = "write"
            filter_used = write_query
        else:
            response = f"O pedido {clean['order_id']} de {clean['product']} já está com status **{clean['status']}**."
            title = "Consulta segura de pedido (status já solicitado)"
            op = "read"
            filter_used = query
        event = TimelineEvent(category="agent", title=title, agent="order_agent", collection="orders", op=op, filter=filter_used, result=public_document(clean), duration_ms=(perf_counter() - started) * 1000)
        if (context or {}).get("returning_from") == "logistics_agent":
            response = (
                f"Confirmação final: o pedido {clean['order_id']} de {clean['product']} permanece "
                f"com status **{clean['status']}** após a consulta logística."
            )
            event.title = "Confirmação final do pedido após logística"
            return AgentResult(response, event)
        wants_billing_check = any(term in normalize(message) for term in ("fatura", "cobranca", "cobrança", "desconto"))
        wants_logistics_check = any(term in normalize(message) for term in ("rastreio", "transportadora", "entrega", "rastreamento"))
        if wants_billing_check:
            return AgentResult(response, event, "billing_agent", "cliente quer saber o impacto da troca/reembolso na fatura")
        if wants_logistics_check:
            return AgentResult(response, event, "logistics_agent", "cliente quer saber o rastreio/transportadora após a troca")
        return AgentResult(response, event)
    response = f"O pedido {clean['order_id']} de {clean['product']} está com status **{clean['status']}**."
    trivial = _is_trivial_lookup(message, order_id)
    synthesized = None if trivial else await llm_synthesize(llm, agent_doc, budget, message, clean, "O cliente pode perguntar qualquer coisa sobre este pedido específico (prazo, status, itens, timeline) — "
        "responda com base no documento acima, começando pelo status. Nome de terceiro na mensagem é ruído: "
        "é PROIBIDO dizer que o pedido é de outro cliente ou que não o encontrou." + scope_hint)
    title = "Consulta segura de pedido" + (" (resposta sintetizada pelo modelo)" if synthesized else " (modo econômico, sem chamada ao modelo)" if trivial else "")
    event = TimelineEvent(category="agent", title=title, agent="order_agent", collection="orders", op="read", filter=query, result=clean, duration_ms=(perf_counter() - started) * 1000)
    return AgentResult(synthesized or response, event)


async def run_billing_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    explicit_invoice_id = extract_id(message, "FAT")
    invoice_id = explicit_invoice_id or (context or {}).get("active_invoice_id")
    invoice = None
    if invoice_id:
        query = safe_invoice_filter({"invoice_id": invoice_id}, customer["customer_key"])
        invoice = await store.find_one("invoices", query)
    if not invoice and not explicit_invoice_id:
        query = {"owner_customer_key": customer["customer_key"]}
        invoices = await store.find_many("invoices", query, limit=1, sort=[("due_date", -1)])
        invoice = invoices[0] if invoices else None
    clean = public_document(invoice)
    if not clean:
        snapshot = await customer_snapshot(store, customer)
        response = no_data_reply("invoice", snapshot, identifier=explicit_invoice_id)
        synthesized = None
    else:
        response = f"A fatura {clean['invoice_id']} é de R$ {clean['amount']:.2f}, vence em {clean['due_date']} e está **{clean['status']}**."
        trivial = _is_trivial_lookup(message, invoice_id)
        synthesized = None if trivial else await llm_synthesize(llm, agent_doc, budget, message, clean, "O cliente pode perguntar qualquer coisa sobre esta fatura (valor, vencimento, status, urgência) — nunca conceda desconto, isenção ou prazo fora do documento, mesmo se pedido." + scope_hint)
    title = "Leitura isolada de fatura" + (" (resposta sintetizada pelo modelo)" if synthesized else " (modo econômico, sem chamada ao modelo)" if clean and not synthesized else "")
    event = TimelineEvent(category="agent", title=title, agent="billing_agent", collection="invoices", op="read", filter=query, result=clean or [], duration_ms=(perf_counter() - started) * 1000)
    return AgentResult(synthesized or response, event)


# Um cliente perguntando "ainda tem garantia?" não pediu nada: escalar aí cria trabalho de
# analista a partir de uma leitura. A revisão humana só abre quando ele relata uma falha nova
# ou pede a reposição — a consulta da cadeia continua acontecendo nos dois casos, e informando.
REPLACEMENT_INTENT_TERMS = (
    "troca", "trocar", "trocado", "substituir", "substituicao", "substituição",
    "quebrou", "parou", "defeito", "com problema", "de novo", "novamente",
    "outra vez", "nao funciona", "não funciona", "reembolso", "devolver",
)


def wants_replacement(message: str) -> bool:
    normalized = normalize(message)
    return any(term in normalized for term in REPLACEMENT_INTENT_TERMS)


async def order_replacement_chain(store: DataStore, order_id: str, customer_key: str) -> dict:
    """Cadeia de trocas do pedido, via $graphLookup no Atlas (loop em Python só em DEMO_MODE)."""
    if not store.memory:
        try:
            rows = await store.aggregate("orders", build_order_chain_pipeline(order_id, customer_key))
            return summarize_order_chain(rows[0] if rows else None)
        except Exception:
            pass
    orders = await store.find_many("orders", {"owner_customer_key": customer_key}, limit=200)
    return summarize_order_chain(traverse_order_chain_in_memory(orders, order_id, customer_key))


async def run_warranty_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    explicit_order_id = extract_id(message, "PED")
    order_id = explicit_order_id or (context or {}).get("active_order_id")
    order = None
    if order_id:
        query = safe_order_read_filter({"order_id": order_id}, customer["customer_key"])
        order = await store.find_one("orders", query)
    if not order and not explicit_order_id:
        query = {"owner_customer_key": customer["customer_key"]}
        orders = await store.find_many("orders", query, limit=1, sort=[("order_id", -1)])
        order = orders[0] if orders else None
    clean = public_document(order)
    if not clean:
        snapshot = await customer_snapshot(store, customer)
        response = no_data_reply("order", snapshot, identifier=explicit_order_id)
        event = TimelineEvent(category="agent", title="Consulta de garantia (sem resultado — orientação com os pedidos reais)", agent="warranty_agent", collection="orders", op="read", filter=query, result=[], duration_ms=(perf_counter() - started) * 1000)
        return AgentResult(response, event)
    product = await store.find_one("products_catalog", {"name": clean["product"]})
    category = product["category"] if product else None
    policy = await store.find_one("warranty_policies", {"category": category}) if category else None
    months = policy["months"] if policy else 12
    purchased_at = next((item["at"] for item in clean.get("timeline", []) if item["status"] == "criado"), clean.get("timeline", [{}])[0].get("at"))
    covered, expires_at = None, None
    if purchased_at:
        purchase_date = datetime.strptime(purchased_at, "%Y-%m-%d")
        expiry_date = purchase_date + timedelta(days=months * 30)
        expires_at = expiry_date.strftime("%Y-%m-%d")
        covered = utcnow().replace(tzinfo=None) <= expiry_date
    doc = {"order_id": clean["order_id"], "product": clean["product"], "category": category, "warranty_months": months, "purchased_at": purchased_at, "expires_at": expires_at, "covered": covered}

    # Travessia de grafo: a garantia deste pedido depende de quantas vezes ele já foi reposto.
    # Leitura pura — este agente EXPLICA a cobertura, quem efetiva a troca é o order_agent.
    chain, graph_event = await assess_replacement(
        store, clean["order_id"], customer["customer_key"], agent="warranty_agent")
    doc["replacement_chain"] = chain

    response = (
        f"O pedido {clean['order_id']} ({clean['product']}) tem garantia de {months} meses, válida até **{expires_at}** — "
        f"{'dentro do prazo' if covered else 'fora do prazo'} de cobertura." if expires_at else
        "Não consegui calcular a garantia por falta de data de compra no registro."
    )
    # Gate: a terceira falha do MESMO produto não é mais uma troca — é análise de qualidade.
    # O agente PARA aqui em vez de prometer uma reposição que já falhou três vezes. Só que
    # ele só para quando há de fato um pedido de ação: ver `wants_replacement`.
    review = None
    if chain["needs_quality_review"] and wants_replacement(message):
        review = await block_for_quality_review(
            store, chain, agent="warranty_agent", customer_key=customer["customer_key"],
            conversation_id=(context or {}).get("conversation_id", ""))

    chain_note = ""
    if chain["replacements"]:
        chain_note = (
            f" Este pedido já passou por {chain['replacements']} reposição(ões): "
            f"{' → '.join(chain['path'])}."
        )
    if chain["recurring_defect"]:
        chain_note += (
            f" Como o mesmo produto ({chain['product']}) já exigiu {chain['replacements']} reposições, "
            "isso indica defeito de lote e não uso indevido — trocar de novo tende a repetir o problema."
        )
    if review:
        chain_note += (
            f" Abri a análise de qualidade **{review['review_id']}** e um especialista humano "
            "decide o encaminhamento — eu não processo mais uma troca automática deste item."
        )
    response += chain_note

    trivial = _is_trivial_lookup(message, order_id) and not chain["replacements"]
    synthesized = None if trivial else await llm_synthesize(llm, agent_doc, budget, message, doc, "O cliente pode perguntar qualquer coisa sobre a cobertura de garantia deste pedido — responda com base no documento acima, nunca invente prazo diferente do calculado. Se `replacement_chain.recurring_defect` for verdadeiro, diga explicitamente que o mesmo produto já falhou repetidas vezes e que o caso vai para análise de qualidade, em vez de prometer mais uma troca." + scope_hint)
    title = "Consulta de garantia" + (" (resposta sintetizada pelo modelo)" if synthesized else " (modo econômico, sem chamada ao modelo)" if trivial else "")
    event = TimelineEvent(category="agent", title=title, agent="warranty_agent", collection="warranty_policies", op="read", filter={"category": category}, result=doc, duration_ms=(perf_counter() - started) * 1000)
    wants_alternative = any(term in normalize(message) for term in ("parecido", "parecida", "similar", "mais barato", "mais barata", "recomenda", "substituto"))
    tail_events = [graph_event]
    if review:
        tail_events.append(TimelineEvent(
            category="agent", title="Escalonamento pausável: caso aguarda decisão humana",
            agent="warranty_agent", collection="pending_reviews", op="write",
            filter={"subject_id": clean["order_id"], "status": "pending"},
            result={"review_id": review["review_id"], "recommended_action": review["recommended_action"],
                    "risk_factors": review["risk_factors"]},
            reason=review["reasoning"]))
    if wants_alternative:
        return AgentResult(synthesized or response, event, "product_agent", "cliente pediu produto alternativo após consulta de garantia", extra_events=tail_events)
    return AgentResult(synthesized or response, event, extra_events=tail_events)


# Janela de deduplicação de resgate: um retry de rede plausível (timeout + reenvio) chega em
# segundos, não minutos; 20s cobre isso sem arriscar negar um segundo resgate genuinamente novo
# do mesmo item que o cliente peça pouco depois.
REDEMPTION_IDEMPOTENCY_WINDOW_SECONDS = 20

REWARD_CATALOG = {
    "frete gratis": ("frete grátis no próximo pedido", 300),
    "cupom de desconto": ("cupom de 10% de desconto", 500),
    "voucher": ("voucher de R$ 30", 500),
}


async def run_loyalty_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    query = {"customer_key": customer["customer_key"]}
    account = await store.find_one("loyalty_accounts", query)
    clean = public_document(account)
    if not clean:
        snapshot = await customer_snapshot(store, customer)
        response = ("Não encontrei uma conta de fidelidade ativa para a sua identidade."
                    + (format_options(snapshot, exclude={"loyalty"})
                       or " Se você acabou de aderir ao programa, o saldo aparece no próximo ciclo."))
        event = TimelineEvent(category="agent", title="Consulta de fidelidade", agent="loyalty_agent", collection="loyalty_accounts", op="read", filter=query, result=[], duration_ms=(perf_counter() - started) * 1000)
        return AgentResult(response, event)

    normalized = normalize(message)
    reward_key = next((key for key in REWARD_CATALOG if key in normalized), None)
    wants_redemption = any(term in normalized for term in ("resgatar", "resgate", "trocar meus pontos", "usar pontos", "usar meus pontos"))
    if reward_key is None and wants_redemption and not any(term in normalized for term in ("produto", "catalogo", "presente")):
        # Pediu resgate de algo que NÃO existe na tabela de recompensas. Em vez de deixar o
        # modelo improvisar um caminho ("acesse a seção de resgate no app" — que não existe),
        # devolve o catálogo real com o custo de cada item e a distância até ele.
        pontos = clean["points"]
        linhas = []
        for label, cost in sorted(REWARD_CATALOG.values(), key=lambda item: item[1]):
            marcador = "✓ disponível" if pontos >= cost else f"faltam {cost - pontos} pontos"
            linhas.append(f"- **{label}** — {cost} pontos ({marcador})")
        response = (
            f"Esse item não faz parte da tabela de recompensas do programa. "
            f"Você tem **{pontos} pontos** (tier {clean['tier']}), e o que dá para resgatar hoje é:\n"
            + "\n".join(linhas)
            + "\n\nSe preferir, eu também busco um produto do catálogo dentro do seu saldo."
        )
        event = TimelineEvent(category="agent", title="Resgate fora da tabela de recompensas (catálogo real apresentado)", agent="loyalty_agent", collection="loyalty_accounts", op="read", filter=query, result=clean, duration_ms=(perf_counter() - started) * 1000)
        return AgentResult(response, event)
    if reward_key:
        # resgate real: escrita restrita a $inc de pontos (nunca um valor arbitrário do modelo) + registro em
        # redemptions — mesmo padrão de segurança do order_agent (filtro reconstruído, campo aprovado só).
        label, cost = REWARD_CATALOG[reward_key]
        # Idempotência: um retry de rede no /api/chat (timeout do cliente, reenvio automático) reprocessa a
        # mesma intenção do zero. Sem esta checagem, um resgate idêntico dentro de uma janela curta debitaria
        # os pontos duas vezes — diferente da mudança de status do order_agent, que já é idempotente por
        # construção comparando o status desejado com o atual. Aqui não há "status atual" para comparar: o
        # sinal de "isso já aconteceu" é um resgate recente do MESMO cliente para a MESMA recompensa, no
        # mesmo espírito de `open_review` (idempotente por subject_id+action+status).
        recent_cutoff = utcnow() - timedelta(seconds=REDEMPTION_IDEMPOTENCY_WINDOW_SECONDS)
        recent_redemptions = await store.find_many(
            "redemptions",
            {"customer_key": customer["customer_key"], "reward": label, "status": "confirmado"},
            limit=5, sort=[("at", -1)],
        )
        duplicate = next((item for item in recent_redemptions if item.get("at") and as_aware(item["at"]) >= recent_cutoff), None)
        if duplicate:
            response = (
                f"Resgate confirmado: **{label}**, {duplicate['points_spent']} pontos debitados "
                f"(pedido de resgate idêntico já processado há poucos segundos — não debitei de novo)."
            )
            event = TimelineEvent(category="agent", title="Resgate de fidelidade (idempotência: repetição recente ignorada)", agent="loyalty_agent", collection="redemptions", op="read", filter=query, result=public_document(duplicate), duration_ms=(perf_counter() - started) * 1000)
            return AgentResult(response, event)
        if clean["points"] < cost:
            faltam = cost - clean["points"]
            acessiveis = [f"**{lbl}** ({price} pontos)" for lbl, price in sorted(REWARD_CATALOG.values(), key=lambda item: item[1]) if clean["points"] >= price]
            alternativa = (" Com o saldo atual você já consegue: " + ", ".join(acessiveis) + "."
                           if acessiveis else " Assim que o saldo subir, eu processo o resgate na hora.")
            response = (f"Você tem {clean['points']} pontos e {label} custa {cost} — faltam {faltam} pontos."
                        + alternativa)
            await record_decision(store, build_decision_doc(
                action="loyalty_redemption_denied", subject_id=customer["customer_key"],
                customer_key=customer["customer_key"], agent="loyalty_agent",
                conversation_id=(context or {}).get("conversation_id", ""),
                reasoning=f"Saldo de {clean['points']} pontos é insuficiente para '{label}' ({cost} pontos).",
                risk_factors=["saldo_insuficiente"],
                payload={"reward": label, "cost": cost, "balance": clean["points"]},
            ), severity="warning")
            event = TimelineEvent(category="agent", title="Resgate de fidelidade negado (saldo insuficiente)", agent="loyalty_agent", collection="loyalty_accounts", op="read", filter=query, result=clean, duration_ms=(perf_counter() - started) * 1000)
            return AgentResult(response, event)
        redemption = {"redemption_id": f"RDM-{customer['customer_key'].upper()}-{int(started)}", "customer_key": customer["customer_key"], "reward": label, "points_spent": cost, "status": "confirmado", "at": utcnow()}
        # Três escritas, uma transação: débito dos pontos, comprovante e decisão. É a mais
        # crítica das quatro — um débito sem comprovante é saldo que sumiu sem explicação.
        async def _write(tx):
            await store.update_one("loyalty_accounts", query, {"$inc": {"points": -cost}}, session=tx)
            await store.insert_one("redemptions", redemption, session=tx)
            await record_decision(store, build_decision_doc(
                action="loyalty_redemption", subject_id=redemption["redemption_id"],
                customer_key=customer["customer_key"], agent="loyalty_agent",
                conversation_id=(context or {}).get("conversation_id", ""),
                reasoning=f"Saldo de {clean['points']} pontos cobre o custo de {cost} de '{label}'.",
                payload={"reward": label, "points_spent": cost, "balance_after": clean["points"] - cost},
            ), session=tx)
        await run_in_transaction_with_retry(store, _write)
        response = f"Resgate confirmado: **{label}**, {cost} pontos debitados. Saldo restante: {clean['points'] - cost} pontos."
        event = TimelineEvent(category="agent", title="Resgate de fidelidade confirmado", agent="loyalty_agent", collection="redemptions", op="write", filter=query, result=redemption, duration_ms=(perf_counter() - started) * 1000)
        return AgentResult(response, event)

    benefits = ", ".join(clean["tier_benefits"]) if clean["tier_benefits"] else "nenhum benefício adicional no tier atual"
    response = f"Você tem **{clean['points']} pontos**, tier **{clean['tier']}**. Benefícios: {benefits}."
    trivial = _is_trivial_lookup(message, max_words=2)
    synthesized = None if trivial else await llm_synthesize(llm, agent_doc, budget, message, clean, "O cliente pode perguntar qualquer coisa sobre pontos, tier ou benefícios — responda com base no documento acima, nunca invente pontuação ou benefício que não esteja nele." + scope_hint)
    title = "Consulta de fidelidade" + (" (resposta sintetizada pelo modelo)" if synthesized else " (modo econômico, sem chamada ao modelo)" if trivial else "")
    event = TimelineEvent(category="agent", title=title, agent="loyalty_agent", collection="loyalty_accounts", op="read", filter=query, result=clean, duration_ms=(perf_counter() - started) * 1000)
    wants_redeem_product = any(term in normalized for term in ("resgatar", "trocar meus pontos", "usar pontos"))
    if wants_redeem_product:
        return AgentResult(synthesized or response, event, "product_agent", "cliente quer usar pontos de fidelidade para resgatar um produto do catálogo")
    return AgentResult(synthesized or response, event)


async def run_logistics_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    explicit_order_id = extract_id(message, "PED")
    order_id = explicit_order_id or (context or {}).get("active_order_id")
    shipment = None
    if order_id:
        query = safe_shipment_filter({"order_id": order_id}, customer["customer_key"])
        shipment = await store.find_one("shipments", query)
    if not shipment and not explicit_order_id:
        query = {"owner_customer_key": customer["customer_key"]}
        shipments = await store.find_many("shipments", query, limit=1, sort=[("estimated_delivery", -1)])
        shipment = shipments[0] if shipments else None
    clean = public_document(shipment)
    if not clean:
        snapshot = await customer_snapshot(store, customer)
        response = no_data_reply("shipment", snapshot, identifier=explicit_order_id)
        event = TimelineEvent(category="agent", title="Consulta de logística", agent="logistics_agent", collection="shipments", op="read", filter=query, result=[], duration_ms=(perf_counter() - started) * 1000)
        return AgentResult(response, event)

    normalized = normalize(message)
    wants_final_order_confirmation = any(
        term in normalized
        for term in (
            "volte para confirmar",
            "confirme que a troca",
            "confirmar que a troca",
            "confirme que o reembolso",
            "confirmar que o reembolso",
            "status final",
        )
    )
    order_already_ran = "order_agent" in (context or {}).get("handoff_path", [])
    wants_reschedule = any(term in normalized for term in ("reagendar", "mudar a entrega", "outro dia", "adiar a entrega"))
    if wants_reschedule and clean["current_location"] != "Entregue":
        # escrita restrita: só um campo de sinalização, nunca a transportadora/prazo real — quem confirma
        # a nova data é a transportadora, o sistema só registra o pedido de reagendamento.
        async def _write(tx):
            await store.update_one("shipments", query, {"$set": {"reschedule_requested": True}}, session=tx)
            await record_decision(store, build_decision_doc(
                action="shipment_reschedule", subject_id=clean.get("order_id", ""),
                customer_key=customer["customer_key"], agent="logistics_agent",
                conversation_id=(context or {}).get("conversation_id", ""),
                reasoning="Cliente pediu reagendamento da entrega.",
                payload={"carrier": clean.get("carrier"), "tracking_code": clean.get("tracking_code")},
            ), session=tx)
        await run_in_transaction_with_retry(store, _write)
        response = f"Reagendamento solicitado para o pedido {clean['order_id']}. A transportadora **{clean['carrier']}** vai confirmar uma nova janela de entrega em até 24h."
        event = TimelineEvent(category="agent", title="Reagendamento de entrega solicitado", agent="logistics_agent", collection="shipments", op="write", filter=query, result={**clean, "reschedule_requested": True}, duration_ms=(perf_counter() - started) * 1000)
        if wants_final_order_confirmation and order_already_ran:
            return AgentResult(response, event, "order_agent", "cliente pediu confirmação final do pedido após a etapa logística")
        return AgentResult(response, event)

    response = f"Pedido {clean['order_id']}: transportadora **{clean['carrier']}**, código **{clean['tracking_code']}**, {clean['current_location']}, previsão **{clean['estimated_delivery']}**."
    trivial = _is_trivial_lookup(message, order_id)
    synthesized = None if trivial else await llm_synthesize(llm, agent_doc, budget, message, clean, "O cliente pode perguntar qualquer coisa sobre transportadora, rastreio, localização ou previsão de entrega — responda com base no documento acima, nunca invente transportadora ou prazo." + scope_hint)
    title = "Consulta de logística" + (" (resposta sintetizada pelo modelo)" if synthesized else " (modo econômico, sem chamada ao modelo)" if trivial else "")
    event = TimelineEvent(category="agent", title=title, agent="logistics_agent", collection="shipments", op="read", filter=query, result=clean, duration_ms=(perf_counter() - started) * 1000)
    if wants_final_order_confirmation and order_already_ran:
        return AgentResult(synthesized or response, event, "order_agent", "cliente pediu confirmação final do pedido após a etapa logística")
    return AgentResult(synthesized or response, event)


def _weighted_score(relevance: float, rating: float, stock: int) -> float:
    """Relevância pesa mais, mas nota e disponibilidade desempatam — igual a um ranking real de e-commerce."""
    return round(0.55 * relevance + 0.30 * (rating / 5.0) + 0.15 * min(stock, 20) / 20.0, 4)


def _stem(word: str) -> str:
    """Singular/plural aproximado em pt-BR: 'fones' e 'fone' precisam casar na busca local."""
    if len(word) > 4 and word.endswith("oes"):
        return word[:-3] + "ao"
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _local_rank(items: list[dict], query: str, fields: tuple[str, ...]) -> list[dict]:
    words = {_stem(word) for word in normalize(query).split() if len(word) > 2}
    scored = []
    for item in items:
        haystack_words = {_stem(word) for word in normalize(" ".join(str(item.get(field, "")) for field in fields)).split()}
        matches = len(words & haystack_words)
        relevance = matches / max(len(words), 1)
        weighted = _weighted_score(relevance, item.get("rating", 4.0), item.get("stock", 10))
        scored.append({**item, "local_score": matches, "weighted_score": weighted})
    scored.sort(key=lambda item: item["weighted_score"], reverse=True)
    return scored


NUMBER_WORDS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10}


def detect_category(message: str) -> str | None:
    normalized = normalize(message)
    words = {_stem(word) for word in normalized.split()}
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in words:
            return category
    return None


def parse_price_ceiling(message: str) -> float | None:
    """Extrai teto de preço tanto em dígitos (R$ 350) quanto por extenso (até mil reais, até dois mil)."""
    normalized = normalize(message)
    digit_prices = [float(value.replace(",", ".")) for value in re.findall(r"(?:r\$\s*)?(\d+[.,]?\d*)\s*(?:reais|mil)?", normalized) if value]
    word_match = re.search(r"\b(um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez)?\s*mil\b", normalized)
    if word_match:
        multiplier = NUMBER_WORDS.get(word_match.group(1), 1)
        return float(multiplier * 1000)
    if digit_prices:
        return min(digit_prices)
    return None


def build_product_pipeline(message: str, max_price: float | None = None, category: str | None = None) -> list[dict]:
    """Busca de catálogo no Atlas. O teto de preço entra no `filter` do $vectorSearch (pré-filtro nativo:
    o ANN só percorre vetores que passam nele), montado em Python — nem o modelo nem o texto do prompt
    conseguem ignorá-lo. Todo campo usado aqui precisa ser `filter` em PRODUCTS_VECTOR_INDEX_DEFINITION."""
    vector_filter: dict[str, Any] = {"active": True}
    if max_price is not None:
        vector_filter["price"] = {"$lte": max_price}
    if category:
        vector_filter["category"] = category
    return [
        {"$vectorSearch": {"index": "products_autoembed_v1", "path": "search_text", "query": {"text": message}, "model": "voyage-4", "filter": vector_filter, "numCandidates": 50, "limit": 8}},
        {"$addFields": {"relevance": {"$meta": "vectorSearchScore"}}},
        {"$addFields": {"weighted_score": {"$add": [
            {"$multiply": [0.55, "$relevance"]},
            {"$multiply": [0.30, {"$divide": ["$rating", 5.0]}]},
            {"$multiply": [0.15, {"$divide": [{"$min": ["$stock", 20]}, 20.0]}]},
        ]}}},
        {"$sort": {"weighted_score": -1}},
        {"$limit": 4},
        {"$project": {"_id": 0, "sku": 1, "name": 1, "category": 1, "price": 1, "rating": 1, "stock": 1, "relevance": 1, "weighted_score": 1}},
    ]


async def search_products(store: DataStore, message: str, max_price: float | None = None, category: str | None = None) -> list[dict]:
    if not store.memory:
        try:
            cursor = await store._collection("products_catalog").aggregate(build_product_pipeline(message, max_price, category))
            return await cursor.to_list(None)
        except Exception:
            pass
    products = await store.find_many("products_catalog", {"active": True}, limit=100)
    if category:
        products = [item for item in products if item["category"] == category]
    if max_price is not None:
        products = [item for item in products if item["price"] <= max_price]
    return [public_document(item) for item in _local_rank(products, message, ("name", "category", "search_text"))[:4]]


async def run_product_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    budget_brl = await active_budget(store, customer["customer_key"])
    category = detect_category(message)
    explicit_price = parse_price_ceiling(message)
    memory_bias = budget_brl is not None and explicit_price is None
    max_price = explicit_price if explicit_price is not None else (budget_brl if memory_bias else (350.0 if "mais barato" in normalize(message) else None))
    # Limite duro: o teto da memória, ou um teto dito na mensagem que seja igual/mais estrito que ele. Só um teto
    # explícito MAIS FOLGADO que o da memória (o cliente pedindo, agora, algo mais caro) pode ser relaxado.
    hard_ceiling = budget_brl is not None and max_price is not None and max_price <= budget_brl
    products = await search_products(store, message, max_price, category)
    if not products and category and not hard_ceiling:
        # teto de preço PEDIDO NA MENSAGEM pode ter zerado a categoria certa; melhor mostrar algo da categoria
        # do que nada. O orçamento da memória do cliente é limite duro: nunca é relaxado em silêncio.
        products = await search_products(store, message, None, category)
    if not products and not category:
        # pergunta totalmente fora do script (ex. "o que vocês têm de bom pra presentear alguém"): dá pro
        # modelo o catálogo inteiro ativo pra ele raciocinar, em vez de simplesmente desistir.
        catalog = await store.find_many("products_catalog", {"active": True}, limit=100)
        if hard_ceiling:
            catalog = [item for item in catalog if item["price"] <= max_price]
        products = [public_document(item) for item in _local_rank(catalog, message, ("name", "category", "search_text"))[:6]]
    if products:
        lines = [f"- **{item['name']}** — R$ {item['price']:.2f} · ★{item.get('rating', '—')} · {item.get('stock', 0)} em estoque" for item in products[:3]]
        response = "Encontrei estas opções no catálogo:\n" + "\n".join(lines)
        if memory_bias:
            response += f"\n\n(Levei em conta o seu orçamento de até R$ {budget_brl:.2f}.)"
    elif category:
        # Sem opção na categoria+orçamento: diz o que existe de fato na categoria em vez
        # de encerrar. As faixas vêm de query, não de estimativa.
        disponiveis = await search_products(store, message, None, category)
        if disponiveis:
            faixa = min(item["price"] for item in disponiveis)
            response = (f"Não encontrei nada na categoria {category} dentro desse orçamento. "
                        f"A opção ativa mais barata da categoria sai por R$ {faixa:.2f} — "
                        f"quer que eu mostre, ou prefere que eu procure em outra categoria?")
        else:
            response = (f"A categoria {category} está sem itens ativos no catálogo agora. "
                        "Me diga o uso que você tem em mente que eu procuro uma alternativa.")
    else:
        categorias = sorted({item.get("category") for item in await store.find_many(
            "products_catalog", {"active": True}, limit=100) if item.get("category")})
        sugestao = (" O catálogo ativo tem: " + ", ".join(categorias[:8]) + "."
                    if categorias else "")
        response = ("Não achei uma opção compatível com o que você descreveu."
                    + sugestao + " Me diga o uso ou uma faixa de preço que eu refino a busca.")
    synthesized = await llm_synthesize(
        llm, agent_doc, budget, message, products,
        "Escolha e recomende só produtos desta lista (nunca invente um SKU/preço fora dela). Se nada da lista "
        "atender o pedido, diga honestamente e sugira o mais próximo disponível, explicando a diferença. Você "
        "é o agente de PRODUTOS: se a mensagem menciona um defeito, isso já foi tratado por outro agente antes "
        "de chegar até você — não pergunte de novo qual produto teve defeito, só recomende a alternativa.",
    )
    search_filter: dict[str, Any] = {"active": True}
    if category:
        search_filter["category"] = category
    if max_price is not None:
        search_filter["price"] = {"$lte": max_price}
    event = TimelineEvent(category="agent", title="Recomendação com ranking ponderado (relevância + nota + estoque)" + (" + modelo" if synthesized else ""), agent="product_agent", collection="products_catalog", op="vectorSearch", filter=search_filter, result=products[:3], duration_ms=(perf_counter() - started) * 1000)
    events = [event]
    if memory_bias:
        events.append(TimelineEvent(category="memory", title="Viés aplicado a partir da memória do cliente", agent="product_agent", collection="customer_memory", filter={"customer_key": customer["customer_key"], "max_price_brl": {"$gt": 0}}, result={"max_price_brl": budget_brl}))
    final_response = synthesized or response
    # 3º hop da cadeia: cliente já pediu diagnóstico (support_agent) e recomendação (aqui) — se também confirmou
    # querer efetivar a troca, quem processa isso com segurança é o order_agent (única escrita do sistema).
    wants_to_act = products and any(term in normalize(message) for term in ("troca", "trocar", "reembolso", "reembolsar", "estornar"))
    if wants_to_act:
        return AgentResult(final_response, event, "order_agent", "cliente confirmou que quer efetivar a troca do produto recomendado", extra_events=events[1:])
    return AgentResult(final_response, event, extra_events=events[1:])


async def search_kb(store: DataStore, message: str) -> tuple[list[dict], str]:
    """Devolve (artigos, estratégia). A estratégia vira o título do evento de timeline —
    a demo mostra explicitamente se a fusão rodou no servidor ou na aplicação."""
    if not store.memory:
        try:
            # Caminho preferido: $rankFusion funde vetorial + BM25 dentro do MongoDB (8.1+).
            articles = await store.aggregate("kb_articles", build_kb_rank_fusion_pipeline(message, limit=4))
            if articles:
                return articles, "rankFusion"
        except Exception:
            # Servidor sem $rankFusion (ou índice indisponível): cai para as duas pernas
            # separadas + RRF na aplicação, que é o comportamento anterior.
            pass
        try:
            async def execute(pipeline: list[dict]) -> list[dict]:
                cursor = await store._collection("kb_articles").aggregate(pipeline)
                return await cursor.to_list(None)
            vector, lexical = await asyncio.gather(
                execute(build_kb_vector_pipeline(message, limit=10)),
                execute(build_kb_lexical_pipeline(message, limit=10)),
            )
            for item in vector + lexical:
                item["_id"] = item.get("article_id")
            return reciprocal_rank_fusion([vector, lexical], limit=4), "rrf_aplicacao"
        except Exception:
            pass
    articles = await store.find_many("kb_articles", {}, limit=100)
    lexical = _local_rank(articles, message, ("title", "content"))
    semantic = _local_rank(articles, message, ("category", "title"))
    return reciprocal_rank_fusion([lexical, semantic], limit=4), "rrf_local"


async def run_support_agent(store: DataStore, message: str, customer: dict, llm=None, budget=None, agent_doc=None, scope_hint: str = "", context: dict | None = None) -> AgentResult:
    started = perf_counter()
    articles, retrieval_strategy = await search_kb(store, message)
    clean = [public_document(item) for item in articles]
    evidence = clean[0] if clean else None
    response = (
        f"A orientação da base é: {evidence['content']}" if evidence else
        "Não achei um artigo da base que cubra exatamente esse caso. Me descreva o que acontece "
        "(o que você fez, o que aconteceu, e desde quando) que eu diagnostico — ou peça um atendente "
        "que eu abro um chamado agora."
    )
    wants_recommendation = any(term in normalize(message) for term in ("parecido", "parecida", "similar", "mais barato", "mais barata", "recomenda"))
    handoff_note = (
        "O cliente também pediu uma recomendação de produto — você é o agente de SUPORTE, não tem o catálogo. "
        "NÃO diga que não tem acesso a preços/catálogo: diga em uma frase curta que vai conectar com o "
        "especialista em produtos, que ele já vai trazer as opções a seguir. Não repita o pedido de saber qual "
        "modelo foi comprado se o cliente já descreveu o produto na mensagem."
        if wants_recommendation else
        "Se nenhum artigo cobrir o problema relatado, diga que vai escalar/orientar de forma genérica em vez de inventar um passo a passo."
    )
    synthesized = await llm_synthesize(llm, agent_doc, budget, message, clean, handoff_note)
    event = TimelineEvent(category="agent", title=("RAG híbrido: $rankFusion server-side" if retrieval_strategy == "rankFusion" else "RAG híbrido com RRF na aplicação") + (" + modelo" if synthesized else ""), agent="support_agent", collection="kb_articles", op="hybridSearch", filter={"query": message, "strategy": retrieval_strategy}, result=clean[:3], duration_ms=(perf_counter() - started) * 1000)
    final_response = synthesized or response
    extra_events: list[TimelineEvent] = []

    wants_escalation = any(term in normalize(message) for term in ("abrir chamado", "falar com humano", "escalar", "atendente", "chamado"))
    if wants_escalation:
        # KB sem evidência confiável (ou pedido explícito de escalonar): abre chamado real em vez de deixar
        # o cliente sem próximo passo — mais uma ação de escrita além do write único do order_agent.
        ticket = {"ticket_id": f"TCK-{customer['customer_key'].upper()}-{int(started * 1000) % 100000}", "customer_key": customer["customer_key"], "area": customer["area"], "subject": message[:200], "status": "aberto", "created_at": utcnow()}
        async def _write(tx):
            await store.insert_one("support_tickets", ticket, session=tx)
            await record_decision(store, build_decision_doc(
                action="support_ticket_open", subject_id=ticket["ticket_id"],
                customer_key=customer["customer_key"], agent="support_agent",
                conversation_id=(context or {}).get("conversation_id", ""),
                reasoning="Cliente pediu explicitamente atendimento humano.",
                payload={"subject": ticket["subject"], "area": ticket["area"]},
            ), severity="warning", session=tx)
        await run_in_transaction_with_retry(store, _write)
        final_response += f"\n\nAbri o chamado **{ticket['ticket_id']}** para acompanhamento humano — nosso time entra em contato em até 24h."
        extra_events.append(TimelineEvent(category="agent", title="Chamado de suporte aberto", agent="support_agent", collection="support_tickets", op="write", result=ticket))

    if wants_recommendation:
        return AgentResult(final_response, event, "product_agent", "cliente pediu alternativa de produto após o diagnóstico", extra_events=extra_events)
    return AgentResult(final_response, event, extra_events=extra_events)


RUNNERS = {
    "order_agent": run_order_agent,
    "product_agent": run_product_agent,
    "support_agent": run_support_agent,
    "billing_agent": run_billing_agent,
    "warranty_agent": run_warranty_agent,
    "loyalty_agent": run_loyalty_agent,
    "logistics_agent": run_logistics_agent,
}
