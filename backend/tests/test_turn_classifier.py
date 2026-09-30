"""Classificador de turno pessoal: caminho vetorial (Atlas simulado) e caminho de fallback do DEMO_MODE."""

import pytest

from app import turn_classifier as tc
from app.config import Settings
from app.database import DataStore


class FakeAtlas:
    """Imita o DataStore com Atlas: `memory=False`, agregação e config controladas pelo teste."""

    memory = False

    def __init__(self, probes=None, config=None, error=None):
        self.probes, self.config, self.error = probes, config, error
        self.aggregates: list[tuple[str, list, bool]] = []

    async def find_one(self, name, query, *, brain=False, session=None):
        assert (name, brain) == (tc.CONFIG_COLLECTION, True)
        return self.config

    async def aggregate(self, name, pipeline, *, brain=False):
        self.aggregates.append((name, pipeline, brain))
        if self.error:
            raise self.error
        return self.probes


CONFIG = {"active": True, "threshold": 0.72}


@pytest.mark.parametrize("score", [0.72, 0.85])
async def test_score_at_or_above_threshold_is_personal(score):
    atlas = FakeAtlas([{"phrase": "com qual nome você me trata?", "score": score}], CONFIG)
    out = await tc.classify(atlas, "como você me chama?")
    assert out["personal"] and not out["error"] and out["method"] == "vector"
    assert out["nearest"] == "com qual nome você me trata?" and out["threshold"] == 0.72


async def test_score_below_threshold_is_generic():
    out = await tc.classify(FakeAtlas([{"phrase": "x", "score": 0.6}], CONFIG), "como cancelo uma compra?")
    assert not out["personal"] and not out["error"]


async def test_query_targets_probe_index_in_ai_brain():
    atlas = FakeAtlas([{"phrase": "x", "score": 0.1}], CONFIG)
    await tc.classify(atlas, "oi")
    name, pipeline, brain = atlas.aggregates[0]
    stage = pipeline[0]["$vectorSearch"]
    assert (name, brain, stage["index"], stage["path"]) == (tc.PROBES_COLLECTION, True, tc.PROBES_INDEX, tc.PROBES_PATH)
    assert stage["query"] == {"text": "oi"} and stage["limit"] == 1


@pytest.mark.parametrize("atlas", [
    FakeAtlas(error=RuntimeError("índice fora do ar"), config=CONFIG),
    # índice inexistente no Atlas devolve lista vazia, sem exceção: não pode virar "genérico"
    FakeAtlas(probes=[], config=CONFIG),
    # sem limiar medido não há como decidir: fecha
    FakeAtlas(probes=[{"phrase": "x", "score": 0.0}], config=None),
    FakeAtlas(probes=[{"phrase": "x", "score": 0.99}], config={"active": True}),
])
async def test_every_uncertain_case_fails_closed(atlas):
    out = await tc.classify(atlas, "como você me chama?")
    assert out["personal"] and out["error"]


async def test_fallback_flags_paraphrase_of_a_probe_without_touching_atlas():
    store = DataStore(Settings(demo_mode=True))
    out = await tc.classify(store, "como você me chama mesmo, pode dizer?")
    assert out["personal"] and not out["error"] and out["method"] == "fallback"


async def test_fallback_keeps_generic_questions_generic():
    store = DataStore(Settings(demo_mode=True))
    for message in ("como parear o fone bluetooth?", "qual a política de troca?", "onde está meu pedido PED-1001?"):
        assert not (await tc.classify(store, message))["personal"]


def test_probes_are_unique_nonempty_and_cover_recall_and_preference():
    assert len(tc.PERSONAL_PROBES) == len(set(tc.PERSONAL_PROBES))
    assert all(p.strip() for p in tc.PERSONAL_PROBES) and len(tc.PERSONAL_PROBES) >= 30


async def test_seed_probes_is_idempotent():
    from seed_turn_probes import seed_probes
    store = DataStore(Settings(demo_mode=True))
    first = await seed_probes(store)
    assert first == len(tc.PERSONAL_PROBES)
    assert await seed_probes(store) == 0
    assert len(await store.find_many(tc.PROBES_COLLECTION, {}, brain=True, limit=1000)) == len(tc.PERSONAL_PROBES)
