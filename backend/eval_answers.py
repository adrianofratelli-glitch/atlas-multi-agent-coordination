"""Eval de RESPOSTA do núcleo agêntico: trajetória, efeito, juiz LLM e consistência (pass^k).

Por que existe: `eval.py` e `eval_routing.py` medem rota e contagem de handoffs. Uma resposta
concatenada e incoerente ("não achei a troca" + "troca registrada") passava 29/29. Aqui cada caso
de `eval/answer_dataset.json` é julgado em quatro eixos, e repetido k vezes:

* trajetória — `expected_agents` ⊆ caminho do turno (ou subsequência, com `strict_order`), nenhum
  `forbidden_agents`, `max_agents`, bloqueio / fora de escopo / pausa HITL conforme o caso;
* efeito — `expect_state` conferido no banco depois do turno (escrita que devia ou não acontecer);
* qualidade — juiz LLM (`app/judge.py`) com a evidência da timeline: média ≥ `min_judge` (4,0);
* consistência — pass@k (algum trial passou) e pass^k (TODOS passaram), por caso e no total.

Em processo, como `eval_routing.py` (não é HTTP). Antes de cada trial o estado de negócio do banco
de TESTE volta ao seed (pedidos, faturas, entregas, pontos; resgates, chamados, decisões, casos
pausados, cache e memória do cliente apagados) e a conversa é nova: um trial não contamina o outro.

    cd backend && MONGODB_DB=multi_agent_poc_test MONGODB_BRAIN_DB=multiagent_brain_test \\
        ../.venv/bin/python eval_answers.py --trials 3 --json out.json [--compare antes.json] [--case ID]
    cd backend && ../.venv/bin/python eval_answers.py --offline      # LLM roteirizado, sem rede (CI)

O modo live escreve dado real e por isso usa `scripts/isolation.py` (recusa o banco da demo sem
`ALLOW_DEMO_DB_WRITE=1`); o relatório também vai para `eval_runs` com `kind: "answers"`.
Custo: um turno multiagente custa ~US$ 0,25 e ~40 s; o juiz soma ~US$ 0,01 por trial.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from math import comb
from pathlib import Path
from statistics import mean
from time import perf_counter
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import judge as judge_module  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import DataStore, utcnow  # noqa: E402
from app.demo_reset import reset_customer_memory  # noqa: E402
from app.llm import LLMGateway  # noqa: E402
from app.memory import EXTRACTOR_PERSONA  # noqa: E402
from app.orchestration import OrchestrationService  # noqa: E402

DATASET = Path(__file__).resolve().parents[1] / "eval" / "answer_dataset.json"
DEFAULT_MIN_JUDGE = 4.0
SPECIALISTS = ("order_agent", "billing_agent", "product_agent", "support_agent", "warranty_agent",
               "loyalty_agent", "logistics_agent")
# Coleções de negócio que o seed define (voltam ao documento do seed) e escritas que só o turno cria.
SEEDED_BUSINESS = {"orders": "order_id", "invoices": "invoice_id", "shipments": "order_id",
                   "loyalty_accounts": "customer_key"}
TURN_WRITES = ("redemptions", "support_tickets", "pending_reviews", "agent_decisions", "agent_audit_events",
               "semantic_cache", "short_term_memory")


# ----------------------------------------------------------------------------------- pass@k / pass^k

def pass_at_k(n: int, c: int, k: int) -> float:
    """Probabilidade de pelo menos 1 sucesso em k tentativas, estimada sem viés de n trials com c sucessos."""
    if not 0 < k <= n or not 0 <= c <= n:
        raise ValueError("exige 0 < k <= n e 0 <= c <= n")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k: probabilidade de as k tentativas TODAS passarem (consistência), estimada de n trials com c sucessos."""
    if not 0 < k <= n or not 0 <= c <= n:
        raise ValueError("exige 0 < k <= n e 0 <= c <= n")
    return comb(c, k) / comb(n, k)


