"""Eval harness: roda o golden dataset (app/seed_data.py:EVAL_CASES) contra o servidor no ar,
mede GoalSuccessRate por caso e grava o resultado como documento em eval_runs — histórico de
qualidade fica no mesmo banco de dados, consultável como qualquer outro dado operacional.

NÃO é redundante com `eval_routing.py`: aqui é caixa-preta por HTTP contra um servidor de
verdade, com verificação de EFEITO (status de pedido persistido, débito de pontos conferido,
chamado aberto) e histórico em `eval_runs`. O `eval_routing.py` mede roteamento/resolução em
processo, com outro dataset. Os dois ficam.

Como este modo escreve dado real (o turno grava, e o relatório vai para `eval_runs`), ele segue
a mesma proteção dos outros scripts (`scripts/isolation.py`): **recusa rodar contra o banco da
demo**. Suba o servidor apontado para o banco de teste e rode o eval com o mesmo banco:

    cd backend && MONGODB_DB=multi_agent_poc_test MONGODB_BRAIN_DB=multiagent_brain_test python run.py
    cd backend && MONGODB_DB=multi_agent_poc_test MONGODB_BRAIN_DB=multiagent_brain_test python eval.py

Para medir a demo mesmo (gastando estado dela), passe `ALLOW_DEMO_DB_WRITE=1` conscientemente.
`--offline` usa fixtures em memória e não toca banco nenhum, então não passa pela guarda.

Uso: python eval.py [URL]
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import Settings, get_settings  # noqa: E402
from app.database import DataStore, utcnow  # noqa: E402
from app.seed_data import EVAL_CASES  # noqa: E402
from scripts.isolation import guard  # noqa: E402


def grade_case(case: dict, body: dict) -> dict:

    checks: list[tuple[str, bool]] = []
    if case.get("expect_blocked"):
        checks.append(("blocked", body["active_agent"] == "guardrail"))
    else:
        checks.append(("not_blocked", body["active_agent"] != "guardrail"))
    if case.get("expect_agent"):
        checks.append(("active_agent", body["active_agent"] == case["expect_agent"]))
    if case.get("expect_route_source"):
        checks.append(("route_source", body["route_source"] == case["expect_route_source"]))
    if case.get("expect_contains"):
        checks.append(("contains", case["expect_contains"] in body["response"]))
    raw_agent_sequence = [
        event["agent"]
        for event in body.get("timeline", [])
        if event.get("category") == "agent" and event.get("agent")
    ]
    agent_sequence = [
        agent
        for index, agent in enumerate(raw_agent_sequence)
        if index == 0 or agent != raw_agent_sequence[index - 1]
    ]
    if "expected_agents" in case:
        checks.append(("agent_sequence", agent_sequence == case["expected_agents"]))
    if "expect_handoffs" in case:
        handoffs = sum(1 for event in body.get("timeline", []) if event.get("category") == "handoff")
        checks.append(("handoffs", handoffs == case["expect_handoffs"]))
    if "expect_revisit" in case:
        has_revisit = len(agent_sequence) != len(set(agent_sequence))
        checks.append(("revisit", has_revisit == case["expect_revisit"]))

    passed = all(ok for _, ok in checks)
    return {
        "case_id": case["case_id"],
        "message": case["message"],
        "passed": passed,
        "checks": {name: ok for name, ok in checks},
        "active_agent": body["active_agent"],
        "route_source": body["route_source"],
        "agent_sequence": agent_sequence,
        "response_preview": body["response"][:200],
        "cache_hit": body.get("cache_hit", False),
        "economics": body.get("economics", {}),
        "llm_calls": body.get("llm_calls", []),
    }


BUSINESS_COLLECTIONS = ("orders", "shipments", "loyalty_accounts", "redemptions", "support_tickets", "pending_reviews")


async def snapshot(store, customer_key):
    # Bounded demo fixtures; fail rather than silently truncate an outcome verification.
    result = {}
    for collection in BUSINESS_COLLECTIONS:
        owner_field = "owner_customer_key" if collection in ("orders", "shipments") else "customer_key"
        docs = await store.find_many(collection, {owner_field: customer_key}, limit=1001)
        if len(docs) > 1000:
            raise ValueError("Eval requer fixture isolada com no máximo 1000 documentos por coleção")
        result[collection] = docs
    return result


def grade_outcome(case, before, after):
    import re
    checks = {}
    collection = case.get("expect_write_collection")
    if not collection:
        checks["no_unexpected_business_write"] = before == after
    elif collection == "orders":
        order_id = re.search(r"PED-\d+", case["message"]).group()
        expected_status = "reembolsado" if case["case_id"] == "diego-refund-return" else "troca_solicitada"
        checks["persisted_order_status"] = any(
            doc.get("order_id") == order_id and doc.get("status") == expected_status
            for doc in after["orders"])
    elif collection == "shipments":
        order_id = re.search(r"PED-\d+", case["message"]).group()
        checks["persisted_reschedule"] = any(
            doc.get("order_id") == order_id and doc.get("reschedule_requested") is True
            for doc in after["shipments"])
    elif collection == "support_tickets":
        previous = {doc.get("ticket_id") for doc in before[collection]}
        checks["persisted_new_ticket"] = any(
            doc.get("ticket_id") not in previous and doc.get("status") == "aberto"
            for doc in after[collection])
    elif collection == "redemptions":
        previous = {doc.get("redemption_id") for doc in before[collection]}
        new = [doc for doc in after[collection] if doc.get("redemption_id") not in previous]
        before_points = sum(doc["points"] for doc in before["loyalty_accounts"])
        after_points = sum(doc["points"] for doc in after["loyalty_accounts"])
        checks["persisted_redemption"] = any(doc.get("reward") == "voucher de R$ 30" and doc.get("points_spent") == 500 and doc.get("status") == "confirmado" for doc in after[collection])
        checks["balanced_points_debit"] = before_points - after_points == sum(doc["points_spent"] for doc in new)
    elif collection == "pending_reviews":
        order_id = re.search(r"PED-\d+", case["message"]).group()
        checks["persisted_pending_review"] = any(doc.get("status") == "pending" and doc.get("subject_id") == order_id for doc in after[collection])
    else:
        checks["outcome_supported"] = False
    return checks


async def run(args):
    import hashlib
    import json
    from app.economics import summarize_evals
    settings = Settings(_env_file=None, demo_mode=True) if args.offline else get_settings()
    if not args.offline and settings.use_memory_store:
        raise ValueError("Use --offline para DEMO_MODE; eval externo exige o mesmo Atlas do servidor")
    if not args.offline:
        # O turno grava de verdade e o relatório vai para eval_runs: mesma guarda dos demais
        # scripts. O banco tem de ser o MESMO do servidor sob teste, então aqui não se troca o
        # destino automaticamente — recusa-se o da demo e diz como apontar os dois para o teste.
        guard(settings, what="eval.py (modo live, grava o turno e eval_runs)",
              hint=("Suba o servidor E rode o eval com MONGODB_DB=multi_agent_poc_test "
                    "MONGODB_BRAIN_DB=multiagent_brain_test (o eval precisa do MESMO banco do servidor),"))
    store = DataStore(settings)
    await store.connect()
    results = []
    started = time.perf_counter()
    try:
        if args.offline:
            from seed import seed
            from app.llm import LLMGateway
            from app.orchestration import OrchestrationService
            await seed(store, create_indexes=False)
            service = OrchestrationService(store, LLMGateway(settings), settings.global_turn_token_budget)
        selected = [case for case in EVAL_CASES if not args.case or case["case_id"] in args.case]
        if not selected:
            raise ValueError("Nenhum caso selecionado")
        async with httpx.AsyncClient(base_url=args.url, timeout=180, follow_redirects=False) as client:
            for trial in range(args.repeats):
                for case in selected:
                    case_started = time.perf_counter()
                    try:
                        before = await snapshot(store, case["customer_key"])
                        if args.offline:
                            turn_started = time.perf_counter()
                            customer = await store.find_one("customers", {"customer_key": case["customer_key"]})
                            body = (await service.run_turn(case["message"], customer, None)).model_dump()
                        else:
                            auth = await client.post("/api/auth/token", json={"customer_key": case["customer_key"]})
                            auth.raise_for_status()
                            turn_started = time.perf_counter()
                            response = await client.post("/api/chat", headers={"Authorization": f"Bearer {auth.json()['access_token']}"}, json={"message": case["message"]})
                            response.raise_for_status()
                            body = response.json()
                        turn_latency = (time.perf_counter() - turn_started) * 1000
                        after = await snapshot(store, case["customer_key"])
                        result = grade_case(case, body)
                        result["checks"].update(grade_outcome(case, before, after))
                        result["passed"] = all(result["checks"].values())
                    except Exception as exc:
                        turn_latency = (time.perf_counter() - case_started) * 1000
                        result = {"case_id": case["case_id"], "passed": False, "checks": {}, "error_type": type(exc).__name__}
                    result.update(trial=trial + 1, latency_ms=round(turn_latency, 2))
                    results.append(result)
                    print(f"{'✓' if result['passed'] else '✗'} {result['case_id']} (trial {trial + 1})")
        report = {"at": utcnow().isoformat(), "label": args.label,
                  "mode": "offline-contracts" if args.offline else "live",
                  "dataset_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest(),
                  "repeat_policy": "sequential; state and cache preserved; not independent fresh trials",
                  "duration_s": round(time.perf_counter() - started, 2),
                  **summarize_evals(results), "results": results}
        report["measurement_scope"] = "offline contracts; no inference benchmark" if args.offline else "LLM estimated cost; end-to-end chat request latency"
        if args.offline:
            report.update(estimated_cost_usd=None, cost_per_success_usd=None, cost_coverage=0)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        if not args.offline:
            await store.insert_one("eval_runs", {**report, "at": utcnow()})
        print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
        return 1 if report["failed"] else 0
    finally:
        await store.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8031")
    parser.add_argument("--offline", action="store_true", help="Fixtures em memória; sem Atlas ou LLM, valida contratos")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--case", action="append")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats deve ser positivo")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
