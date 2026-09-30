"""Orientação ancorada em dado real do cliente.

Regra desta camada: uma sugestão só existe se o documento que a sustenta existe.
Nunca "você quer perguntar sobre um pedido?" no vazio — sempre "quer acompanhar o
PED-1001 (Fone Pulse X, enviado)?", com o id lido de `orders` naquele instante.

O motivo é o mesmo da PoV inteira: se a sugestão for genérica e plausível, alguém
vai clicar nela e receber "não encontrei" — o beco sem saída volta um turno depois,
agora com a credibilidade gasta. Sugestão que veio de query sempre funciona quando
clicada, e de quebra vira mais uma evidência de que o Atlas responde a pergunta.
"""
from __future__ import annotations

import unicodedata
from typing import Any

from .database import DataStore
from .policies import public_document

# Quantos itens de cada tipo entram na orientação. Três é o limite prático de uma
# frase que alguém lê em voz alta numa demo sem perder o fio.
MAX_ITEMS = 3


async def customer_snapshot(store: DataStore, customer: dict) -> dict[str, Any]:
    """O que existe HOJE para esta identidade, em uma passada.

    Uma leitura por coleção, todas filtradas por dono (mesmo isolamento das
    queries de negócio). Falha de qualquer uma degrada para lista vazia: a
    orientação é auxiliar, nunca pode derrubar o turno que a chamou.
    """
    owner = customer["customer_key"]
    snapshot: dict[str, Any] = {"orders": [], "invoices": [], "loyalty": None, "shipments": []}
    try:
        snapshot["orders"] = [
            public_document(item)
            for item in await store.find_many(
                "orders", {"owner_customer_key": owner}, limit=MAX_ITEMS, sort=[("order_id", -1)]
            )
        ]
    except Exception:  # noqa: BLE001 — orientação é auxiliar
        pass
    try:
        snapshot["invoices"] = [
            public_document(item)
            for item in await store.find_many(
                "invoices", {"owner_customer_key": owner}, limit=MAX_ITEMS, sort=[("due_date", -1)]
            )
        ]
    except Exception:  # noqa: BLE001
        pass
    try:
        loyalty = await store.find_one("loyalty_accounts", {"customer_key": owner})
        snapshot["loyalty"] = public_document(loyalty) if loyalty else None
    except Exception:  # noqa: BLE001
        pass
    try:
        snapshot["shipments"] = [
            public_document(item)
            for item in await store.find_many(
                "shipments", {"owner_customer_key": owner}, limit=MAX_ITEMS
            )
        ]
    except Exception:  # noqa: BLE001
        pass
    return snapshot


def _open_invoice(snapshot: dict) -> dict | None:
    return next((item for item in snapshot.get("invoices") or [] if item.get("status") == "aberta"), None)


def _in_transit(snapshot: dict) -> dict | None:
    """Entrega ainda em curso: o pedido correspondente não está entregue."""
    delivered = {
        order["order_id"] for order in snapshot.get("orders") or [] if order.get("status") == "entregue"
    }
    return next(
        (item for item in snapshot.get("shipments") or [] if item.get("order_id") not in delivered),
        None,
    )


def build_suggestions(snapshot: dict, *, exclude: set[str] | None = None) -> list[dict[str, str]]:
    """Sugestões clicáveis, cada uma com a mensagem exata que ela dispara.

    `exclude` remove os temas que o próprio turno já cobriu — sugerir ao cliente
    exatamente o que ele acabou de perguntar é pior que não sugerir nada.
    """
    skip = exclude or set()
    out: list[dict[str, str]] = []

    orders = snapshot.get("orders") or []
    if "order" not in skip and orders:
        order = orders[0]
        out.append({
            "topic": "order",
            "label": f"Status do {order['order_id']} ({order.get('product', 'seu pedido')})",
            "message": f"qual é o status do pedido {order['order_id']}?",
        })

    invoice = _open_invoice(snapshot)
    if "invoice" not in skip and invoice:
        out.append({
            "topic": "invoice",
            "label": f"Fatura {invoice['invoice_id']} em aberto",
            "message": f"qual é o valor e o vencimento da fatura {invoice['invoice_id']}?",
        })

    shipment = _in_transit(snapshot)
    if "shipment" not in skip and shipment:
        out.append({
            "topic": "shipment",
            "label": f"Entrega do {shipment['order_id']} ({shipment.get('carrier', 'transportadora')})",
            "message": f"onde está a entrega do pedido {shipment['order_id']}?",
        })

    loyalty = snapshot.get("loyalty")
    if "loyalty" not in skip and loyalty:
        out.append({
            "topic": "loyalty",
            "label": f"{loyalty.get('points', 0)} pontos ({loyalty.get('tier', 'seu plano')})",
            "message": "quantos pontos de fidelidade eu tenho e o que dá para resgatar?",
        })

    if "product" not in skip and orders:
        order = orders[0]
        out.append({
            "topic": "product",
            "label": f"Produto parecido com {order.get('product', 'o que comprei')}",
            "message": f"recomende um produto parecido com {order.get('product', '')} e mais barato".strip(),
        })

    return out[:4]


