"""Estimated LLM cost from provider usage; absent tariffs are never zero cost."""

from math import ceil


def call_cost(call: dict, prices: dict, blended_prices: dict | None = None) -> float | None:
    if call.get("status") != "ok" or not call.get("usage_known", False):
        return None
    if call["model"] not in prices and call["model"] in (blended_prices or {}):
        tokens = sum(call.get(field, 0) for field in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        return round(tokens * blended_prices[call["model"]] / 1_000_000, 10)
    rates = prices.get(call["model"], {})
    total = 0.0
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        tokens = call.get(field, 0)
        if tokens and field not in rates:
            return None
        total += tokens * rates.get(field, 0) / 1_000_000
    return round(total, 10)


def summarize_calls(calls: list[dict]) -> dict:
    costs = [call.get("estimated_cost_usd") for call in calls]
    complete = all(cost is not None for cost in costs)
    return {
        "calls": sum(call.get("status") != "circuit_open" for call in calls),
        "skipped_calls": sum(call.get("status") == "circuit_open" for call in calls),
        "failed_calls": sum(call.get("status") not in ("ok", "circuit_open") for call in calls),
        "cost_complete": complete,
        "estimated_cost_usd": round(sum(costs), 10) if complete else None,
        "known_cost_usd": round(sum(cost for cost in costs if cost is not None), 10),
        "cost_bases": sorted({call.get("cost_basis", "configured_tariffs") for call in calls if call.get("status") != "circuit_open"}),
        "scope": "LLM estimate; configured rates or historical blended average, not provider invoice",
    }


def summarize_evals(results: list[dict]) -> dict:
    passed = sum(bool(row.get("passed")) for row in results)
    latencies = sorted(row["latency_ms"] for row in results if "latency_ms" in row)
    costs = [row.get("economics", {}).get("estimated_cost_usd") for row in results]
    complete = bool(results) and all(cost is not None for cost in costs)
    total = round(sum(costs), 10) if complete else None
    return {
        "total": len(results), "passed": passed, "failed": len(results) - passed,
        "pass_rate": passed / len(results) if results else 0,
        "p95_ms": latencies[ceil(len(latencies) * .95) - 1] if latencies else None,
        "cache_hits": sum(bool(row.get("cache_hit")) for row in results),
        "successful_llm_calls": sum(call.get("status") == "ok" for row in results for call in row.get("llm_calls", [])),
        "cost_coverage": sum(cost is not None for cost in costs) / len(results) if results else 0,
        "estimated_cost_usd": total,
        # Failed attempts are included in the numerator: cost to achieve a successful task.
        "cost_per_success_usd": total / passed if total is not None and passed else None,
    }
