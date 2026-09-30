from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.budget import TurnBudget
from app.config import Settings
from app.economics import call_cost, summarize_calls, summarize_evals
from app.llm import LLMGateway, _circuits


def test_historical_estimate_counts_disjoint_token_categories_once():
    call = dict(model="luna", status="ok", usage_known=True, input_tokens=8000,
                output_tokens=3183, cache_read_tokens=0, cache_write_tokens=0)
    assert call_cost(call, {}, {"luna": 2.21}) == .02471443
    call.update(input_tokens=7000, cache_read_tokens=1000)
    assert call_cost(call, {}, {"luna": 2.21}) == .02471443
    assert call_cost({**call, "usage_known": False}, {}, {"luna": 2.21}) is None


def test_detailed_prices_take_precedence_over_blended_average():
    call = dict(model="m", status="ok", usage_known=True, input_tokens=1000)
    assert call_cost(call, {"m": {"input_tokens": 1}}, {"m": 2.21}) == .001


def test_cached_input_is_priced_separately_and_missing_tariff_is_unknown():
    call = dict(model="m", status="ok", usage_known=True, input_tokens=100,
                output_tokens=10, cache_read_tokens=900, cache_write_tokens=0)
    assert call_cost(call, {}) is None
    assert call_cost(call, {"m": {"input_tokens": 2, "output_tokens": 10, "cache_read_tokens": .2}}) == .00048
    assert summarize_calls([{"estimated_cost_usd": .1}, {"estimated_cost_usd": None}])["estimated_cost_usd"] is None


def test_cost_per_success_includes_failed_tasks_and_no_success_is_undefined():
    results = [{"passed": True, "latency_ms": 10, "economics": {"estimated_cost_usd": .1}},
               {"passed": False, "latency_ms": 100, "economics": {"estimated_cost_usd": .2}}]
    report = summarize_evals(results)
    assert report["cost_per_success_usd"] == .3
    assert report["p95_ms"] == 100
    assert summarize_evals(results[1:])["cost_per_success_usd"] is None


@pytest.mark.parametrize("url", ["https://evil.test/", "https://grove-gateway-prod.azure-api.net.evil.test/", "http://grove-gateway-prod.azure-api.net/", "https://user@ grove-gateway-prod.azure-api.net/"])
def test_grove_credential_cannot_target_arbitrary_hosts(url):
    with pytest.raises(ValueError):
        LLMGateway(Settings(_env_file=None, grove_api_key="test", grove_anthropic_base_url=url))


@pytest.mark.parametrize("rate", [-1, float("inf"), float("nan")])
def test_invalid_tariffs_are_rejected(rate):
    with pytest.raises(ValueError):
        Settings(_env_file=None, llm_prices={"m": {"input_tokens": rate}})


async def test_fallback_records_each_attempt_without_exposing_prompts():
    _circuits.clear()
    gateway = LLMGateway(Settings(_env_file=None))
    gateway.client = True
    gateway._request = AsyncMock(side_effect=[RuntimeError("sensitive body"),
        ("ok", {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0}, True)])
    budget = TurnBudget(1000, {"a": 1000})
    result, _ = await gateway.complete(agent={"agent_key": "a", "model": "m1", "fallback_model": "m2", "persona": "secret", "max_turn_tokens": 100}, user_message="private", dynamic_context="context", budget=budget)
    assert result == "ok"
    assert [call["model"] for call in budget.llm_calls] == ["m1", "m2"]
    assert budget.llm_calls[-1]["fallback"] is True
    assert "sensitive" not in str(budget.llm_calls)
    assert "private" not in str(budget.llm_calls)


async def test_openai_adapter_uses_explicit_endpoint_and_normalizes_cached_tokens(monkeypatch):
    seen = []
    url = "https://grove-gateway-prod.azure-api.net/test/chat/completions"
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 80}}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    gateway = LLMGateway(Settings(_env_file=None, grove_api_key="test-key", grove_chat_completions_url=url, grove_openai_models=["model-test"]))
    text, counts, known = await gateway._request("model-test", "s", "d", "u", 20)
    assert text == "ok" and known
    assert counts["input_tokens"] == 20 and counts["cache_read_tokens"] == 80
    assert str(seen[0].url) == url and seen[0].headers["api-key"] == "test-key"


def test_replayed_agent_event_is_not_a_generation(monkeypatch):
    from app import langfuse_client
    from app.models import TimelineEvent
    generations, spans = [], []
    trace = SimpleNamespace(generation=lambda **kw: generations.append(kw), span=lambda **kw: spans.append(kw), update=lambda **kw: None, get_trace_url=lambda: "trace")
    monkeypatch.setattr(langfuse_client, "get_langfuse", lambda: SimpleNamespace(trace=lambda **kw: trace))
    langfuse_client.build_turn_trace(conversation_id="c", customer_key="a", message="m", response="r", active_agent="a", timeline=[TimelineEvent(category="agent", title="cached", replayed=True)])
    assert not generations and spans[0]["metadata"]["replayed"]