def format_options(snapshot: dict, *, exclude: set[str] | None = None) -> str:
    """As mesmas sugestões em prosa, para entrar na resposta do agente.

    Vai em markdown com o id em negrito: numa demo o cliente lê o id na tela e
    digita/clica em cima dele.
    """
    suggestions = build_suggestions(snapshot, exclude=exclude)
    if not suggestions:
        return ""
    linhas = "\n".join(f"- {item['label']}" for item in suggestions)
    return f"\n\nPosso ajudar agora com:\n{linhas}"


def no_data_reply(kind: str, snapshot: dict, *, identifier: str | None = None) -> str:
    """Resposta para 'procurei e não existe para esta identidade'.

    Diz o que aconteceu, por que aconteceu, e o que existe de fato — nessa ordem.
    O isolamento continua honesto: nada aqui revela documento de outro cliente, só
    lista o que pertence a quem perguntou.
    """
    # (artigo, substantivo, pronome) — concordância de gênero em português não é
    # cosmética aqui: "o fatura" na frente do cliente derruba a impressão de produto.
    rotulos = {
        "order": ("o", "pedido", "ele"),
        "invoice": ("a", "fatura", "ela"),
        "shipment": ("a", "entrega", "ela"),
        "loyalty": ("a", "conta de fidelidade", "ela"),
    }
    artigo, alvo, pronome = rotulos.get(kind, ("o", "registro", "ele"))
    citado = f" **{identifier}**" if identifier else ""
    cabeca = (
        f"Não encontrei {artigo} {alvo}{citado} vinculad{'a' if artigo == 'a' else 'o'} à sua identidade — "
        f"ou o número está diferente, ou {pronome} pertence a outro cadastro."
    )

    if kind == "order" and snapshot.get("orders"):
        itens = "\n".join(
            f"- **{item['order_id']}** — {item.get('product', 'produto')} ({item.get('status', 'status desconhecido')})"
            for item in snapshot["orders"]
        )
        return f"{cabeca}\n\nEstes são os pedidos que constam para você:\n{itens}\n\nQuer que eu abra algum deles?"

    if kind == "invoice" and snapshot.get("invoices"):
        itens = "\n".join(
            f"- **{item['invoice_id']}** — R$ {float(item.get('amount', 0)):.2f}, "
            f"vence em {item.get('due_date', '—')} ({item.get('status', '—')})"
            for item in snapshot["invoices"]
        )
        return f"{cabeca}\n\nEstas são as faturas do seu cadastro:\n{itens}\n\nQuer que eu detalhe alguma?"

    if kind == "shipment" and snapshot.get("shipments"):
        itens = "\n".join(
            f"- **{item['order_id']}** — {item.get('carrier', 'transportadora')}, "
            f"previsão {item.get('estimated_delivery', '—')}"
            for item in snapshot["shipments"]
        )
        return f"{cabeca}\n\nEstas são as entregas em andamento:\n{itens}\n\nQuer acompanhar alguma?"

    complemento = format_options(snapshot, exclude={kind})
    return f"{cabeca}{complemento}" if complemento else (
        f"{cabeca}\n\nSe você tiver o número em mãos, me mande que eu consulto na hora."
    )


GREETINGS = ("oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "e ai", "eai",
             "hey", "hello", "tudo bem", "opa", "alo", "alô", "menu", "ajuda", "help")


def _words(message: str) -> str:
    """Minúsculas, sem acento, pontuação virando espaço: "Bom dia, tudo bem?" → "bom dia tudo bem"."""
    folded = "".join(char for char in unicodedata.normalize("NFKD", message.lower()) if not unicodedata.combining(char))
    return " ".join("".join(c if c.isalnum() else " " for c in folded).split())


def is_greeting(message: str) -> bool:
    """Cumprimento/abertura de conversa — curto e sem pedido embutido."""
    text = _words(message)
    if len(text.split()) > 4:
        return False
    return any(text == _words(term) or text.startswith(_words(term) + " ") for term in GREETINGS)


CAPABILITY_QUESTIONS = ("o que voce sabe fazer", "o que voce faz", "o que voce pode fazer", "como voce pode me ajudar",
                        "no que voce pode me ajudar", "como pode me ajudar", "com o que voce ajuda", "quais sao suas funcoes",
                        "o que posso perguntar", "o que posso pedir", "para que voce serve")


def is_capabilities_question(message: str) -> bool:
    """"O que você faz?" é pergunta sobre o atendimento, não assunto alheio: responde com o que existe."""
    text = _words(message)
    return any(term in text for term in CAPABILITY_QUESTIONS)


def greeting_reply(snapshot: dict, *, customer: dict) -> str:
    """Abertura de conversa: recebe bem e já mostra o que existe para esta identidade.

    Um "oi" respondido com "isso está fora do meu escopo" é o pior primeiro contato
    possível — e é exatamente o que alguém testando a demo vai digitar primeiro.
    """
    nome = (customer.get("name") or customer.get("customer_key") or "").split()[0]
    saudacao = f"Olá, {nome}! " if nome else "Olá! "
    corpo = saudacao + "Sou o atendimento da loja e consigo resolver pedidos, entregas, faturas, garantia, fidelidade, suporte técnico e recomendação de produtos."
    opcoes = format_options(snapshot)
    return corpo + (opcoes or "\n\nMe conta o que você precisa que eu direciono ao time certo.")


