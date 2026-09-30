"""Regressão permanente dos cenários de caos (mesma implementação de scripts/chaos_suite.py).

Só roda com CHAOS=1 — a suíte normal continua medindo o comportamento sem injeção. Cada teste
cobra a MESMA assertion do cenário, então o que quebrar aqui quebrou de verdade a resiliência,
não o roteiro da demo.

    cd backend && CHAOS=1 ../.venv/bin/python -m pytest tests/test_chaos.py -q
"""

import os

import pytest

from scripts import chaos_suite

pytestmark = pytest.mark.skipif(
    os.getenv("CHAOS", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="cenários de caos só rodam com CHAOS=1",
)


async def _assert_scenario(name: str):
    verdict = await chaos_suite.SCENARIOS[name]()
    assert verdict.passed, f"{name}: {verdict.detail} | esperado: {verdict.assertion}"
    return verdict


async def test_tool_timeout_is_bounded_by_the_tool_ceiling():
    verdict = await _assert_scenario("tool_timeout")
    assert verdict.elapsed_s < 5


async def test_hung_agent_is_interrupted_by_the_supervisor():
    verdict = await _assert_scenario("agent_hang")
    assert verdict.elapsed_s < 5


async def test_provider_429_before_first_token_still_answers():
    await _assert_scenario("llm_429_before_first_token")


async def test_provider_failure_after_the_response_never_returns_half_an_answer():
    await _assert_scenario("llm_500_mid_stream")


async def test_provider_failure_between_handoffs_does_not_duplicate_the_handoff():
    await _assert_scenario("llm_error_between_handoffs")


async def test_malformed_tool_payload_never_invents_data():
    await _assert_scenario("tool_malformed_payload")


async def test_failing_tool_opens_its_circuit():
    await _assert_scenario("tool_circuit_breaker")


async def test_loop_guard_escalates_to_a_human():
    await _assert_scenario("loop_guard")


async def test_legacy_flag_restores_the_old_500_behaviour():
    await _assert_scenario("legacy_500_flag")


async def test_concurrent_requests_on_one_conversation_do_not_corrupt_state():
    await _assert_scenario("concurrent_same_conversation")


@pytest.mark.live
@pytest.mark.skipif(os.getenv("LIVE") != "1", reason="crash-resume precisa de Atlas real: LIVE=1")
async def test_state_survives_sigkill_mid_conversation():
    verdict = await chaos_suite.SCENARIOS["crash_resume"]()
    if verdict.skipped:
        pytest.skip(verdict.detail)
    assert verdict.passed, verdict.detail
