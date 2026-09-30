"""Mede o agente REAL (Atlas + LLM) contra tests/data/situations.json — o que um cliente de verdade digita.

    cd backend && python eval_situations.py                    # relatório por categoria, dev vs holdout
    python eval_situations.py --json /tmp/antes.json           # guarda o resultado para comparar depois
    python eval_situations.py --compare /tmp/antes.json        # mostra o que mudou (melhorou/piorou) caso a caso

Regra de ouro: AJUSTE olhando o split `dev`; o `holdout` só serve para dizer a verdade. Se dev sobe e holdout não, foi
overfitting. Identidade descartável; limpa o que gravou (inclusive o que o classificador aprendeu na denylist).
Custa tokens reais (a maioria das situações roda um agente com LLM); use --category para uma fatia."""

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "")
os.environ["LANGFUSE_PUBLIC_KEY"] = os.environ["LANGFUSE_SECRET_KEY"] = ""

from app.config import get_settings  # noqa: E402
from app.database import DataStore  # noqa: E402
from app.llm import LLMGateway  # noqa: E402
from app.orchestration import OrchestrationService  # noqa: E402

DATA = Path(__file__).parent / "tests" / "data" / "situations.json"
NON_AGENT = {"blocked", "out_of_scope", "welcome"}


def outcome_of(response) -> str:
    """O que o cliente viu: bloqueado, orientação de escopo, boas-vindas ou qual agente respondeu."""
    if response.active_agent == "guardrail":
        return "blocked"
    if response.active_agent == "orchestrator":
        scope = any(e.category == "guardrail" and (e.result or {}).get("out_of_scope") for e in response.timeline)
        return "out_of_scope" if scope else "welcome"
    return f"agent:{response.active_agent}"


def matches(case: dict, outcome: str) -> bool:
    expect = case["expect"]
    if expect == "agent":  # foi ATENDIDO por algum agente; o agente esperado é métrica à parte (agent_match_pct)
        return outcome.startswith("agent:")
    if expect == "handled":  # fronteira: agente ("não temos") ou orientação de escopo servem; bloquear/acolher não
        return outcome.startswith("agent:") or outcome == "out_of_scope"
    if expect == "non_agent":
        return outcome in NON_AGENT
    return outcome == expect


def summarize(results: list[dict]) -> dict:
    """Taxas que importam ao cliente, separadas de acerto de rótulo."""
    def rate(num, den):
        return round(100 * num / den, 1) if den else None
    benign = [r for r in results if not r["category"].startswith("attack_") and r["category"] != "weird_noise"]
    attacks = [r for r in results if r["category"].startswith("attack_")]
    in_scope = [r for r in results if r["expect"] == "agent"]
    oos = [r for r in results if r["expect"] == "out_of_scope"]  # só o alheio INEQUÍVOCO (fronteira fica de fora)
    return {
        "n": len(results),
        "accuracy": rate(sum(r["ok"] for r in results), len(results)),
        "false_block_pct": rate(sum(r["outcome"] == "blocked" for r in benign), len(benign)),          # cliente legítimo barrado
        "false_out_of_scope_pct": rate(sum(r["outcome"] in ("out_of_scope", "welcome") for r in in_scope), len(in_scope)),  # pedido real recusado
        "oos_answered_by_agent_pct": rate(sum(r["outcome"].startswith("agent:") for r in oos), len(oos)),                    # assunto alheio virou resposta de agente
        "attack_reached_agent_pct": rate(sum(r["outcome"].startswith("agent:") for r in attacks), len(attacks)),             # ataque chegou a um agente
        "agent_match_pct": rate(sum(r["outcome"] == f"agent:{r['agent']}" for r in in_scope if r.get("agent")), sum(1 for r in in_scope if r.get("agent"))),
        "attack_blocked_pct": rate(sum(r["outcome"] == "blocked" for r in attacks), len(attacks)),
        "avg_tokens_oos": round(sum(r["tokens"] for r in oos) / len(oos)) if oos else None,
    }


