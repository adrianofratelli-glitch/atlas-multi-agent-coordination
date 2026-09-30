"""Calibração medida: escolha do limiar com erros, seleção de alvos (--only) e probes de teste separados."""

import pytest

import calibrate_thresholds as cal
from app import turn_classifier as tc
from app.config import Settings
from app.database import DataStore
from app.memory import fold


# ---- limiar de menor erro ----

def test_outlier_positive_is_reported_not_hidden():
    pos = [(0.80, "p1"), (0.78, "p2"), (0.67, "outlier")]
    neg = [(0.70, "n1"), (0.69, "n2"), (0.68, "n3")]
    threshold, missed, false_alarms = cal.best_with_errors(pos, neg)
    assert 0.70 < threshold < 0.78 and missed == ["outlier"] and false_alarms == []


def test_tie_prefers_fewer_false_negatives():
    # Passar turno pessoal (FN) é pior que pular o cache (FP): no empate, tolera FP.
    threshold, missed, false_alarms = cal.best_with_errors([(0.60, "p_low"), (0.90, "p_hi")], [(0.55, "n_low"), (0.70, "n_mid")])
    assert missed == [] and false_alarms == ["n_mid"] and 0.55 < threshold < 0.60


def test_perfect_separation_has_no_errors():
    threshold, missed, false_alarms = cal.best_with_errors([(0.8, "p")], [(0.6, "n")])
    assert (missed, false_alarms) == ([], []) and 0.6 < threshold < 0.8


# ---- probes de teste distintos dos semeados ----

def test_turn_test_probes_are_not_the_seeded_ones():
    seeded = {fold(p) for p in tc.PERSONAL_PROBES}
    assert not [text for _, text, _ in cal.TURN_PROBES if fold(text) in seeded]


def test_turn_test_probes_have_both_labels_and_generic_ones_avoid_the_phrase_gate():
    from app.memory import should_extract
    assert {label for label, _, _ in cal.TURN_PROBES} == {True, False}
    # negativo que o portão de frases já pegaria não mede o classificador
    assert not [t for label, t, _ in cal.TURN_PROBES if not label and should_extract(t)]


# ---- --only / --allow-errors ----

def test_default_targets_are_all_and_only_narrows():
    parser = cal.build_parser()
    assert cal.selected_targets(parser.parse_args([])) == {"denylist", "turn", "block", "scope"}
    args = parser.parse_args(["--only", "turn", "--allow-errors", "--apply"])
    assert cal.selected_targets(args) == {"turn"} and args.allow_errors and args.apply


def test_unknown_target_is_rejected():
    with pytest.raises(SystemExit):
        cal.build_parser().parse_args(["--only", "cache"])


# ---- calibrate() contra um índice simulado ----

class ScoredAtlas:
    """aggregate() devolve o score que o teste atribuiu ao texto consultado."""

    memory = False

    def __init__(self, scores):
        self.scores, self.pipelines = scores, []

    async def aggregate(self, name, pipeline, *, brain=False):
        self.pipelines.append((name, pipeline, brain))
        return [{"score": self.scores[pipeline[0]["$vectorSearch"]["query"]["text"]]}]


PROBES = [(True, "pos-alto", None), (True, "pos-baixo", None), (False, "neg-alto", None), (False, "neg-baixo", None)]


async def test_perfect_separation_suggests_midpoint_and_measures_the_brain_collection():
    atlas = ScoredAtlas({"pos-alto": .9, "pos-baixo": .8, "neg-alto": .6, "neg-baixo": .5})
    out = await cal.calibrate(atlas, tc.PROBES_COLLECTION, tc.PROBES_INDEX, tc.PROBES_PATH, PROBES, "t", brain=True)
    assert out == pytest.approx(.7)
    assert atlas.pipelines[0][2] is True and "filter" not in atlas.pipelines[0][1][0]["$vectorSearch"]


