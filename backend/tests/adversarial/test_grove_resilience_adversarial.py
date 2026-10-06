"""Grove fora do ar, lento ou limitando: o caminho REAL do LLMGateway (SDK Anthropic e rota
Chat Completions via httpx), com o transporte HTTP simulado. Nenhuma chamada de rede.

Cobre o que a resiliência promete sem flag: retry em 429/5xx/timeout, fallback de modelo,
circuit breaker por endpoint que curto-circuita a chamada seguinte, e erro de configuração
(404 de modelo) que NÃO abre o circuito dos outros agentes.
"""

import anthropic
import httpx
import pytest

from app import llm as llm_module
from app.budget import TurnBudget
from app.config import Settings
from app.llm import LLMGateway, _circuits

GW = "https://gw.mongodb.com/anthropic"
CHAT = "https://gw.mongodb.com/test/chat/completions"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    _circuits.clear()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(llm_module.asyncio, "sleep", no_sleep)
    yield
    _circuits.clear()


def _anthropic_ok():
    return httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 1}})


def _gateway(script):
    """Gateway pela rota Anthropic do Grove; `script` decide a resposta de cada chamada."""
    calls = []

    def handler(request):
        calls.append(request)
        return script(len(calls), request)

    gateway = LLMGateway(Settings(_env_file=None, grove_api_key="k", grove_anthropic_base_url=GW))
    gateway.anthropic_client = anthropic.AsyncAnthropic(
        api_key="k", base_url=GW, max_retries=0, timeout=2,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return gateway, calls


AGENT = {"agent_key": "a", "model": "m1", "fallback_model": "m2", "persona": "p", "max_turn_tokens": 2000}


async def _complete(gateway):
    budget = TurnBudget(5000, {"a": 5000})
    text, _ = await gateway.complete(agent=AGENT, user_message="u", dynamic_context="", budget=budget)
    return text, budget.llm_calls


async def test_429_is_retried_and_the_turn_succeeds():
    gateway, calls = _gateway(lambda n, _: httpx.Response(429, json={}) if n == 1 else _anthropic_ok())
    text, records = await _complete(gateway)
    assert text == "ok" and len(calls) == 2
    assert [r["status"] for r in records] == ["error", "ok"]


async def test_persistent_5xx_falls_back_to_the_second_model():
    def script(n, request):
        body = request.read().decode()
        return _anthropic_ok() if '"m2"' in body else httpx.Response(503, json={})
    gateway, calls = _gateway(script)
    text, records = await _complete(gateway)
    assert text == "ok"
    assert [r["model"] for r in records] == ["m1", "m1", "m1", "m2"]
    assert records[-1]["fallback"] is True


async def test_timeout_is_retried_not_fatal():
    def script(n, request):
        if n == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _anthropic_ok()
    gateway, calls = _gateway(script)
    text, records = await _complete(gateway)
    assert text == "ok" and records[0]["error_type"] in {"APITimeoutError", "APIConnectionError"}


async def test_breaker_opens_and_short_circuits_without_calling_the_provider():
    gateway, calls = _gateway(lambda n, _: httpx.Response(500, json={}))
    text, _ = await _complete(gateway)  # 3 tentativas m1 + 1 de m2 = 4 falhas consecutivas → abre
    assert text is None
    made = len(calls)
    text, records = await _complete(gateway)
    assert text is None and len(calls) == made, "circuito aberto não pode bater no provedor"
    assert {r["status"] for r in records} == {"circuit_open"}


async def test_model_not_found_does_not_open_the_shared_breaker():
    """Um agente com modelo inexistente (404) não pode derrubar os outros pelo breaker do endpoint."""
    gateway, calls = _gateway(lambda n, _: httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "x"}}))
    for _ in range(3):
        await _complete(gateway)
    breaker = _circuits[GW]
    assert await breaker.allow() and breaker._consecutive_failures == 0


async def test_chat_completions_route_retries_5xx_and_sends_real_key(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(502, json={})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    gateway = LLMGateway(Settings(_env_file=None, grove_api_key="real-key", grove_chat_completions_url=CHAT,
                                  grove_openai_models=["gpt-x"]))
    monkeypatch.setattr(httpx, "AsyncClient", Mocked)  # depois do SDK: ele valida isinstance no construtor
    budget = TurnBudget(5000, {"a": 5000})
    text, _ = await gateway.complete(agent={**AGENT, "model": "gpt-x", "fallback_model": "gpt-x"},
                                     user_message="u", dynamic_context="", budget=budget)
    assert text == "ok" and len(seen) == 2
    assert all(r.headers["x-api-key"] == "real-key" and r.headers["authorization"] == "Bearer real-key" for r in seen)
