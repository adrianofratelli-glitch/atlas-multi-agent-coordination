from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from .budget import estimate_tokens
from .config import get_settings
from . import turn_classifier
from .database import DataStore, utcnow
from .memory import fold, should_extract
from .router import normalize

# Only catalog/KB content can be cached. The orchestrator must also pass
# ``cache_eligible=True`` after proving that the turn did not use customer memory,
# hand off, or write. Intent alone does not prove that an answer is stable.
GLOBAL_CACHE_INTENTS = frozenset({"recomendacao", "produto_similar", "suporte", "defeito"})
CACHE_POLICY = "stable_v1"

# Pedido de AÇÃO ou mensagem composta não é "pergunta genérica já respondida": o cache semântico (>= 0,80) não
# distingue "meu monitor não liga, o que faço?" de "meu monitor não liga; abra um chamado e depois recomende...".
# Frases sobre texto sem acento, com borda à esquerda (mesma técnica do portão de memória, sem regex).
_ACTION_PHRASES = (
    "chamado", "atendente", "escal", "abra ", "abrir ", "registr", "cancel", "reembols", "estorn", "resgat",
    "reagend", "agende", "trocar", "quero trocar", "depois ", "em seguida", "e volte", "e confirm",
)
# HIT semântico de uma mensagem MUITO mais longa que a pergunta guardada = pedido composto, não paráfrase.
MAX_LENGTH_RATIO = 1.75


def looks_like_action_request(message: str) -> bool:
    folded = fold(message)
    return any(f" {phrase}" in folded for phrase in _ACTION_PHRASES)


def is_compound_of(message: str, cached_question: str | None) -> bool:
    if not cached_question:
        return False
    return len(fold(message).split()) > MAX_LENGTH_RATIO * len(fold(cached_question).split()) + 2


@dataclass
class CascadeResult:
    hit: bool
    fonte: Literal["curto_prazo", "cache"] | None = None
    score: float | None = None
    answer: str | None = None
    active_agent: str | None = None
    timeline: list[dict] = field(default_factory=list)
    tokens_economizados: int = 0
    # Por que um HIT (ou a leitura) foi descartado: turno pessoal nunca vem do cache.
    scope: Literal["sessao", "customer", "global"] = "global"  # sem informação = o mais restrito
    personal_reason: Literal["frase", "classificador", "orcamento", "acao", "composta"] | None = None
    question_text: str | None = None  # a pergunta que gerou o HIT (para comparar tamanho com a mensagem atual)
    classifier: dict | None = None


async def cascade_lookup(store: DataStore, *, target: str, area: str, customer_key: str, session_id: str, message: str) -> CascadeResult:
    """Cascata com bypass de turno pessoal: uma resposta que depende da memória do cliente não vem do cache.

    1) portão de frases (grátis): turno pessoal nem consulta o cache; 2) num HIT, o classificador
    vetorial ainda pode descartá-lo (paráfrase que o portão não conhece). Num MISS nunca roda.

    Se o classificador NÃO consegue decidir (índice ausente, limiar não medido, erro), falha fechado só
    onde há risco de vazamento: HIT de escopo global é descartado; HIT da própria sessão/cliente segue,
    porque não sai do dono. Só um veredito "pessoal" decisivo descarta qualquer escopo.
    """
    if should_extract(message):
        return CascadeResult(hit=False, personal_reason="frase")
    if looks_like_action_request(message):
        return CascadeResult(hit=False, personal_reason="acao")
    result = await _cascade_lookup_raw(store, target=target, area=area, customer_key=customer_key, session_id=session_id, message=message)
    if not result.hit:
        return result
    if is_compound_of(message, result.question_text):
        return CascadeResult(hit=False, personal_reason="composta")
    verdict = await turn_classifier.classify(store, message)
    if verdict["personal"] and (not verdict["error"] or result.scope == "global"):
        return CascadeResult(hit=False, personal_reason="classificador", classifier=verdict)
    result.classifier = verdict
    return result