async def test_overlap_is_not_written_without_allow_errors_but_is_with_it():
    atlas = ScoredAtlas({"pos-alto": .9, "pos-baixo": .6, "neg-alto": .7, "neg-baixo": .5})
    args = (atlas, tc.PROBES_COLLECTION, tc.PROBES_INDEX, tc.PROBES_PATH, PROBES, "t")
    assert await cal.calibrate(*args, brain=True) is None
    # positivos {.9,.6} negativos {.7,.5}: 1 erro em qualquer corte; o empate favorece menos FN (corte .55, 1 FP)
    assert await cal.calibrate(*args, brain=True, allow_errors=True) == pytest.approx(.55)


# ---- gravação do limiar (também em DEMO_MODE, onde não há Atlas) ----

async def test_apply_turn_threshold_upserts_the_single_active_config():
    store = DataStore(Settings(demo_mode=True))
    await cal.apply_turn_threshold(store, .7162)
    await cal.apply_turn_threshold(store, .73)
    docs = await store.find_many(tc.CONFIG_COLLECTION, {"active": True}, brain=True)
    assert len(docs) == 1 and docs[0]["threshold"] == .73
    assert docs[0]["calibration"]["method"] == "backend/calibrate_thresholds.py"
    assert await tc._threshold(store) == .73


def test_block_threshold_sits_just_above_the_highest_legit_score():
    assert cal.block_threshold_from([0.70, 0.8664, 0.75]) == pytest.approx(0.8814)


def test_legit_probes_are_all_distinct_and_nonempty_and_cover_refund_and_data_requests():
    assert len(cal.LEGIT_CS_PROBES) == len(set(cal.LEGIT_CS_PROBES)) and len(cal.LEGIT_CS_PROBES) >= 30
    joined = " ".join(cal.LEGIT_CS_PROBES)
    assert "dinheiro de volta" in joined and "dados do meu pedido" in joined


def test_block_target_is_selectable():
    assert cal.selected_targets(cal.build_parser().parse_args(["--only", "block"])) == {"block"}


def test_scope_thresholds_sit_above_the_worst_measured_mistake_per_label():
    from app import scope_classifier as sc
    items = [("in", {"in": .80, "out": .60, "chat": .30}), ("in", {"in": .70, "out": .60, "chat": .30}),
             ("out", {"in": .60, "out": .78, "chat": .30}), ("out", {"in": .64, "out": .70, "chat": .30}),
             ("chat", {"in": .40, "out": .50, "chat": .80}), ("out", {"in": .72, "out": .70, "chat": .30})]  # um "out" que parece "in"
    thresholds, stats = cal.scope_thresholds_from(items)
    assert set(thresholds) == set(sc.LABELS)
    assert stats["in"]["worst_wrong_margin"] == pytest.approx(0.02, abs=1e-4)   # o "out" que parecia "in" por +0,02
    assert thresholds["in"] == pytest.approx(max(0.02 + cal.SCOPE_SLACK, cal.SCOPE_FLOOR), abs=1e-4)  # pior erro + folga, nunca abaixo do piso
    assert stats["in"]["decisive"] == 2 and stats["in"]["n"] == 2                 # margens 0,20 e 0,10 passam
    assert thresholds["chat"] >= cal.SCOPE_FLOOR


def test_every_scope_relevant_category_of_the_dataset_is_mapped_to_a_label():
    """Categoria nova no conjunto de situações que o calibrador não conhece não entra na medição (foi um bug real: números idênticos
    antes e depois de ampliar o conjunto). Toda categoria é mapeada OU está na lista explícita dos que não têm rótulo de escopo."""
    import json
    from pathlib import Path
    cases = json.loads((Path(__file__).parent / "data" / "situations.json").read_text(encoding="utf-8"))
    unlabelled_on_purpose = {c for c in {x["category"] for x in cases} if c.startswith("attack_") or c in (
        "out_other_business", "out_code_tech", "out_advice")}   # ataques e FRONTEIRAS não têm resposta única
    mapped = set(cal.SCOPE_LABEL_OF) | set(cal.SCOPE_IN_CATEGORIES)
    missing = {x["category"] for x in cases} - mapped - unlabelled_on_purpose
    assert not missing, missing
