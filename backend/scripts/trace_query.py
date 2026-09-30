"""Visão operacional em cima dos spans: QUEM travou e ONDE.

Só faz sentido com `TRACE_SINK=atlas`, quando os spans viram documentos numa collection
(`TRACE_DB`, default `observability`; `TRACE_COLLECTION`, default `spans`) — que é o ponto da
PoV: o traço é dado operacional, consultável com a mesma agregação de qualquer outra coleção.

    cd backend && TRACE_SINK=atlas ../.venv/bin/python run.py         # produz spans
    cd backend && ../.venv/bin/python scripts/trace_query.py          # lê os spans
    cd backend && ../.venv/bin/python scripts/trace_query.py --conversation conv-abc123

Saída: por conversa, o span mais lento e o primeiro com erro — com agente, tool e latência.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

SLOW_PIPELINE = [
    # O span raiz `turn` contém todos os outros: ele é sempre o mais longo e nunca é a resposta
    # para "quem travou". O ranking olha só as folhas (agente, handoff, tool, chamada de LLM).
    {"$addFields": {"leaf_ms": {"$cond": [{"$eq": ["$name", "turn"]}, -1, "$duration_ms"]}}},
    {"$sort": {"leaf_ms": -1}},
    {"$group": {
        "_id": "$trace_id",
        "conversation_id": {"$first": "$attributes.conversation_id"},
        "slowest_span": {"$first": "$name"},
        "slowest_ms": {"$first": "$leaf_ms"},
        "slowest_agent": {"$first": "$attributes.agent"},
        "slowest_tool": {"$first": "$attributes.tool.name"},
        "spans": {"$sum": 1},
        "errors": {"$sum": {"$cond": [{"$eq": ["$status", "ERROR"]}, 1, 0]}},
        "error_span": {"$max": {"$cond": [{"$eq": ["$status", "ERROR"]}, "$name", None]}},
        "error_type": {"$max": "$attributes.error_type"},
        "tokens": {"$sum": {"$add": [{"$ifNull": ["$attributes.llm.input_tokens", 0]},
                                     {"$ifNull": ["$attributes.llm.output_tokens", 0]}]}},
        "cost_usd": {"$sum": {"$ifNull": ["$attributes.llm.estimated_cost_usd", 0]}},
    }},
    {"$sort": {"slowest_ms": -1}},
    {"$limit": 20},
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="quem travou e onde, a partir dos spans")
    parser.add_argument("--conversation", default="", help="filtra por conversation_id")
    parser.add_argument("--errors-only", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    uri = os.getenv("TRACE_MONGODB_URI") or settings.mongodb_uri
    if not uri:
        print("sem MONGODB_URI/TRACE_MONGODB_URI: nada para consultar (TRACE_SINK=atlas grava aqui)")
        return 2

    from pymongo import AsyncMongoClient

    client = AsyncMongoClient(uri)
    collection = client[os.getenv("TRACE_DB", "observability")][os.getenv("TRACE_COLLECTION", "spans")]
    match: dict = {}
    if args.conversation:
        match["attributes.conversation_id"] = args.conversation
    if args.errors_only:
        match["status"] = "ERROR"
    pipeline = ([{"$match": match}] if match else []) + SLOW_PIPELINE
    rows = await (await collection.aggregate(pipeline)).to_list(length=None)
    await client.close()

    if not rows:
        print("nenhum span encontrado — o backend rodou com TRACE_SINK=atlas?")
        return 1
    header = f"{'conversa':<22}{'spans':>6}{'erros':>6}{'tokens':>8}{'custo_usd':>11}  mais lento"
    print(header)
    print("-" * len(header))
    for row in rows:
        conversation = str(row.get("conversation_id") or row["_id"][:12])
        where = row.get("slowest_agent") or row.get("slowest_tool") or "-"
        print(f"{conversation:<22}{row['spans']:>6}{row['errors']:>6}{row['tokens']:>8}"
              f"{row['cost_usd']:>11.5f}  {row['slowest_span']} [{where}] {row['slowest_ms']:.0f}ms"
              + (f"  ERRO em {row['error_span']} ({row.get('error_type') or '?'})" if row["errors"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
