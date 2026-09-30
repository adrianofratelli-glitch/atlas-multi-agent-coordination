"""Warmup automático: aquece perguntas GENÉRICAS por área (as únicas que o cache pode servir), sozinho,
uma vez por janela, sem derrubar nada e sem custo descontrolado."""

import asyncio

from app.config import Settings
from app.database import DataStore
from app.warmup import GENERIC_WARMUP_PROMPTS, WarmupService
from seed import seed

AREAS = {"varejo", "financeiro"}  # áreas dos clientes semeados


class FakeOrchestrator:
    def __init__(self, fail_on=None, delay=0.0):
        self.calls, self.fail_on, self.delay = [], fail_on, delay

    async def run_turn(self, message, customer, conversation_id):
        await asyncio.sleep(self.delay)
        self.calls.append((customer, message))
        if message == self.fail_on:
            raise RuntimeError("LLM caiu")


async def service(orch, *, has_llm=True, cooldown=45, clock=None):
    store = DataStore(Settings(demo_mode=True))
    await store.connect()
    await seed(store, create_indexes=False)
    return WarmupService(store, orch, has_llm=has_llm, cooldown_minutes=cooldown, clock=clock or (lambda: 0.0))


async def test_warms_each_generic_prompt_once_per_area_with_a_neutral_identity():
    orch = FakeOrchestrator()
    state = await (await service(orch)).run()
    assert len(orch.calls) == len(GENERIC_WARMUP_PROMPTS) * len(AREAS)
    assert {c["area"] for c, _ in orch.calls} == AREAS
    # identidade neutra: sem orçamento nem histórico de cliente real, e nunca a de um cliente da demo
    assert all(c["customer_key"].startswith("warmup-") for c, _ in orch.calls)
    assert state["status"] == "warm" and state["failed"] == 0


async def test_second_call_inside_cooldown_costs_nothing():
    orch, now = FakeOrchestrator(), [0.0]
    svc = await service(orch, clock=lambda: now[0])
    await svc.run()
    n = len(orch.calls)
    now[0] = 44 * 60
    assert (await svc.run())["status"] == "warm" and len(orch.calls) == n
    now[0] = 46 * 60  # o TTL do cache está perto de expirar: aquece de novo
    await svc.run()
    assert len(orch.calls) == 2 * n


async def test_concurrent_triggers_share_one_run():
    orch = FakeOrchestrator(delay=0.01)
    svc = await service(orch)
    await asyncio.gather(*[svc.run() for _ in range(10)])
    assert len(orch.calls) == len(GENERIC_WARMUP_PROMPTS) * len(AREAS)


async def test_one_failing_turn_does_not_stop_the_rest_and_is_reported():
    orch = FakeOrchestrator(fail_on=GENERIC_WARMUP_PROMPTS[0])
    state = await (await service(orch)).run()
    assert state["failed"] == len(AREAS) and len(orch.calls) == len(GENERIC_WARMUP_PROMPTS) * len(AREAS)


async def test_a_failed_run_can_be_retried_immediately():
    orch = FakeOrchestrator(fail_on=GENERIC_WARMUP_PROMPTS[0])
    svc = await service(orch)
    await svc.run()
    orch.fail_on = None
    assert (await svc.run())["failed"] == 0


async def test_without_llm_nothing_runs():
    orch = FakeOrchestrator()
    state = await (await service(orch, has_llm=False)).run()
    assert state["status"] == "disabled" and orch.calls == []


async def test_trigger_returns_immediately_and_runs_in_background():
    orch = FakeOrchestrator(delay=0.02)
    svc = await service(orch)
    assert svc.trigger()["status"] == "running"
    await svc.wait()
    assert svc.state()["status"] == "warm"
