"""Classificador de ESCOPO — "esta mensagem é assunto da loja?" — feito no MongoDB, sem lista de palavras.

Por quê: o vocabulário de domínio (`router.DOMAIN_VOCAB`) é uma lista de palavras em português. Tudo o que ela não previu
escorrega: "where is my order?" (inglês), erro de digitação, gíria, assunto novo. Embedding é bom exatamente nisso
(tópico), então a pergunta "é da loja?" sai de `$vectorSearch` em vez de `in`.

Como funciona (mesmo padrão de `turn_classifier`, `guardrails` e do cache):
  - `<brain>.scope_probes` guarda exemplos rotulados `in` (assunto da loja), `out` (alheio) e `chat` (saudação, agradecimento,
    "quem é você?"); o índice `scope_probes_vs` (autoEmbed voyage-4) os vetoriza. classify() pega os vizinhos mais próximos e
    compara o melhor score de cada rótulo.
  - Decide pela MARGEM entre o rótulo líder e o segundo colocado, não por score absoluto (varia com o tamanho da frase):
      margem do líder >= limiar desse rótulo  → decisivo: "in" | "out" | "chat"
      caso contrário                           → "unsure"  (faixa ambígua: quem decide é o LLM)
    "out" = recusa educada, 0 tokens de LLM; "chat" = boas-vindas; "in" = é da loja, mesmo sem palavra-chave.
  - Os limiares (`out_margin`, `in_margin`, `chat_margin`) vivem em `<brain>.scope_classifier_config` e são MEDIDOS por
    `calibrate_thresholds.py --only scope`
    contra `tests/data/situations.json` (split dev) — situações que o índice nunca viu, não os próprios probes.
    Escolha "precisão primeiro": cada rótulo só é decisivo acima do MAIOR erro medido dele, então nunca se chama de `out` um
    pedido real nem de `in` um assunto alheio; a dúvida vai para o LLM, que custa tokens só onde há dúvida.

Sem veredito (DEMO_MODE, índice ausente/vazio, config não medida, erro) devolve `unsure` com `error=True`/`method`:
quem chama mantém o comportamento anterior (lista de palavras). Nunca derruba o turno e nunca inventa uma decisão.
"""

import time

from .database import DataStore

PROBES_COLLECTION = "scope_probes"
CONFIG_COLLECTION = "scope_classifier_config"
PROBES_INDEX = "scope_probes_vs"
PROBES_PATH = "phrase"
NEIGHBOURS = 12
LABELS = ("in", "out", "chat")

