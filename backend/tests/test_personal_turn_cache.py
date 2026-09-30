"""Turno pessoal não lê nem grava o cache semântico; classificador só no HIT e antes de gravar."""

from app.cascade import cascade_lookup, cascade_store_turn
from app.config import Settings
from app.database import DataStore
from app import turn_classifier as tc

KW = dict(target="support_agent", area="varejo", customer_key="ana", session_id="s1")
GENERIC = "como parear o fone bluetooth?"
PERSONAL_BY_PHRASE = "como você me chama mesmo?"  # portão de frases pega ("me chama")
PARAPHRASE = "com qual nome você costuma se dirigir a mim?"  # nenhuma frase do portão; só o classificador pega


class Atlas:
    """Atlas simulado: short_term devolve um HIT; turn_probes devolve o score que o teste definir."""

    memory = False

    def __init__(self, probe_score=0.5, probe_error=None, config=True):
        self.probe_score, self.probe_error, self.has_config = probe_score, probe_error, config
        self.calls: list[str] = []
        self.written: list[tuple[str, dict]] = []

    async def find_one(self, name, query, *, brain=False, session=None):
        if name == tc.CONFIG_COLLECTION:
            return {"active": True, "threshold": 0.72} if self.has_config else None
        return None

    async def aggregate(self, name, pipeline, *, brain=False):
        self.calls.append(name)
        if name == tc.PROBES_COLLECTION:
            if self.probe_error:
                raise self.probe_error
            return [{"phrase": "p", "score": self.probe_score}]
        return [{"fonte": "cache", "score": 0.95, "answer": "resposta pronta", "active_agent": "support_agent", "timeline": []}]

    async def replace_one(self, name, query, document, *, brain=False, upsert=False):
        self.written.append((name, document))


def cached(atlas):
    return [name for name, _ in atlas.written if name == "semantic_cache"]


# ---- leitura ----

async def test_phrase_gate_skips_cache_read_entirely_and_costs_nothing():
    atlas = Atlas()
    result = await cascade_lookup(atlas, message=PERSONAL_BY_PHRASE, **KW)
    assert not result.hit and result.personal_reason == "frase"
    assert atlas.calls == []  # nem cache nem classificador: o portão é grátis


async def test_hit_is_discarded_when_classifier_says_personal():
    atlas = Atlas(probe_score=0.80)
    result = await cascade_lookup(atlas, message=PARAPHRASE, **KW)
    assert not result.hit and result.personal_reason == "classificador"
    assert result.classifier["score"] == 0.8


async def test_hit_is_served_when_classifier_says_generic():
    atlas = Atlas(probe_score=0.55)
    result = await cascade_lookup(atlas, message=GENERIC, **KW)
    assert result.hit and result.answer == "resposta pronta" and result.personal_reason is None
    assert atlas.calls == ["short_term_memory", "turn_probes"]  # classificador roda DEPOIS do HIT


async def test_classifier_error_on_hit_fails_closed():
    result = await cascade_lookup(Atlas(probe_error=RuntimeError("fora")), message=GENERIC, **KW)
    assert not result.hit and result.personal_reason == "classificador"


async def test_uncalibrated_classifier_fails_closed_rather_than_serving():
    result = await cascade_lookup(Atlas(config=False), message=GENERIC, **KW)
    assert not result.hit


async def test_miss_never_pays_for_classifier():
    class NoHit(Atlas):
        async def aggregate(self, name, pipeline, *, brain=False):
            self.calls.append(name)
            return []
    atlas = NoHit()
    store = DataStore(Settings(demo_mode=True))
    atlas.find_one = store.find_one  # fallback exato consulta o store; sem entrada = MISS
    result = await cascade_lookup(atlas, message=GENERIC, **KW)
    assert not result.hit and tc.PROBES_COLLECTION not in atlas.calls


# ---- escrita ----

async def store_turn(atlas, message, **over):
    args = dict(intent="suporte", cache_eligible=True, **KW) | over
    return await cascade_store_turn(atlas, message=message, answer="a", timeline=[], active_agent="support_agent", **args)


async def test_generic_turn_is_cached_after_classifier_clears_it():
    atlas = Atlas(probe_score=0.5)
    assert await store_turn(atlas, GENERIC) is None
    assert len(cached(atlas)) == 2  # escopo cliente + global


async def test_phrase_gate_blocks_cache_write_but_keeps_conversation_record():
    atlas = Atlas()
    assert await store_turn(atlas, PERSONAL_BY_PHRASE) == "frase"
    assert cached(atlas) == [] and [n for n, _ in atlas.written] == ["short_term_memory"]
    assert atlas.calls == []


async def test_classifier_blocks_cache_write_of_paraphrase():
    atlas = Atlas(probe_score=0.9)
    assert await store_turn(atlas, PARAPHRASE) == "classificador"
    assert cached(atlas) == []