def test_outcome_requires_real_write_and_balanced_debit():
    from eval import grade_outcome
    empty = {name: [] for name in ("orders", "shipments", "loyalty_accounts", "redemptions", "support_tickets", "pending_reviews")}
    case = {"case_id": "return", "message": "trocar PED-1001", "expect_write_collection": "orders"}
    assert not all(grade_outcome(case, empty, empty).values())
    assert all(grade_outcome(case, empty, {**empty, "orders": [{"order_id": "PED-1001", "status": "troca_solicitada"}]}).values())
    case = {"expect_write_collection": "redemptions"}
    before = {**empty, "loyalty_accounts": [{"points": 1000}]}
    after = {**before, "redemptions": [{"redemption_id": "x", "points_spent": 300}]}
    assert not grade_outcome(case, before, after)["balanced_points_debit"]


async def test_cache_hit_preserves_replay_but_does_not_count_old_operations():
    from app.database import DataStore
    from app.metrics import metrics
    from app.orchestration import OrchestrationService
    from seed import seed
    settings = Settings(_env_file=None, demo_mode=True)
    store = DataStore(settings)
    await seed(store, create_indexes=False)
    customer = await store.find_one("customers", {"customer_key": "ana"})
    service = OrchestrationService(store, LLMGateway(settings), 20000)
    message = "onde está o meu pedido PED-1001 e qual é o valor e o vencimento da fatura FAT-1001?"
    first = await service.run_turn(message, customer, None)
    before = metrics.snapshot()["counters"].get("collection.orders.read", 0)
    second = await service.run_turn(message, customer, first.conversation_id)
    assert second.cache_hit
    assert any(event.replayed for event in second.timeline)
    assert metrics.snapshot()["counters"].get("collection.orders.read", 0) == before
    traces = await store.find_many("agent_traces", {"conversation_id": first.conversation_id})
    assert all(trace["duration_ms"] > 0 for trace in traces)
    assert second.economics["estimated_cost_usd"] == 0 and not second.llm_calls


async def test_truncated_response_is_charged_without_being_accepted():
    _circuits.clear()
    settings = Settings(_env_file=None, llm_prices={"m": {"input_tokens": 1, "output_tokens": 2}})
    gateway = LLMGateway(settings)
    gateway.client = True
    gateway._request = AsyncMock(return_value=(None, dict(input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0), True))
    budget = TurnBudget(1000, {"a": 1000})
    text, _ = await gateway.complete(agent=dict(agent_key="a", model="m", persona="p", max_turn_tokens=500), user_message="u", dynamic_context="d", budget=budget)
    assert text is None
    assert budget.llm_calls[0]["status"] == "incomplete"
    assert budget.llm_calls[0]["estimated_cost_usd"] == .0002
    assert budget.total_used == 150


async def test_empty_dynamic_context_never_sends_an_empty_system_block():
    """A API rejeita bloco de texto vazio (BadRequestError). Contexto dinâmico vazio é legítimo: só não vira bloco."""
    captured = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=usage)

    gateway = LLMGateway(Settings(_env_file=None, anthropic_api_key="test"))
    gateway.anthropic_client = SimpleNamespace(messages=FakeMessages())
    gateway.client = gateway.anthropic_client
    text, *_ = await gateway._request("claude-haiku-4-5", "persona", "", "oi", 50)
    assert text == "ok"
    assert [block["text"] for block in captured["system"]] == ["persona"]
    await gateway._request("claude-haiku-4-5", "persona", "contexto", "oi", 50)
    assert [block["text"] for block in captured["system"]] == ["persona", "contexto"]


async def test_temperature_is_sent_only_when_requested_and_dropped_for_models_that_reject_it():
    """Classificação precisa ser determinística (temperature 0). Nem todo modelo aceita o parâmetro: nesse caso repete sem ele."""
    import anthropic

    calls = []

    class FakeMessages:
        def __init__(self, reject):
            self.reject = reject

        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if self.reject and "temperature" in kwargs:
                response = SimpleNamespace(status_code=400, request=SimpleNamespace(), headers={}, text="temperature is deprecated")
                raise anthropic.BadRequestError("`temperature` is deprecated for this model", response=response, body=None)
            usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=usage)

    gateway = LLMGateway(Settings(_env_file=None, anthropic_api_key="test"))
    gateway.anthropic_client = SimpleNamespace(messages=FakeMessages(reject=False))
    gateway.client = gateway.anthropic_client
    await gateway._request("claude-haiku-4-5", "p", "c", "oi", 50)
    assert "temperature" not in calls[-1]                       # padrão: não manda
    await gateway._request("claude-haiku-4-5", "p", "c", "oi", 50, temperature=0)
    assert calls[-1]["temperature"] == 0
    gateway.anthropic_client = SimpleNamespace(messages=FakeMessages(reject=True))
    text, *_ = await gateway._request("claude-haiku-4-5", "p", "c", "oi", 50, temperature=0)
    assert text == "ok" and "temperature" not in calls[-1]      # modelo rejeitou: repete sem o parâmetro
