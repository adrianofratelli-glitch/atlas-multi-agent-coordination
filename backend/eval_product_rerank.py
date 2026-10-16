"""B2: $rerank na busca de catálogo do product_agent — mede antes de decidir (só leitura, *_test).

Compara o pipeline atual (relevância vetorial 0,55 + nota 0,30 + estoque 0,15) com a mesma
ponderação usando o score do $rerank no lugar da relevância vetorial, sobre os mesmos 8
candidatos do $vectorSearch.

    cd backend && ../.venv/bin/python eval_product_rerank.py [--model rerank-2.5-lite]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import atlas_compat as ac  # noqa: E402
from app.agents import build_product_pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def with_rerank(message: str, model: str) -> list[dict]:
    base = build_product_pipeline(message)
    vs, rest = base[0], base[1:]
    # o mesmo pool de 8 do $vectorSearch; o rerank vira a "relevância" da ponderação de negócio
    rr = ac.build_rerank_stages(message, ["name", "search_text"],
                                ac.RerankConfig(enabled=True, model=model, pool=8, top_n=8))
    rest = [{"$addFields": {"relevance": "$rerank_score"}} if st == {"$addFields": {"relevance": {"$meta": "vectorSearchScore"}}}
            else st for st in rest]
    return [vs, {"$addFields": {"_vs": {"$meta": "vectorSearchScore"}}}, *rr[:3], *rest]


async def run(store, variant: str, model: str, cases, repeat: int):
    coll = store._collection("products_catalog")
    rows, lat = [], []
    for case in cases:
        for i in range(repeat):
            pipe = build_product_pipeline(case["query"]) if variant == "atual" else with_rerank(case["query"], model)
            t0 = time.perf_counter()
            cursor = await coll.aggregate(pipe)
            got = await cursor.to_list(None)
            lat.append((time.perf_counter() - t0) * 1000)
            if i == 0:
                ids = [g["sku"] for g in got]
                rank = next((n + 1 for n, s in enumerate(ids) if s in case["expected"]), None)
                rows.append({"id": case["id"], "query": case["query"], "returned": ids, "rank": rank})
    n = len(rows)
    return {"variant": variant, "model": model if variant != "atual" else None,
            "recall_at_1": round(sum(r["rank"] == 1 for r in rows) / n, 3),
            "recall_at_4": round(sum(bool(r["rank"]) for r in rows) / n, 3),
            "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 3),
            "p50": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1),
            "rows": rows}


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="rerank-2.5-lite")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    from scripts.isolation import open_test_store

    store, _ = await open_test_store(what="eval_product_rerank", seed_if_empty=False, wait_indexes=False)
    try:
        cases = json.loads((ROOT / "eval" / "product_retrieval.json").read_text(encoding="utf-8"))["cases"]
        await store._collection("products_catalog").aggregate(build_product_pipeline("aquecimento"))
        out = [await run(store, "atual", args.model, cases, args.repeat),
               await run(store, "rerank", args.model, cases, args.repeat)]
    finally:
        await store.close()
    path = ROOT / "eval" / "reports" / f"product-rerank-{args.model}-{datetime.now():%Y-%m-%d}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for o in out:
        print({k: v for k, v in o.items() if k != "rows"})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