# Exemplos que SEMEIAM o índice (não são o conjunto de teste; ver situations.json). Cobrem os 7 agentes, o mesmo pedido
# em inglês/espanhol, erro de digitação e gíria — e, do outro lado, o que um cliente digita que não é da loja.
IN_SCOPE_PROBES = [
    "onde está o meu pedido?", "qual o status da minha encomenda", "cadê meu pedido, já era pra ter chegado", "rastreio do pedido PED-1234",
    "meu pedido está atrasado", "ainda não recebi a minha compra", "quero trocar o produto que comprei", "preciso devolver o que recebi",
    "quero cancelar minha compra", "desisti do pedido, como cancelo?", "quero meu dinheiro de volta", "preciso de reembolso do pedido",
    "não recebi o produto e quero o estorno", "o item chegou quebrado, quero trocar", "chegou o produto errado",
    "qual o valor da minha fatura?", "quando vence o boleto", "preciso da segunda via da fatura", "quero a nota fiscal da compra",
    "cobraram duas vezes no meu cartão", "não reconheço essa cobrança", "o valor cobrado está errado",
    "me recomenda um fone de ouvido", "quais teclados vocês têm?", "preciso de um monitor bom", "quanto custa o mouse?",
    "tem algo mais barato que esse fone?", "qual a diferença entre esses dois monitores", "vocês têm carregador rápido?",
    "quero um presente até 300 reais", "meu fone não liga", "o teclado não conecta no computador", "o monitor está sem imagem",
    "o mouse parou de funcionar", "meu smartwatch não sincroniza", "quero falar com um atendente humano", "abre um chamado pra mim",
    "meu produto ainda está na garantia?", "o que a garantia cobre?", "quantos pontos de fidelidade eu tenho?", "quero resgatar meus pontos",
    "qual é o meu nível no programa de fidelidade", "qual a transportadora do meu pedido", "quero reagendar a entrega",
    "qual a previsão de entrega", "código de rastreamento do pedido", "quero ver o histórico das minhas compras",
    "pode mostrar meus dados cadastrais?", "quero ver o meu último pedido",
    "where is my order?", "I want a refund for my order", "my package never arrived", "I need to return this product",
    "how much is my invoice?", "can you recommend a headset?", "my keyboard is not working", "I want to talk to a human agent",
    "donde está mi pedido?", "quiero devolver el producto", "necesito un reembolso", "mi factura tiene un error",
    "ondi ta meu pedido", "meu pedio nao chegou", "quero trokar o fone", "nao recebi minha encomenda ainda pfv",
    "vcs tem fone bluetooth?", "manda a fatura de novo", "q dia chega meu pedido",
    "o roteador que comprei não conecta", "o notebook que adquiri não liga mais", "a placa de vídeo que comprei trava toda hora",
    "o controle que recebi veio com defeito", "a impressora que comprei aí não imprime", "o tablet que pedi não carrega",
    "meu fone pifou", "tô esperando meu pedido faz uma semana", "cancela aí meu pedido", "me passa pra um humano", "quero falar com atendente",
    "cobraram errado", "me devolve a grana", "quando chega minha encomenda", "veio quebrado", "não funciona mais",
]
OUT_OF_SCOPE_PROBES = [
    "qual é a temperatura hoje?", "vai chover amanhã?", "que horas são agora?", "qual a previsão do tempo para o fim de semana",
    "como está o trânsito na marginal", "que dia é hoje?",
    "qual a capital da França?", "quem foi o primeiro presidente do Brasil?", "quem ganhou a copa de 2010", "quantos planetas tem o sistema solar",
    "quem escreveu Dom Casmurro", "qual o maior rio do mundo", "quem é o atual presidente da França",
    "quanto é 15 vezes 23?", "me ajuda com meu dever de matemática", "traduza essa frase para o inglês", "escreva uma redação sobre meio ambiente",
    "resolva essa equação de segundo grau", "como faço para instalar o python?", "como programar um loop em javascript",
    "meu computador está lento, como formatar", "como configurar o wifi do roteador da operadora",
    "estou com dor de cabeça, o que tomo?", "como fazer dieta para emagrecer", "posso processar meu vizinho?", "vale a pena investir em ações agora",
    "como salvar meu relacionamento", "me conta uma piada", "escreve um poema sobre o mar", "me dá uma receita de bolo de cenoura",
    "me indica um filme para hoje", "qual a melhor música para relaxar", "recomenda um restaurante em São Paulo", "vou viajar para o Rio, o que visitar",
    "quero comprar um carro", "quanto custa um apartamento", "vocês vendem geladeira?", "quanto custa um iPhone", "quero pedir uma pizza",
    "qual o CNPJ da empresa", "qual o endereço da loja física", "qual o horário de funcionamento da matriz", "quem é o dono da loja",
    "vocês têm vagas de emprego", "quero fazer uma parceria comercial", "quero patrocínio para meu evento",
    "what is the weather today?", "tell me a joke", "who won the game last night", "what is the capital of Spain", "cuéntame un chiste",
    "cual es la capital de Italia", "que tiempo hace hoy",
    "kkkk", "asdfghjkl", "???", "batata", "teste", "hmm", "sei lá", "qual o sentido da vida", "você gosta de futebol?",
    "qual a temperatura agora", "tá frio hoje?", "vai fazer sol no domingo", "me indica uma música", "qual a melhor série da netflix",
    "lição de casa de física", "me ajuda com um trabalho da escola", "resume esse texto pra mim", "quem ganhou o jogo do flamengo",
    "resultado do jogo de ontem", "qual meu signo hoje", "me dá uma dica de livro", "onde comer pizza perto de mim", "como chegar no aeroporto",
    "como fazer um bolo", "como emagrecer rápido", "estou triste, me dá um conselho", "traduz isso pra espanhol", "quanto está o dólar hoje",
    "bitcoin vai subir?", "me conta uma história", "adivinha um número", "você tem namorada?",
    "me recomenda um restaurante bom", "qual podcast você indica", "me indica um app de organização", "recomenda uma série pra maratonar",
    "me sugere uma música nova", "qual livro você recomenda ler", "recomenda um jogo pro fim de semana", "me indica um curso online",
    "escreve uma carta de amor pra mim", "faz um discurso de formatura", "cria uma letra de música", "escreve um poema sobre a vida",
]