async def test_classifier_error_keeps_customer_scope_but_never_writes_global():
    atlas = Atlas(probe_error=RuntimeError("fora"))
    assert await store_turn(atlas, GENERIC) is None
    scopes = [doc["scope"] for name, doc in atlas.written if name == "semantic_cache"]
    assert scopes == ["customer"]  # o global espera o classificador decidir


async def test_classifier_not_called_when_turn_was_never_going_to_be_cached():
    atlas = Atlas()
    await store_turn(atlas, GENERIC, cache_eligible=False)
    await store_turn(atlas, GENERIC, intent="status_pedido")
    assert atlas.calls == []


# ---- DEMO_MODE (armazenamento em memória, sem $vectorSearch) ----

async def test_demo_mode_personal_turn_never_reads_or_writes_shared_cache():
    store = DataStore(Settings(demo_mode=True))
    assert await store_turn(store, PERSONAL_BY_PHRASE) == "frase"
    assert await store.find_many("semantic_cache", {}) == []
    other = await cascade_lookup(store, message=PERSONAL_BY_PHRASE, **{**KW, "customer_key": "bruno", "session_id": "s9"})
    assert not other.hit


async def test_demo_mode_fallback_classifier_blocks_probe_paraphrase_and_lets_generic_through():
    store = DataStore(Settings(demo_mode=True))
    assert await store_turn(store, "que informações minhas você tem guardadas mesmo?") == "classificador"
    assert await store_turn(store, GENERIC) is None
    assert len(await store.find_many("semantic_cache", {})) == 2
    hit = await cascade_lookup(store, message=GENERIC, **{**KW, "customer_key": "bruno", "session_id": "s9"})
    assert hit.hit and hit.fonte == "cache"


# ---- sem veredito: fecha só onde há risco de vazamento (o escopo global) ----

class Scoped(Atlas):
    def __init__(self, fonte, scope, **kw):
        super().__init__(**kw)
        self.fonte, self.scope = fonte, scope

    async def aggregate(self, name, pipeline, *, brain=False):
        rows = await super().aggregate(name, pipeline, brain=brain)
        if name != tc.PROBES_COLLECTION:
            rows[0].update(fonte=self.fonte, scope=self.scope)
        return rows


async def test_undecided_classifier_still_serves_session_and_customer_hits():
    for fonte, scope in (("curto_prazo", None), ("cache", "customer")):
        result = await cascade_lookup(Scoped(fonte, scope, probe_error=RuntimeError("fora")), message=GENERIC, **KW)
        assert result.hit and result.classifier["error"], (fonte, scope)


async def test_undecided_classifier_discards_global_hits():
    result = await cascade_lookup(Scoped("cache", "global", probe_error=RuntimeError("fora")), message=GENERIC, **KW)
    assert not result.hit and result.personal_reason == "classificador"


async def test_decisive_personal_verdict_discards_even_customer_scope():
    result = await cascade_lookup(Scoped("cache", "customer", probe_score=0.9), message=PARAPHRASE, **KW)
    assert not result.hit and result.personal_reason == "classificador"


# ---- pedido de AÇÃO / mensagem composta nunca sai do cache semântico ----
# Incidente real (eval live): "meu Monitor View 27 não liga; abra um chamado ... e depois recomende ..." casou
# (>= 0,80) com a pergunta aquecida "meu monitor não liga, o que devo fazer?" e recebeu a resposta genérica, sem chamado.

COMPOUND = "meu Monitor View 27 não liga; abra um chamado para um atendente e depois recomende um monitor parecido"


async def test_action_request_bypasses_the_cache_without_any_lookup():
    atlas = Atlas()
    result = await cascade_lookup(atlas, message=COMPOUND, **KW)
    assert not result.hit and result.personal_reason == "acao" and atlas.calls == []


async def test_action_request_is_never_written_to_the_shared_cache():
    atlas = Atlas()
    assert await store_turn(atlas, COMPOUND) == "acao"
    assert cached(atlas) == []


async def test_much_longer_message_than_the_cached_question_is_discarded_even_without_action_words():
    class Short(Atlas):
        async def aggregate(self, name, pipeline, *, brain=False):
            rows = await super().aggregate(name, pipeline, brain=brain)
            if name != tc.PROBES_COLLECTION:
                rows[0]["question_text"] = "meu monitor não liga, o que devo fazer?"
            return rows
    long_message = "meu monitor não liga e eu já testei o cabo e a tomada e também outro computador e continua igual, e agora, o que devo fazer?"
    result = await cascade_lookup(Short(probe_score=0.5), message=long_message, **KW)
    assert not result.hit and result.personal_reason == "composta"


async def test_genuine_paraphrase_of_similar_length_is_still_served():
    class Short(Atlas):
        async def aggregate(self, name, pipeline, *, brain=False):
            rows = await super().aggregate(name, pipeline, brain=brain)
            if name != tc.PROBES_COLLECTION:
                rows[0]["question_text"] = "meu fone chegou com defeito, o que eu faço?"
            return rows
    result = await cascade_lookup(Short(probe_score=0.5), message="meu fone veio com defeito, como resolvo?", **KW)
    assert result.hit
