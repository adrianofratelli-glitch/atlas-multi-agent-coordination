import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    target_agent: str | None
    source: str
    confidence: float


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in value if not unicodedata.combining(char))


def cheap_route(message: str, rules: Iterable[dict]) -> RouteDecision | None:
    """Resolve somente regras inequívocas; empates vão para o orquestrador."""
    text = normalize(message)
    scored: list[tuple[int, int, dict]] = []
    for rule in rules:
        seen_norms: set[str] = set()
        matches = 0
        for keyword in rule.get("keywords", []):
            keyword_norm = normalize(str(keyword))
            if keyword_norm in seen_norms:
                continue
            # Fronteira de palavra à direita só faz sentido quando a keyword TERMINA em
            # caractere de palavra ("troca" não pode casar "trocador"). Para prefixo de
            # identificador — "ped-", "fat-" — o lookahead nunca casa, porque o que vem
            # depois do hífen é dígito: "PED-1001" jamais bateu a keyword "PED-", que
            # ficou letra morta no seed. As mensagens de demo escondiam isso por sempre
            # trazerem a palavra "pedido" junto; digitar só o id caía no fallback.
            direita = r"(?!\w)" if keyword_norm[-1:].isalnum() else ""
            pattern = rf"(?<!\w){re.escape(keyword_norm)}{direita}"
            if re.search(pattern, text):
                matches += 1
                seen_norms.add(keyword_norm)
        if matches:
            scored.append((matches, int(rule.get("priority", 0)), rule))
    if not scored:
        return None
    # PRIORIDADE primeiro, contagem de keywords como desempate — nesta ordem.
    # Com a contagem na frente, uma intenção genérica que casa duas palavras ("pedido"
    # + "PED-") ultrapassava a específica que casa uma ("garantia"), e a mensagem
    # composta ia para o agente errado. A prioridade seedada existe exatamente para
    # dizer quem atende primeiro numa mensagem composta; ela não pode ser desempate.
    scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
    best = scored[0]
    competing = [item for item in scored if (item[1], item[0]) == (best[1], best[0])]
    targets = {item[2]["target_agent"] for item in competing}
    if len(targets) > 1:
        return None
    return RouteDecision(best[2]["intent"], best[2]["target_agent"], "rules", min(0.99, 0.7 + best[0] * 0.1))


FANOUT_ELIGIBLE = ("order_agent", "billing_agent")


def detect_fanout(message: str, rules: Iterable[dict]) -> list[str] | None:
    """Pattern 'Parallel Fan-Out': pedido composto ('status do pedido e quanto devo') dispara 2 agentes
    independentes ao mesmo tempo em vez de handoff sequencial — só entre agentes cujo trabalho é genuinamente
    independente (pedido/fatura). Se suporte/produto também tiverem sinal na mesma mensagem, ela tem uma
    dependência real de contexto (diagnóstico → recomendação → efetivação) e deve ir pela cadeia de handoff
    sequencial, não pelo fan-out — por isso aborta se qualquer agente fora do par elegível também bater."""
    text = normalize(message)
    hits: set[str] = set()
    for rule in rules:
        target = rule.get("target_agent")
        for keyword in rule.get("keywords", []):
            kw = normalize(str(keyword))
            direita = r"(?!\w)" if kw[-1:].isalnum() else ""
            pattern = rf"(?<!\w){re.escape(kw)}{direita}"
            if re.search(pattern, text):
                hits.add(target)
                break
    if hits - set(FANOUT_ELIGIBLE):
        return None
    return sorted(hits) if len(hits) >= 2 else None


CATEGORY_KEYWORDS = {
    "fone": "Áudio", "caixa": "Áudio", "som": "Áudio",
    "teclado": "Periféricos", "mouse": "Periféricos",
    "monitor": "Monitores", "tela": "Monitores",
    "webcam": "Vídeo", "camera": "Vídeo",
    "smartwatch": "Vestíveis", "relogio": "Vestíveis",
    "carregador": "Energia",
    "hub": "Conectividade",
    "ssd": "Armazenamento", "armazenamento": "Armazenamento",
    "mochila": "Acessórios",
}