def summarize_case(case_id: str, trials: list[dict], k: int | None = None) -> dict:
    n = len(trials)
    k = k or n
    c = sum(bool(t["passed"]) for t in trials)
    judged = [t["judge"]["media"] for t in trials if (t.get("judge") or {}).get("available")]
    costs = [t["cost_usd"] for t in trials if t.get("cost_usd") is not None]
    return {
        "id": case_id, "trials": n, "passed": c, "k": k,
        "pass_at_k": round(pass_at_k(n, c, k), 4), "pass_hat_k": round(pass_hat_k(n, c, k), 4),
        "trajectory_rate": round(mean(bool(t["checks"].get("trajectory")) for t in trials), 4) if trials else 0,
        "judge_media": round(mean(judged), 2) if judged else None,
        "latency_ms": round(mean(t["latency_ms"] for t in trials), 1) if trials else None,
        "tokens": round(mean(t["tokens"] for t in trials), 1) if trials else None,
        "cost_usd": round(mean(costs), 6) if costs else None,
        "failures": sorted({name for t in trials for name, ok in t["checks"].items() if not ok}),
    }


def summarize_run(per_case: list[dict]) -> dict:
    if not per_case:
        return {"cases": 0}
    trials = sum(row["trials"] for row in per_case)
    judged = [row["judge_media"] for row in per_case if row["judge_media"] is not None]
    costs = [row["cost_usd"] * row["trials"] for row in per_case if row["cost_usd"] is not None]
    return {
        "cases": len(per_case), "trials_total": trials,
        "trial_pass_rate": round(sum(row["passed"] for row in per_case) / trials, 4),
        "pass_at_k": round(mean(row["pass_at_k"] for row in per_case), 4),
        "pass_hat_k": round(mean(row["pass_hat_k"] for row in per_case), 4),
        "trajectory_rate": round(mean(row["trajectory_rate"] for row in per_case), 4),
        "judge_media": round(mean(judged), 2) if judged else None,
        "avg_latency_ms": round(mean(row["latency_ms"] for row in per_case), 1),
        "avg_tokens": round(mean(row["tokens"] for row in per_case), 1),
        "total_cost_usd": round(sum(costs), 4) if costs else None,
    }


# ------------------------------------------------------------------------------------- grading

def agent_path(response) -> list[str]:
    """Caminho de especialistas do turno. `agent_path` cobre cadeia e fan-out; a timeline cobre o resto."""
    path = [a for a in (response.agent_path or []) if a in SPECIALISTS]
    if not path:
        path = [e.agent for e in response.timeline if e.category == "agent" and e.agent in SPECIALISTS]
    return [a for i, a in enumerate(path) if i == 0 or a != path[i - 1]]


def is_subsequence(expected: list[str], path: list[str]) -> bool:
    it = iter(path)
    return all(any(agent == step for step in it) for agent in expected)


def grade_trajectory(case: dict, response, path: list[str]) -> dict:
    checks: dict[str, bool] = {}
    expected = case.get("expected_agents") or []
    checks["expected_agents"] = set(expected) <= set(path)
    if case.get("strict_order"):
        checks["strict_order"] = is_subsequence(expected, path)
    forbidden = set(case.get("forbidden_agents") or [])
    checks["no_forbidden_agent"] = not (forbidden & set(path))
    if case.get("max_agents") is not None:
        checks["max_agents"] = len(set(path)) <= case["max_agents"]
    checks["blocked"] = (response.active_agent == "guardrail") == bool(case.get("expect_blocked"))
    out_of_scope = any((e.result or {}).get("out_of_scope") for e in response.timeline if e.category == "guardrail")
    checks["out_of_scope"] = out_of_scope == bool(case.get("expect_out_of_scope"))
    checks["awaiting_approval"] = bool(response.awaiting_approval) == bool(case.get("expect_awaiting_approval"))
    checks["not_degraded"] = not response.degraded and bool(response.response.strip())
    return checks


async def grade_state(store: DataStore, case: dict) -> dict:
    checks: dict[str, bool] = {}
    for index, spec in enumerate(case.get("expect_state") or []):
        name = f"state[{index}]:{spec['collection']}"
        docs = await store.find_many(spec["collection"], spec["filter"], limit=50)
        if "field" in spec:
            checks[name] = bool(docs) and all(doc.get(spec["field"]) == spec["equals"] for doc in docs)
        else:
            ok = True
            if "min_count" in spec:
                ok = ok and len(docs) >= spec["min_count"]
            if "max_count" in spec:
                ok = ok and len(docs) <= spec["max_count"]
            checks[name] = ok
    return checks


# ------------------------------------------------------------------------------------ state reset

