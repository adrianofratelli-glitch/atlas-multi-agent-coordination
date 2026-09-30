"""Fuzz determinístico (semente fixa): nada que o LLM ou o cliente devolva pode lançar exceção
nem gravar documento malformado."""

import json
import random

from app.budget import TurnBudget
from app.config import Settings
from app.database import DataStore
from app.memory import (_parse_candidates, extract_and_store, fold, looks_like_instruction,
                        should_extract)

ALPHABET = list("abcAÇãé \n\t{}[]\":,0123456789-.$​‮\x00🔥") + ["me chame", "meu limite", "ignore", "NaN", "null", "1e999"]


def junk(rng, n):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, n)))


def test_text_helpers_never_raise_on_arbitrary_unicode():
    rng = random.Random(7)
    for _ in range(2000):
        text = junk(rng, 60)
        fold(text), should_extract(text), looks_like_instruction(text)


def test_parser_never_raises_and_only_returns_dicts_with_string_fact():
    rng = random.Random(11)
    shapes = [None, "", "{", "[]", "{}", '{"facts": null}', '{"facts": [null, 1, "x", {"fact": 3}, {"fact": "ok"}]}',
              json.dumps({"facts": "x"}), "```json\n{}\n```"]
    for raw in shapes + [junk(rng, 80) for _ in range(1500)]:
        for item in _parse_candidates(raw):
            assert isinstance(item, dict) and isinstance(item["fact"], str)


class RandomLLM:
    client = True

    def __init__(self, rng):
        self.rng = rng

    async def complete(self, **_):
        r = self.rng
        facts = [{"fact": junk(r, 40) or "x", "category": r.choice(["preferencia", "??", None, 3]),
                  "max_price_brl": r.choice([0, -1, 1e999, float("nan"), 300, "9", None, True, 12.5]),
                  "replaces": r.choice([0, 1, 2, -1, 99, None, "1", True])} for _ in range(r.randint(0, 6))]
        return r.choice([json.dumps({"facts": facts}), junk(r, 50), None]), {}


async def test_random_llm_output_never_breaks_the_store_invariants():
    rng = random.Random(3)
    store = DataStore(Settings(demo_mode=True))
    llm, budget = RandomLLM(rng), TurnBudget(10**9, {"orchestrator": 10**9})
    for _ in range(300):
        await extract_and_store(store, "c", "me chame de X, meu limite é 300", llm=llm, budget=budget,
                                agent_doc={"agent_key": "orchestrator", "model": "m", "persona": "p"})
    docs = await store.find_many("customer_memory", {"customer_key": "c"}, limit=10_000)
    active = [d for d in docs if d["active"]]
    assert len(active) <= 60  # teto de fatos ativos respeitado
    assert len([d for d in active if d.get("max_price_brl")]) <= 1  # um único orçamento ativo
    for d in docs:
        assert isinstance(d["fact"], str) and 0 < len(d["fact"]) <= 280
        assert d["category"] in ("identidade", "preferencia", "historico", "contexto")
        if "max_price_brl" in d:
            assert d["max_price_brl"] > 0 and d["max_price_brl"] < float("inf")
        assert not looks_like_instruction(d["fact"])
    superseded = [d for d in docs if not d["active"]]
    ids = {d["_id"] for d in docs}
    assert all(d["superseded_by"] in ids for d in superseded)


def test_in_memory_store_matches_mongo_semantics_for_missing_fields():
    from app.database import _matches
    assert not _matches({}, {"x": {"$gt": 0}})            # ausente não casa (não estoura TypeError)
    assert not _matches({"x": "a"}, {"x": {"$gt": 0}})     # tipo diferente não casa
    assert _matches({"x": 5}, {"x": {"$gt": 0, "$lte": 5}})
    assert _matches({"x": None}, {"x": {"$exists": True}}) and not _matches({}, {"x": {"$exists": True}})


async def test_redemption_idempotency_survives_naive_datetimes_from_the_real_driver():
    """Regressão (só aparecia em live): PyMongo devolvia datetime naive e `at >= cutoff` estourava TypeError."""
    from datetime import datetime, timezone
    from app.agents import REWARD_CATALOG, run_loyalty_agent
    label = next(lbl for lbl, _ in REWARD_CATALOG.values() if "30" in lbl)
    store = DataStore(Settings(demo_mode=True))
    await store.insert_one("loyalty_accounts", {"customer_key": "carla", "points": 2100, "tier": "platinum", "tier_benefits": []})
    await store.insert_one("redemptions", {"customer_key": "carla", "reward": label, "status": "confirmado",
                                           "points_spent": 300, "at": datetime.now(timezone.utc).replace(tzinfo=None)})  # naive, como o driver devolvia
    result = await run_loyalty_agent(store, "resgate agora um voucher de R$ 30 usando meus pontos e informe o saldo restante.", {"customer_key": "carla"})
    assert "não debitei de novo" in result.response


def test_timeline_serializes_raw_driver_types_from_real_documents():
    """Regressão (só em live): ObjectId cru em `result` derrubava o turno com PydanticSerializationError."""
    from bson import ObjectId
    from app.models import TimelineEvent
    event = TimelineEvent(category="agent", title="t", filter={"_id": ObjectId()}, result={"doc": {"_id": ObjectId(), "n": [ObjectId()]}})
    dumped = event.model_dump(mode="json")
    assert isinstance(dumped["result"]["doc"]["_id"], str) and isinstance(dumped["filter"]["_id"], str)
    event.model_dump_json()