# Vocabulário amplo do domínio de atendimento. Serve a UMA pergunta: "essa mensagem
# tem QUALQUER sinal de que fala com esta loja?". Não é roteamento — é o filtro que
# separa "pergunta legítima escrita de um jeito que as regras não pegaram" (vai pelo
# caminho normal) de "assunto aleatório / teste / texto sem sentido" (vira orientação).
# Deliberadamente generoso: falso-negativo aqui manda alguém para a orientação, que
# ainda é uma boa resposta; falso-positivo devolve o palpite genérico que queremos matar.
DOMAIN_VOCAB = (
    "pedido", "ped-", "compra", "comprei", "comprar", "encomenda", "entrega", "entregue",
    "enviar", "envio", "enviado", "rastreio", "rastreamento", "transportadora", "frete",
    "chegou", "chegar", "atraso", "atrasado", "prazo", "endereco", "reagendar",
    "fatura", "fat-", "cobranca", "cobrado", "boleto", "pagamento", "pagar", "cartao",
    "preco", "barato", "barata", "desconto", "vencimento", "parcela", "dinheiro",
    "produto", "catalogo", "recomenda", "recomendacao", "sugestao",
    "parecido", "parecida", "similar", "estoque", "disponivel",
    "troca", "trocar", "devolucao", "devolver", "devolv", "reembolso", "estornar", "estorno",
    "cancelar", "cancelamento", "garantia", "garantido", "defeito", "quebrado",
    "suporte", "conserto", "assistencia", "nao funciona",
    "nao liga", "nao conecta", "atendente", "chamado", "escalar", "reclamacao",
    "pontos", "fidelidade", "resgatar", "resgate", "milhas", "beneficio",
    "nota fiscal", "recibo", "encomendei", "minha loja", "custa", "quanto custa",
)
# Palavras genéricas demais para provar sozinhas que a mensagem é da loja ("me CONTA uma piada", "me AJUDA com meu
# dever", "qual a MARCA do carro"). Sozinhas NÃO liberam o palpite genérico: só o classificador (LLM) pode decidir; sem
# ele a resposta é a orientação de escopo. Casadas por palavra inteira, não por pedaço ("conta" não casa "contar").
WEAK_VOCAB = ("ajuda", "conta", "problema", "valor", "modelo", "marca", "opcao", "opcoes", "cadastro",
              "caro", "pago", "tier", "loja")


def _padded(message: str) -> str:
    """Texto sem acento e sem pontuação, com espaço nas bordas: permite casar palavra inteira sem regex."""
    from .memory import fold
    return fold(message)


def has_domain_signal(message: str) -> bool:
    """True quando a mensagem tem sinal FORTE de que fala com esta loja."""
    text = normalize(message)
    return any(term in text for term in DOMAIN_VOCAB)


def has_weak_signal(message: str) -> bool:
    """Só palavras genéricas (ajuda, conta, valor...) — inconclusivo sem o classificador."""
    padded = _padded(message)
    return any(f" {term} " in padded for term in WEAK_VOCAB)


def out_of_scope_sentences(message: str) -> list[str]:
    """Numa mensagem MISTA (várias frases, ao menos uma da loja), as frases sem nenhum sinal de domínio.

    Só fatia em pontuação de frase (? ; . !) — nunca em "e"/"depois": "quero trocar o fone e receber o dinheiro" é
    uma frase só. Mensagem toda fora do escopo (nenhuma frase da loja) devolve [] (é outro caminho)."""
    sentences = [part.strip() for part in message.replace("?", "\n").replace(";", "\n").replace(".", "\n").replace("!", "\n").splitlines()]
    sentences = [part for part in sentences if len(part.split()) >= 3]
    if len(sentences) < 2 or not any(has_domain_signal(part) for part in sentences):
        return []
    return [part for part in sentences if not has_domain_signal(part) and not has_weak_signal(part)][:2]


def has_catalog_anchor(message: str) -> bool:
    """A mensagem cita algo do catálogo (categoria de produto, "produto", "catálogo")?

    A regra seedada `recomendacao` casa "recomenda" sozinho, então "me recomenda um filme" ganha rota de `product_agent`. Sem uma
    âncora de catálogo, a rota de produto se apoia só no verbo genérico e pode ser recusada pelo classificador de escopo."""
    words = _padded(message).split()
    return any(word.startswith(key) for word in words for key in (*CATEGORY_KEYWORDS, "produto", "catalogo"))


def deterministic_orchestrator(message: str) -> RouteDecision:
    text = normalize(message)
    if any(word in text for word in ("defeito", "quebrado", "nao funciona", "suporte", "nao conecta", "nao liga", "como resolvo")):
        return RouteDecision("suporte_tecnico", "support_agent", "orchestrator", 0.82)
    if any(word in text for word in ("produto", "parecido", "parecida", "recomenda", "mais barato", "mais barata")) or any(
        keyword in text for keyword in CATEGORY_KEYWORDS
    ):
        return RouteDecision("recomendacao", "product_agent", "orchestrator", 0.78)
    if any(word in text for word in ("fatura", "cobranca", "boleto")):
        return RouteDecision("cobranca", "billing_agent", "orchestrator", 0.82)
    return RouteDecision("pedido", "order_agent", "fallback", 0.55)

