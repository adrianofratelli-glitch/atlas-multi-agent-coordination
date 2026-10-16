"""B3: economia de LLM em time series — medições, agregação por janela, DEMO_MODE e fail-open."""

import asyncio
import random
from datetime import datetime, timedelta, timezone

from app import usage_ts
from app.config import Settings
from app.database import DataStore

CUSTOMER = {"customer_key": "c1", "area": "varejo"}
CALL = {"agent": "order_agent", "model": "claude-sonnet-5-5", "status": "ok", "input_tokens": 1000,
        "output_tokens": 200, "cache_read_tokens": 800, "estimated_cost_usd": 0.0042, "latency_ms": 900}


def test_measurements_shape_and_invariants():
    docs = usage_ts.build_measurements(customer=CUSTOMER, llm_calls=[CALL, {"status": "circuit_open"}],
                                       cache_hit=True, tokens_saved=350)
    assert [d["meta"]["kind"] for d in docs] == ["llm", "cache"]
    assert docs[0]["meta"] == {"kind": "llm", "agent": "order_agent", "model": "claude-sonnet-5-5", "area": "varejo"}
    assert docs[1]["tokens_saved"] == 350 and docs[1]["cache_hit"] is True
    assert all(isinstance(d["at"], datetime) for d in docs)


def test_hostile_and_garbage_calls_never_raise_or_go_negative():
    rng = random.Random(8)
    atoms = [None, -5, float("nan"), float("inf"), "x", True, 3, 2.5, {"$gt": 1}, "a" * 500]
    for _ in range(500):
        call = {k: rng.choice(atoms) for k in ("agent", "model", "status", "input_tokens", "output_tokens",
                                                  "cache_read_tokens", "estimated_cost_usd", "latency_ms")}
        docs = usage_ts.build_measurements(customer=rng.choice([None, {}, CUSTOMER]), llm_calls=[call, "lixo"],
                                           cache_hit=rng.random() < .5, tokens_saved=rng.choice(atoms))
        for d in docs:
            assert d["cost_usd"] is None or d["cost_usd"] >= 0
            assert all(d[f] >= 0 for f in usage_ts._NUM_FIELDS)
            assert isinstance(d["meta"]["agent"], str) and len(d["meta"]["agent"]) <= 40


def test_window_clamped():
    assert usage_ts._window(0) == 1 and usage_ts._window("x") == 24
    assert usage_ts._window(10**9) == usage_ts.MAX_WINDOW_HOURS


def test_pipeline_aggregates_by_hour_and_agent_on_server():
    pipe = usage_ts.usage_pipeline(24)
    assert "$gte" in pipe[0]["$match"]["at"]
    group = pipe[1]["$facet"]["by_agent_hour"][1]["$group"]
    assert group["_id"]["hour"] == {"$dateTrunc": {"date": "$at", "unit": "hour"}}
    assert "cache" in pipe[1]["$facet"]


def _demo_store():
    store = DataStore(Settings(demo_mode=True))
    asyncio.run(store.connect())
    return store


def test_demo_mode_record_and_report_parity():
    store = _demo_store()
    asyncio.run(usage_ts.record_turn(store, customer=CUSTOMER, llm_calls=[CALL, dict(CALL, agent="billing_agent")],
                                     cache_hit=False))
    asyncio.run(usage_ts.record_turn(store, customer=CUSTOMER, llm_calls=[], cache_hit=True, tokens_saved=300))
    report = asyncio.run(usage_ts.usage_report(store, 24))
    assert report["source"] == "memory"
    agents = {r["agent"]: r for r in report["by_agent_hour"]}
    assert set(agents) == {"order_agent", "billing_agent"}
    assert agents["order_agent"]["cost_usd"] == 0.0042 and agents["order_agent"]["cost_complete"]
    assert report["cache"] == {"hits": 1, "llm_calls": 2, "tokens_saved": 300, "hit_rate": round(1 / 3, 4),
                               "cost_usd": 0.0084}


def test_old_measurements_outside_window_are_ignored():
    store = _demo_store()
    old = usage_ts.build_measurements(customer=CUSTOMER, llm_calls=[CALL], cache_hit=False,
                                      tokens_saved=0, at=datetime.now(timezone.utc) - timedelta(hours=30))
    asyncio.run(store.insert_one(usage_ts.COLLECTION, old[0]))
    assert asyncio.run(usage_ts.usage_report(store, 24))["by_agent_hour"] == []
    assert len(asyncio.run(usage_ts.usage_report(store, 48))["by_agent_hour"]) == 1


def test_record_is_fail_open():
    class Broken:
        memory = False

        def _collection(self, name):
            raise RuntimeError("Atlas fora")
    assert asyncio.run(usage_ts.record_turn(Broken(), customer=CUSTOMER, llm_calls=[CALL], cache_hit=False)) == 0


def test_unknown_cost_marks_hour_incomplete_never_zero_silently():
    store = _demo_store()
    asyncio.run(usage_ts.record_turn(store, customer=CUSTOMER, llm_calls=[dict(CALL, estimated_cost_usd=None)],
                                     cache_hit=False))
    row = asyncio.run(usage_ts.usage_report(store, 24))["by_agent_hour"][0]
    assert row["cost_complete"] is False
