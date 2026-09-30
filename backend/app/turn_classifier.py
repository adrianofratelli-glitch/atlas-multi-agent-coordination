"""Classificador "este turno depende da memória DESTE cliente?" — feito no MongoDB.

Uma pergunta que só faz sentido com a memória do cliente ("como você me chama?", "qual é o meu
limite?") nunca pode ser servida do cache semântico compartilhado nem gravada nele. O portão de
frases de `memory.should_extract` pega o óbvio de graça; este classificador cobre paráfrases que
nenhuma lista antecipa.

  - `ai_brain.turn_probes` guarda frases-exemplo de turnos pessoais; o índice `turn_probes_vs`
    (autoEmbed voyage-4) as vetoriza. classify() faz `$vectorSearch` e compara o vizinho mais
    próximo com o limiar.
  - O limiar vive em `ai_brain.turn_classifier_config` e é MEDIDO por `calibrate_thresholds.py
    --apply` contra probes de teste distintos dos semeados. Não existe valor padrão: sem limiar
    medido não há como decidir.
  - Roda só quando importa: num HIT (antes de servir) e antes de gravar no cache.

FALHA FECHADO: erro, índice inexistente/vazio (o Atlas devolve lista vazia, sem exceção) ou limiar
ausente ⇒ turno tratado como pessoal (sem cache). Custa uma chamada ao LLM, nunca um vazamento.

DEMO_MODE não tem `$vectorSearch`: cai numa sobreposição de palavras (Jaccard) contra os probes.
É mais fraco por construção — só o caminho vetorial separa paráfrase de pergunta legítima.
"""

import time

from .database import DataStore
from .memory import fold

PROBES_COLLECTION = "turn_probes"
CONFIG_COLLECTION = "turn_classifier_config"
PROBES_INDEX = "turn_probes_vs"
PROBES_PATH = "phrase"
FALLBACK_OVERLAP = 0.6  # só DEMO_MODE/CI; não é o limiar medido do Atlas

PERSONAL_PROBES = [
    "qual é o meu nome?",
    "você sabe como eu gosto de ser chamado?",
    "o que você lembra a meu respeito?",
    "me diga o que tem salvo no meu cadastro",
    "qual é o meu orçamento máximo?",
    "você guardou as minhas preferências?",
    "como eu prefiro ser contatado?",
    "lembra do que te pedi antes?",
    "qual é o meu limite de gasto?",
    "o que eu já te contei sobre mim?",
    "com qual nome você me trata?",
    "você lembra do meu canal preferido de contato?",
    "já te disse qual é o meu apelido?",
    "que informações minhas você tem guardadas?",
    "considerando o que você sabe de mim, o que me indica?",
    "me sugira algo dentro do meu limite de preço",
    "recomende algo que combine com o meu gosto",
    "com base nas minhas preferências, o que devo comprar?",
    "tenho preferência por contato no whatsapp",
    "só quero ser avisado por e-mail",
    "mude a forma como você me trata",
    "atualize o meu apelido",
    "esqueça o que eu disse sobre o meu orçamento",
    "anote isso sobre mim para as próximas conversas",
    "guarde essa informação para os próximos atendimentos",
    "como você me chama mesmo?",
    "qual apelido eu te passei?",
    "você tem o meu perfil de compras salvo?",
    "o que você registrou das minhas preferências?",
    "lembra qual era o meu teto de preço?",
    "pode repetir o que eu pedi para você anotar?",
    "a partir de agora fale comigo por whatsapp",
    "prefiro ser atendido por telefone",
    "não me ligue, só mensagem",
    "me trate pelo primeiro nome",
    "não quero receber promoções",
    "me avise quando o preço baixar",
    "ajuste as recomendações ao meu perfil",
    "mostre o que você sabe do meu histórico",
    "apague o que você guardou sobre mim",
    "me lembra o que a gente combinou antes",
    "qual valor a gente tinha combinado?",
    "o que ficou combinado entre nós sobre o meu orçamento?",
    "o que ficou registrado sobre as minhas preferências?",
]


def _overlap(left: str, right: str) -> float:
    a, b = set(fold(left).split()), set(fold(right).split())
    return len(a & b) / max(1, len(a | b))


async def _threshold(store: DataStore) -> float | None:
    try:
        doc = await store.find_one(CONFIG_COLLECTION, {"active": True}, brain=True)
    except Exception:  # noqa: BLE001 — sem config o turno fecha, não derruba
        return None
    value = (doc or {}).get("threshold")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def classify(store: DataStore, text: str) -> dict:
    """Devolve {personal, score, threshold, nearest, method, error, latency_ms}.

    `error=True` significa que não foi possível decidir; `personal` já vem True nesse caso.
    """
    started = time.perf_counter()

    def result(**fields) -> dict:
        return {"threshold": None, "nearest": None, "score": 0.0, "error": False,
                "latency_ms": round((time.perf_counter() - started) * 1000), **fields}

    if store.memory:
        best_phrase, best = max(((p, _overlap(text, p)) for p in PERSONAL_PROBES), key=lambda pair: pair[1])
        return result(personal=best >= FALLBACK_OVERLAP, score=round(best, 4), threshold=FALLBACK_OVERLAP,
                      nearest=best_phrase, method="fallback")

    threshold = await _threshold(store)
    if threshold is None:
        return result(personal=True, error=True, method="vector")
    try:
        docs = await store.aggregate(PROBES_COLLECTION, [
            {"$vectorSearch": {"index": PROBES_INDEX, "path": PROBES_PATH, "query": {"text": text},
                               "model": "voyage-4", "numCandidates": 50, "limit": 1}},
            {"$project": {"phrase": 1, "_id": 0, "score": {"$meta": "vectorSearchScore"}}},
        ], brain=True)
    except Exception:  # noqa: BLE001 — fail-closed
        return result(personal=True, error=True, threshold=threshold, method="vector")
    if not docs:
        # índice ausente/vazio: o Atlas não levanta exceção, devolve []. Não é "genérico".
        return result(personal=True, error=True, threshold=threshold, method="vector")
    score = float(docs[0]["score"])
    return result(personal=score >= threshold, score=round(score, 4), threshold=threshold,
                  nearest=docs[0].get("phrase"), method="vector")