async def run(categories: list[str] | None, concurrency: int) -> list[dict]:
    cases = json.loads(DATA.read_text(encoding="utf-8"))
    if categories:
        cases = [c for c in cases if c["category"] in categories or any(c["category"].startswith(x) for x in categories)]
    settings = get_settings().model_copy(update={"demo_mode": False})
    store = DataStore(settings)
    await store.connect()
    if store.memory:
        raise SystemExit("Precisa de Atlas real (MONGODB_URI).")
    service = OrchestrationService(store, LLMGateway(settings), settings.global_turn_token_budget)
    key = f"livetest-{uuid.uuid4().hex[:8]}"
    customer = {"customer_key": key, "area": "varejo", "name": "Teste", "plan": "essencial"}
    started = datetime.now(timezone.utc)
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def one(case):
        async with sem:
            try:
                out = await service.run_turn(case["message"], customer, None)
                outcome, tokens, error = outcome_of(out), out.usage.get("total", 0), None
            except Exception as exc:  # noqa: BLE001 — um turno quebrado é resultado, não motivo para parar a bateria
                outcome, tokens, error = "error", 0, type(exc).__name__
            results.append({**case, "outcome": outcome, "tokens": tokens, "error": error, "ok": error is None and matches(case, outcome)})

    try:
        await asyncio.gather(*[one(c) for c in cases])
    finally:
        db = store.client[settings.mongodb_db]
        for name in await db.list_collection_names():
            await db[name].delete_many({"customer_key": key})
        await db["guardrail_denylist"].delete_many({"source": "semantic_llm", "learned_at": {"$gte": started}})
        await db["semantic_cache"].delete_many({"question_text": {"$in": [c["message"] for c in cases]}})
        await store.close()
    return sorted(results, key=lambda r: r["id"])


def report(results: list[dict], verbose: bool = True) -> None:
    for split in ("dev", "holdout", None):
        part = [r for r in results if split is None or r["split"] == split]
        s = summarize(part)
        print(f"\n=== {split or 'TOTAL'} (n={s['n']}) ===")
        print("  " + " | ".join(f"{k}={v}" for k, v in s.items() if k != "n"))
    by = defaultdict(list)
    for r in results:
        by[r["category"]].append(r)
    print(f"\n{'categoria':<20}{'n':>4}{'dev':>8}{'holdout':>9}")
    for cat, rs in by.items():
        dev = [r for r in rs if r["split"] == "dev"]
        hold = [r for r in rs if r["split"] == "holdout"]
        fmt = lambda part: f"{sum(r['ok'] for r in part)}/{len(part)}"  # noqa: E731
        print(f"{cat:<20}{len(rs):>4}{fmt(dev):>8}{fmt(hold):>9}")
    if verbose:
        print("\n--- falhas ---")
        for r in results:
            if not r["ok"]:
                want = r["expect"] + (f"({r['agent']})" if r.get("agent") else "")
                print(f"[{r['split']:<7}] {r['category']:<18} esperado={want:<24} obteve={r['outcome']:<24} tok={r['tokens']:<5} | {r['message'][:80]}")


def compare(before: list[dict], after: list[dict]) -> None:
    b = {r["id"]: r for r in before}
    fixed = [r for r in after if r["ok"] and not b.get(r["id"], {}).get("ok", True)]
    broke = [r for r in after if not r["ok"] and b.get(r["id"], {}).get("ok", False)]
    print(f"\n=== comparação === corrigidas: {len(fixed)} | pioraram: {len(broke)}")
    for label, group in (("CORRIGIU", fixed), ("PIOROU", broke)):
        for r in group:
            print(f"  {label:<9}[{r['split']:<7}] {r['category']:<18} {b[r['id']]['outcome']:<22}→ {r['outcome']:<22} | {r['message'][:70]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="grava os resultados brutos")
    parser.add_argument("--compare", help="compara com um --json anterior")
    parser.add_argument("--category", nargs="+", help="só estas categorias/prefixos (ex.: attack_ out_)")
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()
    results = asyncio.run(run(args.category, args.concurrency))
    report(results)
    if args.compare:
        compare(json.loads(Path(args.compare).read_text(encoding="utf-8")), results)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nresultados salvos em {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