THANKS = ("obrigado", "obrigada", "valeu", "vlw", "agradeco", "muito obrigado",
          "muito obrigada", "grato", "grata", "perfeito", "otimo", "show", "legal", "tchau", "ate mais")

META = ("voce e humano", "vc e humano", "voce e um robo", "vc e um robo", "voce e uma ia",
        "vc e uma ia", "voce e um bot", "vc e um bot", "quem e voce", "quem e vc",
        "voce e real", "que modelo voce", "qual modelo voce", "voce e o chatgpt")


def _norm_text(message: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", message.lower())
        if not unicodedata.combining(char)
    ).strip(" .!?,")


def is_thanks(message: str) -> bool:
    """Agradecimento/encerramento — mantém a conversa aberta sem repetir o menu inteiro."""
    text = _norm_text(message)
    return len(text.split()) <= 4 and any(text == term or text.startswith(term) for term in THANKS)


def is_meta_question(message: str) -> bool:
    """Pergunta sobre o próprio atendimento ('você é um robô?')."""
    text = _norm_text(message)
    return any(term in text for term in META)


def thanks_reply(snapshot: dict, *, customer: dict) -> str:
    nome = (customer.get("name") or "").split()[0]
    suggestions = build_suggestions(snapshot)
    extra = f" Se precisar de mais alguma coisa — {suggestions[0]['label'].lower()}, por exemplo — é só dizer." if suggestions else ""
    return f"Por nada{', ' + nome if nome else ''}! Fico à disposição.{extra}"


def meta_reply(snapshot: dict, *, customer: dict) -> str:
    """Honestidade sobre o que o sistema é. Mentir aqui custa a demo inteira.

    Vale explicar a arquitetura em uma frase: numa apresentação técnica, essa é a
    pergunta que o cliente faz de propósito para ver se o sistema se contradiz.
    """
    corpo = (
        "Sou um atendimento automatizado — um time de agentes de IA coordenados, cada um "
        "especializado num assunto (pedidos, entregas, faturas, garantia, fidelidade, suporte "
        "e catálogo). Toda resposta que eu dou vem de uma consulta ao banco de dados da loja, "
        "não de memória de modelo. Se você preferir falar com uma pessoa, é só pedir um atendente "
        "que eu abro um chamado."
    )
    return corpo + format_options(snapshot)


def out_of_scope_reply(snapshot: dict, *, customer: dict) -> str:
    """Mensagem fora do domínio de atendimento — o caso 'pergunta sem sentido'.

    Não responde "não sei". Diz o que este atendimento cobre e oferece o que essa
    identidade tem de concreto agora.
    """
    nome = (customer.get("name") or customer.get("customer_key") or "").split()[0]
    saudacao = f"{nome}, " if nome else ""
    corpo = (
        f"{saudacao}essa pergunta está fora do que eu consigo resolver por aqui — este atendimento cobre "
        "pedidos, entregas, faturas, garantia, fidelidade, suporte técnico e recomendação de produtos."
    )
    opcoes = format_options(snapshot)
    if opcoes:
        return corpo + opcoes
    return corpo + "\n\nMe diga o que você precisa em uma frase que eu direciono ao time certo."


PII_TAGS = ("[cpf]", "[cartao]", "[cartão]", "[email]", "[telefone]")


def looks_like_own_pii(masked_message: str) -> bool:
    """A mensagem bloqueada era só o cliente colando o próprio documento?"""
    return any(tag in (masked_message or "").lower() for tag in PII_TAGS)


def pii_block_reply(snapshot: dict) -> str:
    """Bloqueio por PII do próprio cliente: orienta, não acusa.

    A regra de segurança continua valendo — a mensagem não é processada. O que muda é
    de quem é a culpa no texto. "Você violou a política de segurança" para quem só colou
    o próprio CPF trata ingenuidade como ataque, e é o primeiro teste que qualquer pessoa
    faz numa demo ("vou mandar um CPF e ver o que acontece").
    """
    corpo = (
        "Por segurança eu não trabalho com CPF, cartão ou outros dados sensíveis por aqui — "
        "descartei essa mensagem sem processá-la. Você não precisa se identificar: eu já sei "
        "quem você é e consigo resolver tudo pelo número do pedido ou da fatura."
    )
    return corpo + format_options(snapshot)


def blocked_reply(snapshot: dict, *, base: str) -> str:
    """Bloqueio de guardrail + saída construtiva.

    O bloqueio continua sendo bloqueio — o texto não amolece nem negocia. O que a
    orientação faz é impedir que a conversa termine num muro: quem testou o limite
    de propósito vê o limite funcionando E o caminho de volta.
    """
    opcoes = format_options(snapshot)
    return base + (opcoes if opcoes else "")