def _assert_isolated(store: DataStore) -> None:
    settings = store.settings
    if store.memory:
        return
    if not (settings.mongodb_db.endswith("_test") and settings.mongodb_brain_db.endswith("_test")):
        from scripts.isolation import allow_demo_write
        if not allow_demo_write():
            raise SystemExit(f"[eval_answers] RECUSADO: reset de estado no banco {settings.mongodb_db}/"
                             f"{settings.mongodb_brain_db}, que não é de teste.")


async def reset_business_state(store: DataStore, customer_keys: list[str]) -> dict:
    """Volta o mundo de negócio ao seed, barato: só as coleções que um turno altera, sem reindexar nada."""
    from app.seed_data import seed_documents

    _assert_isolated(store)
    documents = seed_documents()
    summary: dict[str, int] = {}
    for collection, key in SEEDED_BUSINESS.items():
        for document in documents[collection]:
            await store.replace_one(collection, {key: document[key]}, document, upsert=True)
        summary[collection] = len(documents[collection])
    for collection in TURN_WRITES:
        summary[f"{collection}_removed"] = await store.delete_many(collection, {})
    # Frase aprendida pelo classificador do guardrail mudaria o 2º trial do jailbreak (e não pode ficar).
    summary["learned_denylist_removed"] = await store.delete_many("guardrail_denylist", {"source": "semantic_llm"})
    for key in customer_keys:
        await reset_customer_memory(store, key)
    return summary


# --------------------------------------------------------------------------------- offline LLM

def _text(value: str):
    return SimpleNamespace(type="text", text=value)


def _use(name: str, args: dict):
    return SimpleNamespace(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:10]}", name=name, input=args)


