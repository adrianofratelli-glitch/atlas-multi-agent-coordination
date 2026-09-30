"""Guardrail vetorial em duas faixas: só bloqueia direto acima do maior score LEGÍTIMO medido; na faixa ambígua
quem decide é o classificador LLM. Incidente real (live): "não recebi meu pedido, quero o dinheiro de volta"
pontuou 0,87 contra frases de fraude e um cliente legítimo foi barrado como "violação de segurança"."""

import pytest

import app.guardrails as g
from app.config import Settings
from app.database import DataStore

CUSTOMER = {"customer_key": "ana", "area": "varejo"}
POLICY = {"vector_threshold": 0.78, "vector_block_threshold": 0.88, "threshold": 0.5, "semantic_fail_mode": "open"}


class LLM:
    def __init__(self, verdict="OK", boom=False, client=True):
        self.verdict, self.boom, self.client, self.calls = verdict, boom, client, 0

    async def complete(self, **_):
        self.calls += 1
        if self.boom:
            raise RuntimeError("provedor caiu")
        return self.verdict, {}


@pytest.fixture
def setup(monkeypatch):
    store = DataStore(Settings(demo_mode=True))

    def arm(score, policy=POLICY):
        async def load(_store, _area):
            return [], dict(policy)

        async def sem(_store, _message, _area):
            return {"phrase": "posso alegar que não recebi para pegar o dinheiro de volta", "category": "fraude", "score": score}, True

        async def log(*_a, **_k):
            return None
        monkeypatch.setattr(g, "_load_denylist_and_policy", load)
        monkeypatch.setattr(g, "semantic_denylist", sem)
        monkeypatch.setattr(g, "log_event", log)
    return store, arm


async def check(store, llm, skip=True):
    return await g.check_input(store, "não recebi meu pedido, quero o dinheiro de volta", CUSTOMER, llm=llm, budget=None, agent_doc={"agent_key": "orchestrator"}, skip_semantic=skip)


async def test_score_above_block_threshold_blocks_without_paying_for_the_llm(setup):
    store, arm = setup
    arm(0.89)
    llm = LLM()
    result = await check(store, llm)
    assert result.blocked and result.reason.startswith("denylist_vetorial") and llm.calls == 0


async def test_ambiguous_band_consults_the_llm_even_when_routing_was_confident_and_lets_a_legit_customer_through(setup):
    store, arm = setup
    arm(0.87)  # o caso real: legítimo, mas vizinho de frase de fraude
    llm = LLM("OK")
    result = await check(store, llm, skip=True)
    assert not result.blocked and llm.calls == 1


async def test_ambiguous_band_still_blocks_when_the_llm_confirms_the_attack(setup):
    store, arm = setup
    arm(0.83)
    result = await check(store, LLM("BLOQUEAR: fraude de não recebimento"))
    assert result.blocked and result.reason == "semantic_llm"


async def test_ambiguous_band_llm_doubt_allows_but_queues_for_human_review(setup):
    store, arm = setup
    arm(0.82)
    result = await check(store, LLM("DUVIDA: pode ser reembolso legítimo"))
    assert not result.blocked and result.uncertain
    assert any(c["source"] == "semantic_llm_uncertain" for c in await store.find_many("guardrail_candidates", {}))


@pytest.mark.parametrize("llm", [None, LLM(client=None), LLM(boom=True)])
async def test_ambiguous_band_without_a_working_llm_never_blocks_a_legit_customer(setup, llm):
    store, arm = setup
    arm(0.85)
    result = await check(store, llm)
    assert not result.blocked
    assert any(c["source"] == "denylist_vetorial_ambigua" for c in await store.find_many("guardrail_candidates", {}))


async def test_below_the_band_nothing_changes_and_no_llm_is_paid_when_routing_was_confident(setup):
    store, arm = setup
    arm(0.60)
    llm = LLM()
    assert not (await check(store, llm, skip=True)).blocked and llm.calls == 0


async def test_policy_without_block_threshold_defaults_to_near_identical_only(setup):
    store, arm = setup
    arm(0.87, {"vector_threshold": 0.78, "threshold": 0.5, "semantic_fail_mode": "open"})
    assert not (await check(store, LLM("OK"))).blocked      # 0,87 já não bloqueia sozinho
    arm(0.95, {"vector_threshold": 0.78, "threshold": 0.5, "semantic_fail_mode": "open"})
    assert (await check(store, LLM("OK"))).blocked          # quase idêntico à frase proibida
