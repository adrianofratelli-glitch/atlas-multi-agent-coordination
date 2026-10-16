"""Eval de retrieval do KB (support_agent): recall@1/@3, MRR, latência e invariantes.

Roda o `search_kb` REAL contra `eval/kb_retrieval.json` no banco de teste (scripts/isolation.py
recusa a demo; o retrieval só lê, mas o padrão do repo é medir no *_test). Invariantes por
consulta (falha = exit 1): estratégia conhecida, sem duplicata, no máximo 4 artigos, filtro de
categoria respeitado pelo servidor.

    cd backend && ../.venv/bin/python eval_kb_retrieval.py --label baseline
    cd backend && ../.venv/bin/python eval_kb_retrieval.py --label rerank --strategy rankFusion+rerank \\
        --rerank '{"enabled": true, "model": "rerank-2.5-lite", "pool": 20}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval" / "kb_retrieval.json"
REPORTS = ROOT / "eval" / "reports"


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 1) if ordered else 0.0


async def _search(store, query: str, category, strategy, agent_doc):
    from app import agents

    if hasattr(agents, "search_kb_explained"):
        found = await agents.search_kb_explained(store, query, agent_doc=agent_doc, category=category,
                                                 strategy=strategy, local_rank=agents._local_rank)
        return found.articles, found.strategy
    if category is not None:  # código antigo (baseline) não tem filtro de categoria
        return None, "sem_suporte"
    return await agents.search_kb(store, query)


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--strategy")
    parser.add_argument("--rerank", help="JSON de agent_registry.support_agent.retrieval.rerank")
    parser.add_argument("--config-json", help="retrieval.fusion do support_agent (JSON)")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    from scripts.isolation import open_test_store

    store, settings = await open_test_store(what="eval_kb_retrieval")
    try:
        # Mesmo documento que o turno usa: o support_agent do registry do banco de teste.
        registry = await store.find_one("agent_registry", {"agent_key": "support_agent"}, brain=True) or {}
        retrieval = dict(registry.get("retrieval") or {})
        if args.config_json is not None:
            retrieval["fusion"] = json.loads(args.config_json)
        if args.rerank:
            retrieval["rerank"] = json.loads(args.rerank)
        agent_doc = {"retrieval": retrieval}
        data = json.loads(DATASET.read_text(encoding="utf-8"))
        known = {"rankFusion", "rankFusion+rerank", "rerank_indisponivel", "rankFusion_basico",
                 "rrf_aplicacao", "vetor", "rrf_local", "sem_suporte"}
        await _search(store, "aquecimento", None, args.strategy, agent_doc)
        rows, lat, violations = [], [], []
        for case in data["cases"]:
            for attempt in range(args.repeat):
                t0 = time.perf_counter()
                articles, strategy = await _search(store, case["query"], case.get("category"),
                                                   args.strategy, agent_doc)
                ms = (time.perf_counter() - t0) * 1000
                if articles is None:
                    break
                lat.append(ms)
                if attempt:
                    continue
                ids = [a.get("article_id") for a in articles]
                problems = []
                if strategy not in known:
                    problems.append(f"estratégia desconhecida {strategy!r}")
                if len(ids) != len(set(ids)):
                    problems.append("duplicata")
                if len(ids) > data.get("limit", 4):
                    problems.append("acima do limite")
                if case.get("category") and any(a.get("category") != case["category"] for a in articles):
                    problems.append("categoria fora do filtro")
                expected = case["expected"]
                rank = next((i + 1 for i, a in enumerate(ids) if a in expected), None)
                rows.append({"id": case["id"], "kind": case["kind"], "query": case["query"],
                             "expected": expected, "returned": ids, "strategy": strategy,
                             "rank": rank, "ms": round(ms, 1), "problems": problems})
                if problems:
                    violations.append({"id": case["id"], "problems": problems})
        scored = [r for r in rows if r["expected"]]

        def agg(sub):
            n = len(sub) or 1
            return {"cases": len(sub),
                    "recall_at_1": round(sum(1 for r in sub if r["rank"] == 1) / n, 3),
                    "recall_at_3": round(sum(1 for r in sub if r["rank"] and r["rank"] <= 3) / n, 3),
                    "mrr": round(sum(1 / r["rank"] for r in sub if r["rank"]) / n, 3)}
        report = {"summary": {"label": args.label, "strategy_forced": args.strategy,
                              "rerank": json.loads(args.rerank) if args.rerank else None,
                              "config": json.loads(args.config_json) if args.config_json else None,
                              "database": settings.mongodb_db, **agg(scored),
                              "latency_ms": {"p50": _pct(lat, .5), "p95": _pct(lat, .95),
                                             "mean": round(statistics.fmean(lat), 1) if lat else 0},
                              "strategies": sorted({r["strategy"] for r in rows}),
                              "by_kind": {k: agg([r for r in scored if r["kind"] == k])
                                          for k in sorted({r["kind"] for r in scored})},
                              "violations": len(violations),
                              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                  "violations": violations, "rows": rows}
    finally:
        await store.close()
    out = args.out or REPORTS / f"kb-retrieval-{args.label}-{datetime.now():%Y-%m-%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    s = report["summary"]
    print(json.dumps({k: s[k] for k in ("recall_at_1", "recall_at_3", "mrr", "latency_ms", "strategies",
                                        "violations", "by_kind")}, ensure_ascii=False))
    print(f"relatório: {out}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