async def _cascade_lookup_raw(store: DataStore, *, target: str, area: str, customer_key: str, session_id: str, message: str) -> CascadeResult:
    """UMA consulta decide HIT/MISS antes do LLM: $vectorSearch em curto_prazo (filtrado por sessão, threshold
    permissivo — pega reformulação) $unionWith $vectorSearch em cache (sem filtro de sessão, threshold rígido —
    pergunta comum já respondida), cada ramo já filtrado pelo próprio threshold ANTES do union, senão um score
    de cache abaixo do seu threshold rígido podia vencer o sort só por ser maior que o corte do curto_prazo."""
    settings = get_settings()
    if store.memory:
        return await _cascade_lookup_fallback(store, target=target, area=area, customer_key=customer_key, session_id=session_id, message=message)

    pipeline = [
        {"$vectorSearch": {"index": "short_term_autoembed_v1", "path": "question_text", "query": {"text": message}, "model": "voyage-4", "filter": {"session_id": session_id, "customer_key": customer_key, "agent": target}, "numCandidates": 50, "limit": 5}},
        {"$addFields": {"score": {"$meta": "vectorSearchScore"}, "fonte": "curto_prazo"}},
        {"$match": {"score": {"$gte": settings.short_term_cache_threshold}}},
        {"$sort": {"score": -1}},
        {"$limit": 1},
        {
            "$unionWith": {
                "coll": "semantic_cache",
                "pipeline": [
                    {"$vectorSearch": {"index": "cache_autoembed_v1", "path": "question_text", "query": {"text": message}, "model": "voyage-4", "filter": {"$or": [{"scope": "global", "area": area, "agent": target}, {"scope": "customer", "customer_key": customer_key, "agent": target}]}, "numCandidates": 100, "limit": 50}},
                    {"$addFields": {"score": {"$meta": "vectorSearchScore"}, "fonte": "cache"}},
                    {"$match": {"cache_policy": CACHE_POLICY, "score": {"$gte": settings.global_cache_threshold}}},
                    {"$sort": {"score": -1}},
                    {"$limit": 1},
                ],
            }
        },
        {"$sort": {"score": -1}},
        {"$limit": 1},
    ]
    try:
        results = await store.aggregate("short_term_memory", pipeline)
    except Exception:
        # Search indexes são atualizados de forma assíncrona no Atlas. Durante rollout ou drift de definição,
        # mantém o atendimento disponível com match exato e os mesmos filtros de isolamento.
        return await _cascade_lookup_fallback(
            store,
            target=target,
            area=area,
            customer_key=customer_key,
            session_id=session_id,
            message=message,
        )
    if not results:
        # MISS vetorial não é a palavra final: texto LITERALMENTE idêntico tem que dar HIT
        # sempre. Medido neste índice, repetir a mesma pergunta pontua entre 0.8101 e
        # 0.9213 — frase curta fica na faixa baixa, então um corte fixo derruba a repetição
        # exata de perguntas curtas ("qual é o valor da fatura FAT-1001?" = 0.8101). O match
        # exato por question_norm é determinístico e fecha esse buraco sem afrouxar o corte
        # semântico, que continua protegendo contra pergunta parecida-mas-diferente.
        return await _cascade_lookup_fallback(
            store,
            target=target,
            area=area,
            customer_key=customer_key,
            session_id=session_id,
            message=message,
        )
    best = results[0]
    tokens = estimate_tokens(best.get("answer", ""))
    return CascadeResult(
        hit=True,
        fonte=best["fonte"],
        score=best["score"],
        answer=best.get("answer"),
        active_agent=best.get("active_agent", target),
        timeline=best.get("timeline", []),
        tokens_economizados=tokens,
        question_text=best.get("question_text"),
        scope="sessao" if best["fonte"] == "curto_prazo" else ("customer" if best.get("scope") == "customer" else "global"),
    )