def _message(*blocks):
    stop = "tool_use" if any(block.type == "tool_use" for block in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


class OfflineScriptedLLM(LLMGateway):
    """LLM roteirizado pelo próprio dataset (`offline_script`), como o ScriptedLLM de tests/test_agentic_core.py.

    O plano do turno é `expected_agents` do caso, as tools são as do roteiro e o juiz devolve nota fixa:
    o modo offline prova o harness e os portões do SERVIDOR (escrita, HITL, isolamento), não a redação.
    """

    def __init__(self, settings):
        super().__init__(settings)
        self.anthropic_client = object()
        self.client = True
        self.scripts: dict[str, list] = {}
        self.plan: list[dict] = []

    def load_case(self, case: dict) -> None:
        self.scripts = {}
        for agent, rounds in (case.get("offline_script") or {}).items():
            self.scripts[agent] = [_message(_use(r["tool"], r.get("args") or {})) if "tool" in r
                                   else _message(_text(r["text"])) for r in rounds]
        self.plan = [{"agent": agent, "task": f"parte de {agent}"} for agent in case.get("expected_agents") or []]

    async def complete_tools(self, *, agent, static_context, dynamic_context, messages, tools, budget):
        queue = self.scripts.get(agent["agent_key"])
        if queue:
            return queue.pop(0)
        return _message(_text(f"Parte de {agent['agent_key']} concluída com os dados conferidos."))

    async def complete(self, *, agent, user_message, dynamic_context, budget, static_context=""):
        if agent.get("agent_key") == judge_module.JUDGE_DOC["agent_key"]:
            return json.dumps({"completude": 5, "coerencia": 5, "grounding": 5, "problemas": []}), {}
        if agent.get("persona") == EXTRACTOR_PERSONA:
            return '{"facts": []}', {}
        if "Decomponha" in dynamic_context:
            return (json.dumps({"steps": self.plan}) if self.plan else None), {}
        if "QUADRO DO TURNO" in dynamic_context:
            return None, {}
        if "classificador" in agent.get("persona", "").lower() or agent.get("max_output_tokens") == 40:
            return "OK", {}
        return "Resposta do agente.", {"input_tokens": 1, "output_tokens": 1}


# ------------------------------------------------------------------------------------------- run

def load_dataset(path: Path = DATASET, only: list[str] | None = None) -> dict:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if only:
        dataset["cases"] = [case for case in dataset["cases"] if case["id"] in only]
        if not dataset["cases"]:
            raise SystemExit(f"nenhum caso com id em {only}")
    return dataset


async def run_trial(service, store, llm, case: dict, customer: dict, *, judge: bool) -> dict:
    conversation_id = f"eval-ans-{uuid.uuid4().hex[:10]}"
    started = perf_counter()
    try:
        response = await service.run_turn(case["message"], customer, conversation_id)
    except Exception as exc:  # noqa: BLE001 — o trial falha, o eval segue
        return {"passed": False, "checks": {"turn_error": False}, "error": f"{type(exc).__name__}: {exc}"[:300],
                "latency_ms": round((perf_counter() - started) * 1000, 1), "tokens": 0, "cost_usd": None,
                "judge": {}, "path": []}
    latency = round((perf_counter() - started) * 1000, 1)
    path = agent_path(response)
    trajectory = grade_trajectory(case, response, path)
    checks = {"trajectory": all(trajectory.values()), **{f"traj.{k}": v for k, v in trajectory.items()}}
    checks.update(await grade_state(store, case))
    minimum = case.get("min_judge", DEFAULT_MIN_JUDGE)
    judged: dict = {}
    if judge and minimum is not None:
        judged = await judge_module.judge(llm, case["message"], response.response,
                                          judge_module.evidence_from_timeline(response.timeline))
        checks["judge"] = bool(judged.get("available")) and judged["media"] >= minimum
    economics = response.economics or {}
    turn_cost = economics.get("estimated_cost_usd")
    if turn_cost is None:
        turn_cost = economics.get("known_cost_usd")
    return {
        "passed": all(checks.values()), "checks": checks, "path": path,
        "active_agent": response.active_agent, "route_source": response.route_source,
        "awaiting_approval": response.awaiting_approval, "degraded": response.degraded,
        "latency_ms": latency, "tokens": response.usage.get("total", 0),
        "cost_usd": turn_cost, "judge_cost_usd": judged.get("cost_usd"), "judge": judged,
        "conversation_id": response.conversation_id, "turn_id": response.turn_id,
        "response": response.response[:1500],
        "timeline": [{"category": e.category, "agent": e.agent, "title": e.title[:120]} for e in response.timeline][:40],
    }


async def run(*, offline: bool, trials: int, only: list[str] | None = None, dataset_path: Path = DATASET,
              judge: bool = True, log=print) -> dict:
    dataset = load_dataset(dataset_path, only)
    if offline:
        settings = Settings(_env_file=None, demo_mode=True, mongodb_uri="")
        store = DataStore(settings)
        await store.connect()
        from seed import seed
        await seed(store, create_indexes=False)
        llm = OfflineScriptedLLM(settings)
    else:
        from scripts.isolation import open_test_store
        store, settings = await open_test_store(what="eval_answers (grava turno, efeitos e eval_runs)")
        llm = LLMGateway(settings)
    service = OrchestrationService(store, llm, settings.global_turn_token_budget)
    customers = {row["customer_key"]: row for row in await store.find_many("customers", {}, limit=50)}
    keys = sorted({case["customer"] for case in dataset["cases"]})
    started = perf_counter()
    per_case, raw = [], {}
    try:
        for case in dataset["cases"]:
            results = []
            for trial in range(1, trials + 1):
                await reset_business_state(store, keys)
                if offline:
                    llm.load_case(case)
                log(f"[eval_answers] {case['id']} trial {trial}/{trials} — rodando turno"
                    + ("" if offline else " (multiagente: ~40 s, ~US$ 0,25)") + " ...")
                result = await run_trial(service, store, llm, case, customers[case["customer"]], judge=judge)
                result["trial"] = trial
                results.append(result)
                media = (result.get("judge") or {}).get("media")
                failed = [name for name, ok in result["checks"].items() if not ok]
                log(f"[eval_answers]   {'PASS' if result['passed'] else 'FAIL'} caminho={result['path']} "
                    f"juiz={media} {result['latency_ms'] / 1000:.1f}s tokens={result['tokens']} "
                    f"custo={result['cost_usd']} {('falhas=' + ','.join(failed)) if failed else ''}")
            raw[case["id"]] = results
            per_case.append(summarize_case(case["id"], results))
        # Deixa o banco de teste como o seed deixaria (sem resgates/chamados/frases aprendidas do eval).
        await reset_business_state(store, keys)
        summary = summarize_run(per_case)
        judge_costs = [t.get("judge_cost_usd") or 0 for rows in raw.values() for t in rows]
        summary.update({
            "mode": "offline" if offline else "live", "k": trials,
            "judge_cost_usd": round(sum(judge_costs), 4),
            "duration_s": round(perf_counter() - started, 1),
            "database": "memória (DEMO_MODE)" if offline else f"{settings.mongodb_db}/{settings.mongodb_brain_db}",
            "dataset_sha256": hashlib.sha256(json.dumps(dataset["cases"], sort_keys=True).encode()).hexdigest(),
            "synthetic": dataset.get("synthetic"), "limitation": dataset.get("limitation"),
        })
        report = {"summary": summary, "cases": per_case, "trials": raw}
        await store.insert_one("eval_runs", {"kind": "answers", "at": utcnow(), "summary": summary,
                                             "cases": per_case,
                                             "trials": {cid: [{k: v for k, v in t.items() if k != "timeline"} for t in rows]
                                                        for cid, rows in raw.items()}})
        return report
    finally:
        await store.close()


def print_report(report: dict, compare: dict | None = None) -> None:
    s = report["summary"]
    print(f"\nbanco={s['database']}  modo={s['mode']}  casos={s['cases']}  k={s['k']}  ({s['duration_s']} s)")
    header = f"{'caso':38} {'pass':>5} {'p@k':>5} {'p^k':>5} {'traj':>5} {'juiz':>5} {'lat s':>6} {'tokens':>7} {'US$':>7}  falhas"
    print(header)
    print("-" * len(header))
    for row in report["cases"]:
        judge = f"{row['judge_media']:.2f}" if row["judge_media"] is not None else "-"
        cost = f"{row['cost_usd']:.3f}" if row["cost_usd"] is not None else "-"
        print(f"{row['id'][:38]:38} {row['passed']}/{row['trials']:<3} {row['pass_at_k']:>5.2f} {row['pass_hat_k']:>5.2f} "
              f"{row['trajectory_rate']:>5.2f} {judge:>5} {row['latency_ms'] / 1000:>6.1f} {row['tokens']:>7.0f} {cost:>7}  "
              f"{', '.join(row['failures'])}")
    print("-" * len(header))
    judge = s["judge_media"] if s["judge_media"] is not None else "-"
    print(f"trial pass rate {s['trial_pass_rate']:.1%}  pass@{s['k']} {s['pass_at_k']:.1%}  pass^{s['k']} {s['pass_hat_k']:.1%}  "
          f"trajetória {s['trajectory_rate']:.1%}  juiz {judge}  latência média {s['avg_latency_ms'] / 1000:.1f} s  "
          f"tokens/turno {s['avg_tokens']:.0f}  custo turnos US$ {s['total_cost_usd']}  juiz US$ {s['judge_cost_usd']}")
    if compare:
        before = compare["summary"]
        print("\ncomparação com o arquivo anterior:")
        for key in ("trial_pass_rate", "pass_at_k", "pass_hat_k", "trajectory_rate", "judge_media",
                    "avg_latency_ms", "avg_tokens", "total_cost_usd"):
            old, new = before.get(key), s.get(key)
            delta = f" ({new - old:+.4f})" if isinstance(old, (int, float)) and isinstance(new, (int, float)) else ""
            print(f"  {key}: {old} -> {new}{delta}")
        old_cases = {row["id"]: row for row in compare.get("cases", [])}
        for row in report["cases"]:
            prev = old_cases.get(row["id"])
            if prev and prev["pass_hat_k"] != row["pass_hat_k"]:
                print(f"  {row['id']}: pass^k {prev['pass_hat_k']} -> {row['pass_hat_k']}")
    print(f"\nlimitação declarada: {s['limitation']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="eval de resposta (trajetória + juiz + pass^k)")
    parser.add_argument("--offline", action="store_true", help="LLM roteirizado em memória; sem rede nem Atlas")
    parser.add_argument("--trials", type=int, default=3, help="k: execuções por caso (default 3)")
    parser.add_argument("--case", action="append", help="roda só este id (repetível)")
    parser.add_argument("--json", default="", help="grava o relatório completo")
    parser.add_argument("--compare", default="", help="compara com um relatório anterior")
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials deve ser >= 1")
    report = await run(offline=args.offline, trials=args.trials, only=args.case)
    compare = json.loads(Path(args.compare).read_text(encoding="utf-8")) if args.compare else None
    print_report(report, compare)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nrelatório completo em {args.json}")
    return 0 if report["summary"]["pass_hat_k"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
