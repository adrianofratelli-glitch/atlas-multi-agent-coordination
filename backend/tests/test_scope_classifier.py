"""Classificador de escopo por embedding (in / out / chat): margem entre líder e segundo, faixa ambígua, e falha sem
veredito (nunca inventa decisão)."""

import pytest

from app import scope_classifier as sc
from app.config import Settings
from app.database import DataStore

CONFIG = {"active": True, "out_margin": 0.06, "in_margin": 0.04, "chat_margin": 0.05}


class Atlas:
    memory = False

    def __init__(self, rows=None, config=CONFIG, error=None):
        self.rows, self.config, self.error, self.pipelines = rows, config, error, []

    async def find_one(self, name, query, *, brain=False, session=None):
        assert (name, brain) == (sc.CONFIG_COLLECTION, True)
        return self.config

    async def aggregate(self, name, pipeline, *, brain=False):
        self.pipelines.append((name, pipeline, brain))
        if self.error:
            raise self.error
        return self.rows


def rows(i, o, c):
    return [{"label": "in", "score": i}, {"label": "in", "score": i - .1}, {"label": "out", "score": o},
            {"label": "out", "score": o - .1}, {"label": "chat", "score": c}]


@pytest.mark.parametrize("i,o,c,expected", [
    (0.70, 0.80, 0.60, "out"),      # líder out, margem 0,10 >= 0,06
    (0.80, 0.70, 0.60, "in"),       # líder in, margem 0,10 >= 0,04
    (0.60, 0.65, 0.75, "chat"),     # líder chat, margem 0,10 >= 0,05
    (0.76, 0.78, 0.60, "unsure"),   # líder out por 0,02 < 0,06: ambíguo
    (0.78, 0.76, 0.60, "unsure"),   # líder in por 0,02 < 0,04
    (0.60, 0.61, 0.62, "unsure"),   # três empatados
    (0.70, 0.76, 0.50, "out"),      # exatamente na borda de out (0,06)
    (0.74, 0.70, 0.50, "in"),       # exatamente na borda de in (0,04)
])
async def test_leader_must_beat_the_runner_up_by_its_own_margin(i, o, c, expected):
    out = await sc.classify(Atlas(rows(i, o, c)), "qualquer coisa")
    assert out["scope"] == expected and not out["error"] and out["method"] == "vector"


def test_each_label_has_its_own_threshold():
    best = {"in": 0.70, "out": 0.60, "chat": 0.20}
    assert sc.decide(best, {"in": 0.05, "out": 0.99, "chat": 0.99})[0] == "in"
    assert sc.decide(best, {"in": 0.20, "out": 0.01, "chat": 0.01})[0] == "unsure"   # limiar de "in" mais rígido


def test_decision_is_by_margin_not_by_absolute_score():
    assert sc.decide({"in": 0.55, "out": 0.65, "chat": 0.3}, {"in": .04, "out": .06, "chat": .05})[0] == "out"
    assert sc.decide({"in": 0.90, "out": 1.00, "chat": 0.3}, {"in": .04, "out": .06, "chat": .05})[0] == "out"


async def test_query_uses_the_scope_index_in_the_brain_db_with_a_neighbourhood():
    atlas = Atlas(rows(0.7, 0.8, 0.6))
    await sc.classify(atlas, "onde está meu pedido")
    name, pipeline, brain = atlas.pipelines[0]
    stage = pipeline[0]["$vectorSearch"]
    assert (name, brain, stage["index"], stage["path"]) == (sc.PROBES_COLLECTION, True, sc.PROBES_INDEX, sc.PROBES_PATH)
    assert stage["limit"] == sc.NEIGHBOURS and stage["query"] == {"text": "onde está meu pedido"}


@pytest.mark.parametrize("atlas", [
    Atlas(error=RuntimeError("índice fora do ar")),                                  # exceção
    Atlas(rows=[]),                                                                  # índice ausente: [] sem exceção
    Atlas(rows=rows(.7, .8, .6), config=None),                                       # limiar não medido
    Atlas(rows=rows(.7, .8, .6), config={"active": True, "out_margin": .06}),        # config incompleta
    Atlas(rows=[{"label": "??", "score": .9}, {"score": .8}]),                       # linhas malformadas
])
async def test_without_a_real_verdict_it_abstains_and_flags_the_error(atlas):
    out = await sc.classify(atlas, "onde está meu pedido")
    assert out["scope"] == "unsure" and out["error"]


async def test_a_label_missing_among_the_neighbours_is_bounded_by_the_last_neighbour_not_treated_as_zero():
    """"where is my order?": os 12 vizinhos são todos `in`. `out` ausente = mais distante que o último vizinho devolvido; usar 0
    superestimaria a margem. O teto conservador (o pior score devolvido) nunca deixa a margem parecer maior do que é."""
    neighbours = [{"label": "in", "score": s} for s in (.90, .85, .80, .75)]
    out = await sc.classify(Atlas(neighbours), "where is my order?")
    assert out["scope"] == "in" and not out["error"]
    assert out["out_score"] == pytest.approx(.75) and out["margin"] == pytest.approx(.15)   # 0,90 - 0,75, não 0,90 - 0
    tight = [{"label": "in", "score": s} for s in (.80, .79, .78, .77)]
    assert (await sc.classify(Atlas(tight), "x"))["scope"] == "unsure"                      # líder por só 0,03: ambíguo


async def test_demo_mode_abstains_so_the_word_list_keeps_working():
    out = await sc.classify(DataStore(Settings(demo_mode=True)), "where is my order?")
    assert out["scope"] == "unsure" and out["method"] == "fallback" and not out["error"]


def test_seed_probes_are_labelled_unique_and_cover_other_languages_and_typos():
    every = sc.IN_SCOPE_PROBES + sc.OUT_OF_SCOPE_PROBES + sc.CHAT_PROBES
    assert len(set(every)) == len(every)  # nenhum probe repetido, nem entre rótulos diferentes
    assert any("where is my order" in p for p in sc.IN_SCOPE_PROBES) and any("pedio" in p for p in sc.IN_SCOPE_PROBES)
    assert len(sc.IN_SCOPE_PROBES) >= 60 and len(sc.OUT_OF_SCOPE_PROBES) >= 50 and len(sc.CHAT_PROBES) >= 25


async def test_seed_scope_probes_is_idempotent_and_labels_all_three_sides():
    from seed_scope_probes import seed_probes
    store = DataStore(Settings(demo_mode=True))
    total = len(sc.IN_SCOPE_PROBES) + len(sc.OUT_OF_SCOPE_PROBES) + len(sc.CHAT_PROBES)
    assert await seed_probes(store) == total and await seed_probes(store) == 0
    docs = await store.find_many(sc.PROBES_COLLECTION, {}, brain=True, limit=1000)
    assert {d["label"] for d in docs} == {"in", "out", "chat"} and len(docs) == total