async def _cascade_lookup_fallback(store: DataStore, *, target: str, area: str, customer_key: str, session_id: str, message: str) -> CascadeResult:
    """DEMO_MODE não tem índice de vetor real — sem embedding local pra simular cosine similarity, o HIT vira
    match exato de question_norm (mesmo contrato hit/fonte/score que o caminho real, score fixo em 1.0).
    Mantém curto_prazo antes de cache, mesma prioridade do caminho com Atlas."""
    question_norm = normalize(message)
    short = await store.find_one("short_term_memory", {"session_id": session_id, "customer_key": customer_key, "agent": target, "question_norm": question_norm, "expires_at": {"$gt": utcnow()}})
    if short:
        return CascadeResult(hit=True, fonte="curto_prazo", scope="sessao", score=1.0, answer=short.get("answer"), active_agent=short.get("active_agent", target), timeline=short.get("timeline", []), tokens_economizados=estimate_tokens(short.get("answer", "")))
    cached = await store.find_one("semantic_cache", {"agent": target, "customer_key": customer_key, "scope": "customer", "cache_policy": CACHE_POLICY, "question_norm": question_norm, "expires_at": {"$gt": utcnow()}})
    if cached:
        return CascadeResult(hit=True, fonte="cache", scope="customer", score=1.0, answer=cached.get("answer"), active_agent=cached.get("active_agent", target), timeline=cached.get("timeline", []), tokens_economizados=estimate_tokens(cached.get("answer", "")))
    if not cached:
        cached = await store.find_one("semantic_cache", {"agent": target, "area": area, "scope": "global", "cache_policy": CACHE_POLICY, "question_norm": question_norm, "expires_at": {"$gt": utcnow()}})
    if cached:
        return CascadeResult(hit=True, fonte="cache", score=1.0, answer=cached.get("answer"), active_agent=cached.get("active_agent", target), timeline=cached.get("timeline", []), tokens_economizados=estimate_tokens(cached.get("answer", "")))
    return CascadeResult(hit=False)