CHAT_PROBES = [
    "oi", "olá", "bom dia", "boa tarde, tudo bem?", "boa noite", "e aí, tudo certo?", "opa, tudo bem por aí?", "oi, como vai?",
    "hello", "hi there", "hola", "buenos días",
    "obrigado", "muito obrigada pela ajuda", "valeu, era só isso", "brigadão", "tchau, até mais", "thank you", "gracias",
    "você é um robô?", "vc é uma inteligência artificial?", "quem é você?", "com quem eu estou falando", "você é uma pessoa de verdade?",
    "o que você sabe fazer?", "como você pode me ajudar?", "para que serve esse atendimento", "me explica como funciona aqui",
    "o que posso perguntar pra você", "qual o seu nome", "quem são vocês", "who are you?", "what can you do?",
]


def _row_label(doc: dict) -> str | None:
    label = doc.get("label")
    return label if label in LABELS else None


async def _thresholds(store: DataStore) -> dict[str, float] | None:
    try:
        doc = await store.find_one(CONFIG_COLLECTION, {"active": True}, brain=True)
    except Exception:  # noqa: BLE001
        return None
    values = {label: (doc or {}).get(f"{label}_margin") for label in LABELS}
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values.values()):
        return None
    return {label: float(v) for label, v in values.items()}


def decide(best: dict[str, float], margins: dict[str, float]) -> tuple[str, float]:
    """Regra pura (testável sem Atlas). Devolve (escopo, margem do líder sobre o segundo)."""
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    (leader, top), (_, second) = ranked[0], ranked[1]
    margin = top - second
    return (leader if margin >= margins[leader] else "unsure"), margin


async def best_scores(store: DataStore, text: str) -> dict[str, float] | None:
    """Melhor score por rótulo entre os vizinhos, ou None se não deu para consultar (índice ausente/vazio, um lado só, erro).
    Não depende dos limiares — é o que a calibração mede."""
    try:
        docs = await store.aggregate(PROBES_COLLECTION, [
            {"$vectorSearch": {"index": PROBES_INDEX, "path": PROBES_PATH, "query": {"text": text}, "model": "voyage-4",
                               "numCandidates": 100, "limit": NEIGHBOURS}},
            {"$project": {"label": 1, "_id": 0, "score": {"$meta": "vectorSearchScore"}}},
        ], brain=True)
    except Exception:  # noqa: BLE001 — sem veredito, nunca derruba o turno
        return None
    scores: dict[str, list[float]] = {label: [] for label in LABELS}
    for doc in docs or []:
        label = _row_label(doc)
        if label and isinstance(doc.get("score"), (int, float)):
            scores[label].append(float(doc["score"]))
    returned = [x for v in scores.values() for x in v]
    if not returned:
        return None  # índice ausente/vazio devolve [] sem exceção: não é "alheio", é falta de informação
    # Rótulo ausente entre os vizinhos = mais distante que o ÚLTIMO devolvido. Usar esse teto (e não 0) mantém a margem
    # conservadora: ela nunca aparece maior do que é. ("where is my order?" traz 12 vizinhos, todos `in`.)
    floor = min(returned)
    return {label: max(v) if v else floor for label, v in scores.items()}


async def classify(store: DataStore, text: str) -> dict:
    """Devolve {scope: in|out|chat|unsure, margin, in_score, out_score, chat_score, method, error, latency_ms}.

    `method="fallback"`/`error=True` = sem veredito real; `scope` vem `unsure` e o chamador mantém o comportamento anterior."""
    started = time.perf_counter()

    def result(scope="unsure", **fields) -> dict:
        return {"scope": scope, "margin": None, "in_score": None, "out_score": None, "chat_score": None, "error": False,
                "method": "vector", "latency_ms": round((time.perf_counter() - started) * 1000), **fields}

    if store.memory:
        return result(method="fallback")  # DEMO_MODE/CI: sem $vectorSearch; a lista de palavras segue valendo
    thresholds = await _thresholds(store)
    best = await best_scores(store, text) if thresholds else None
    if thresholds is None or best is None:
        return result(error=True)
    scope, margin = decide(best, thresholds)
    return result(scope=scope, margin=round(margin, 4), in_score=round(best["in"], 4), out_score=round(best["out"], 4),
                  chat_score=round(best["chat"], 4))
