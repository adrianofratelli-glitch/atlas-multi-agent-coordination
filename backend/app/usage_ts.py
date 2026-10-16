"""Economia de LLM em time series (`llm_usage_ts`, ADR-008).

Uma medição por chamada de LLM (agente, modelo, área, tokens, custo estimado, latência) e uma
por HIT da cascata de cache (tokens economizados). Gravada a partir de
`OrchestrationService._persist_trace`, o ponto de saída de todo turno, sem alterar
`agent_traces` nem `agent_handoffs`. Best-effort: falha aqui nunca derruba o turno.

Coleção (doc oficial: https://www.mongodb.com/docs/manual/core/timeseries/timeseries-procedures/):
  timeField  `at`      (BSON date, obrigatório)
  metaField  `meta`    {agent, model, area, kind} — o que identifica uma série e quase não muda
  granularity "minutes": no ritmo da demo, chamadas da mesma série chegam com minutos de
              intervalo; "seconds" criaria buckets quase vazios, "hours" juntaria horas num bucket
              só e pioraria a agregação por hora no início da janela.
  expireAfterSeconds 90 dias: economia é tendência de semanas, não auditoria (essa fica no trace).
O índice composto meta + at é criado pelo próprio MongoDB.

DEMO_MODE: mesma gravação no armazenamento em memória e as MESMAS agregações em Python.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

COLLECTION = "llm_usage_ts"
TIMESERIES = {"timeField": "at", "metaField": "meta", "granularity": "minutes"}
EXPIRE_AFTER_SECONDS = 90 * 24 * 3600
MAX_WINDOW_HOURS = 24 * 30
_NUM_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value > 0 else 0


def _cost(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else None


def build_measurements(*, customer: dict, llm_calls: list[dict] | None, cache_hit: bool,
                       tokens_saved: int, at: datetime | None = None) -> list[dict]:
    """Documentos da time series para um turno. Nunca levanta; custo nunca negativo."""
    at = at or datetime.now(timezone.utc)
    area = str((customer or {}).get("area") or "desconhecida")[:40]
    docs = []
    for call in llm_calls or []:
        if not isinstance(call, dict) or call.get("status") == "circuit_open":
            continue
        doc = {"at": at, "meta": {"kind": "llm", "agent": str(call.get("agent") or "?")[:40],
                                  "model": str(call.get("model") or "?")[:60], "area": area},
               "status": str(call.get("status") or "?")[:20],
               "cache_hit": False, "cost_usd": _cost(call.get("estimated_cost_usd")),
               "latency_ms": _int(call.get("latency_ms"))}
        doc.update({f: _int(call.get(f)) for f in _NUM_FIELDS})
        docs.append(doc)
    if cache_hit:
        docs.append({"at": at, "meta": {"kind": "cache", "agent": "cascade", "model": "-", "area": area},
                     "status": "hit", "cache_hit": True, "tokens_saved": _int(tokens_saved),
                     "cost_usd": 0.0, "latency_ms": 0, **{f: 0 for f in _NUM_FIELDS}})
    return docs


async def ensure_collection(store) -> str:
    """Cria a time series se não existir (idempotente). Coleção comum com o mesmo nome = aviso."""
    if store.memory:
        return f"{COLLECTION}: em memória (DEMO_MODE)"
    db = store.client[store.settings.mongodb_db]
    existing = [c async for c in await db.list_collections(filter={"name": COLLECTION})]
    if existing:
        kind = existing[0].get("type")
        return f"{COLLECTION}: já existe ({kind})" + ("" if kind == "timeseries" else " — NÃO é time series")
    await db.create_collection(COLLECTION, timeseries=TIMESERIES, expireAfterSeconds=EXPIRE_AFTER_SECONDS)
    return f"{COLLECTION}: time series criada (granularity minutes, TTL 90 dias)"


async def record_turn(store, *, customer: dict, llm_calls: list[dict] | None, cache_hit: bool,
                      tokens_saved: int = 0) -> int:
    docs = build_measurements(customer=customer, llm_calls=llm_calls, cache_hit=cache_hit,
                              tokens_saved=tokens_saved)
    if not docs:
        return 0
    try:
        if store.memory:
            for doc in docs:
                await store.insert_one(COLLECTION, doc)
        else:
            await store._collection(COLLECTION).insert_many(docs, ordered=False)
        return len(docs)
    except Exception:  # noqa: BLE001 — economia é observabilidade: nunca derruba o turno
        logger.warning("llm_usage_ts: gravação falhou (best-effort)", exc_info=True)
        return 0


def _window(hours: Any) -> int:
    try:
        value = int(hours)
    except (TypeError, ValueError):
        value = 24
    return max(1, min(MAX_WINDOW_HOURS, value))


def usage_pipeline(hours: int, *, now: datetime | None = None) -> list[dict]:
    """Custo e tokens por agente por hora + economia do cache, numa agregação ($facet)."""
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    return [
        {"$match": {"at": {"$gte": since}}},
        {"$facet": {
            "by_agent_hour": [
                {"$match": {"meta.kind": "llm"}},
                {"$group": {"_id": {"hour": {"$dateTrunc": {"date": "$at", "unit": "hour"}},
                                    "agent": "$meta.agent"},
                            "calls": {"$sum": 1},
                            "cost_usd": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                            "cost_unknown": {"$sum": {"$cond": [{"$eq": ["$cost_usd", None]}, 1, 0]}},
                            "input_tokens": {"$sum": "$input_tokens"},
                            "output_tokens": {"$sum": "$output_tokens"},
                            "cache_read_tokens": {"$sum": "$cache_read_tokens"},
                            "latency_ms": {"$avg": "$latency_ms"}}},
                {"$sort": {"_id.hour": 1, "_id.agent": 1}},
                {"$limit": 2000},
            ],
            "cache": [
                {"$group": {"_id": None,
                            "hits": {"$sum": {"$cond": ["$cache_hit", 1, 0]}},
                            "llm_calls": {"$sum": {"$cond": [{"$eq": ["$meta.kind", "llm"]}, 1, 0]}},
                            "tokens_saved": {"$sum": {"$ifNull": ["$tokens_saved", 0]}},
                            "cost_usd": {"$sum": {"$ifNull": ["$cost_usd", 0]}}}},
            ],
        }},
    ]


def _shape(raw: dict, hours: int, source: str) -> dict:
    rows = [{"hour": r["_id"]["hour"].isoformat() if isinstance(r["_id"]["hour"], datetime) else str(r["_id"]["hour"]),
             "agent": r["_id"]["agent"], "calls": r["calls"], "cost_usd": round(r["cost_usd"], 8),
             "cost_complete": r.get("cost_unknown", 0) == 0,
             "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
             "cache_read_tokens": r["cache_read_tokens"],
             "avg_latency_ms": round(r["latency_ms"] or 0, 1)} for r in raw.get("by_agent_hour", [])]
    cache = (raw.get("cache") or [{}])[0] if raw.get("cache") else {}
    hits, calls = cache.get("hits", 0), cache.get("llm_calls", 0)
    return {"source": source, "window_hours": hours, "by_agent_hour": rows,
            "cache": {"hits": hits, "llm_calls": calls, "tokens_saved": cache.get("tokens_saved", 0),
                      "hit_rate": round(hits / (hits + calls), 4) if hits + calls else None,
                      "cost_usd": round(cache.get("cost_usd", 0.0), 8)}}


async def usage_report(store, hours: Any = 24) -> dict:
    """Leitura da time series; em DEMO_MODE a mesma agregação em Python."""
    hours = _window(hours)
    if store.memory:
        return _shape(_aggregate_memory(await store.find_many(COLLECTION, {}, limit=100_000), hours),
                      hours, "memory")
    raw = await store.aggregate(COLLECTION, usage_pipeline(hours))
    return _shape(raw[0] if raw else {}, hours, "timeseries")


def _aggregate_memory(docs: list[dict], hours: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    groups: dict = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "cost_unknown": 0, "input_tokens": 0,
                                        "output_tokens": 0, "cache_read_tokens": 0, "_lat": []})
    cache = {"hits": 0, "llm_calls": 0, "tokens_saved": 0, "cost_usd": 0.0}
    for d in docs:
        at = d.get("at")
        if not isinstance(at, datetime):
            continue
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        if at < since:
            continue
        meta = d.get("meta") or {}
        cache["cost_usd"] += d.get("cost_usd") or 0
        if d.get("cache_hit"):
            cache["hits"] += 1
            cache["tokens_saved"] += d.get("tokens_saved", 0)
            continue
        if meta.get("kind") != "llm":
            continue
        cache["llm_calls"] += 1
        g = groups[(at.replace(minute=0, second=0, microsecond=0), meta.get("agent"))]
        g["calls"] += 1
        g["cost_usd"] += d.get("cost_usd") or 0
        g["cost_unknown"] += d.get("cost_usd") is None
        for f in ("input_tokens", "output_tokens", "cache_read_tokens"):
            g[f] += d.get(f, 0)
        g["_lat"].append(d.get("latency_ms", 0))
    rows = []
    for (hour, agent), g in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        lat = g.pop("_lat")
        rows.append({"_id": {"hour": hour, "agent": agent}, **g, "latency_ms": sum(lat) / len(lat) if lat else 0})
    return {"by_agent_hour": rows, "cache": [cache]}