async def cascade_long_term_context(store: DataStore, *, customer_key: str, message: str) -> list[dict]:
    """MISS nos dois: puxa contexto de longo prazo (memória episódica do cliente) pro prompt —
    isso NÃO é resposta pronta, é input do LLM, por isso não conta como cache hit.

    Só entra no prompt o que o sistema escreveu (`kind == "episode"`: rótulos de intent/agente). Documentos
    legados com Pergunta/Resposta crua ficam de fora: eram texto digitado pelo cliente voltando ao prompt.
    Como o episódio é só rótulo, ele não torna a resposta dependente do cliente (não bloqueia o cache)."""
    settings = get_settings()
    limit = settings.long_term_memory_limit
    if store.memory:
        docs = await store.find_many("long_term_memory", {"customer_key": customer_key}, limit=limit * 4)
    else:
        pipeline = [
            {"$vectorSearch": {"index": "long_term_autoembed_v1", "path": "text", "query": {"text": message}, "model": "voyage-4", "filter": {"customer_key": customer_key}, "numCandidates": 50, "limit": limit * 4}},
            {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
        ]
        try:
            docs = await store.aggregate("long_term_memory", pipeline)
        except Exception:
            # Memória de longo prazo enriquece o prompt, mas não pode derrubar o turno se o índice estiver
            # construindo ou indisponível. A consulta comum continua isolada por customer_key.
            docs = await store.find_many("long_term_memory", {"customer_key": customer_key}, limit=limit * 4)
    return [doc for doc in docs if doc.get("kind") == "episode"][:limit]


async def cascade_store_episode(store: DataStore, *, customer_key: str, intent: str | None, agent: str) -> None:
    """Registra na memória de longo prazo QUE o cliente tratou de um assunto — não o que foi dito.

    O texto do episódio é montado só com rótulos do próprio sistema (intent, agente). Antes gravava
    "Pergunta/Resposta" crua, e essa memória volta ao prompt (`long_term_hint`): qualquer frase que o
    cliente digitasse, injeção incluída, virava contexto persistente de turnos futuros. Um episódio por
    (cliente, intent, agente), atualizado — a coleção não cresce a cada repetição.
    """
    label = intent or "geral"
    now = utcnow()
    await store.replace_one(
        "long_term_memory",
        {"customer_key": customer_key, "kind": "episode", "intent": label, "agent": agent},
        {"customer_key": customer_key, "kind": "episode", "intent": label, "agent": agent,
         "text": f"Cliente já foi atendido sobre '{label}' pelo agente {agent}.", "created_at": now},
        upsert=True,
    )


async def cascade_store_short_term(
    store: DataStore, *, target: str, area: str, customer_key: str, session_id: str,
    message: str, answer: str, timeline: list[dict], active_agent: str,
) -> None:
    """Registra o turno APENAS na memória de curto prazo.

    Existe para o caminho de cache HIT: a resposta já veio pronta, então não faz sentido
    regravá-la no cache — mas a memória de CURTO prazo é o registro da conversa, não do
    custo. Sem isto, um turno servido do cache não aparecia em short_term_memory: o painel
    ficava vazio depois de várias perguntas, e uma reformulação seguinte não achava nada
    na sessão (caía no corte mais rígido do cache e podia gastar LLM à toa).
    """
    now = utcnow()
    question_norm = normalize(message)
    await store.replace_one(
        "short_term_memory",
        {"session_id": session_id, "customer_key": customer_key, "agent": target, "question_norm": question_norm},
        {"session_id": session_id, "agent": target, "area": area, "customer_key": customer_key,
         "question_text": message, "question_norm": question_norm, "answer": answer,
         "active_agent": active_agent, "timeline": timeline, "created_at": now,
         "expires_at": now + timedelta(hours=24)},
        upsert=True,
    )


async def cascade_store_turn(
    store: DataStore,
    *,
    target: str,
    area: str,
    customer_key: str,
    session_id: str,
    intent: str | None,
    message: str,
    answer: str,
    timeline: list[dict],
    active_agent: str,
    cache_eligible: bool = False,
) -> Literal["frase", "classificador", "acao"] | None:
    """Grava sempre em curto_prazo e só promove respostas estáveis ao cache semântico.

    Devolve por que a promoção foi negada por ser turno pessoal ("frase" | "classificador"), ou None.
    O classificador vetorial só roda aqui quando a promoção aconteceria de fato.

    ``cache_eligible`` é opt-in: o caller precisa provar que o turno não dependeu de estado
    mutável do cliente, memória, handoff ou escrita. Mesmo quando elegível, o escopo global
    ainda exige uma intenção de catálogo/KB explicitamente permitida.
    """
    question_norm = normalize(message)
    now = utcnow()
    await store.replace_one(
        "short_term_memory",
        {"session_id": session_id, "customer_key": customer_key, "agent": target, "question_norm": question_norm},
        {"session_id": session_id, "agent": target, "area": area, "customer_key": customer_key, "question_text": message, "question_norm": question_norm, "answer": answer, "active_agent": active_agent, "timeline": timeline, "created_at": now, "expires_at": now + timedelta(hours=24)},
        upsert=True,
    )
    if not cache_eligible or intent not in GLOBAL_CACHE_INTENTS:
        return None
    if should_extract(message):
        return "frase"
    if looks_like_action_request(message):
        return "acao"
    verdict = await turn_classifier.classify(store, message)
    if verdict["personal"] and not verdict["error"]:
        return "classificador"
    await store.replace_one(
        "semantic_cache",
        {"agent": target, "customer_key": customer_key, "scope": "customer", "question_norm": question_norm},
        {"agent": target, "area": area, "customer_key": customer_key, "scope": "customer", "cache_policy": CACHE_POLICY, "question_text": message, "question_norm": question_norm, "answer": answer, "active_agent": active_agent, "timeline": timeline, "created_at": now, "expires_at": now + timedelta(hours=24)},
        upsert=True,
    )
    if verdict["error"]:
        return None  # sem veredito: o escopo do cliente já foi gravado; o global espera a calibração
    await store.replace_one(
        "semantic_cache",
        {"agent": target, "area": area, "scope": "global", "question_norm": question_norm},
        {"agent": target, "area": area, "scope": "global", "cache_policy": CACHE_POLICY, "question_text": message, "question_norm": question_norm, "answer": answer, "active_agent": active_agent, "timeline": timeline, "created_at": now, "expires_at": now + timedelta(hours=24)},
        upsert=True,
    )
    return None
