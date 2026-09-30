"""Camada de resiliência da borda das tools e do supervisor.

O gateway de LLM (`app/llm.py`) já tem retry com backoff, fallback de modelo e circuit
breaker por endpoint — não é reimplementado aqui (e não passa a usar `grove_client`, porque
o PoV fala com o SDK Anthropic direto, sem LangChain). O que faltava é o mesmo tratamento na
OUTRA metade das chamadas externas: as tools (consultas e escritas no Atlas), e um supervisor
que não fique esperando para sempre.

A degradação graciosa do supervisor é o PADRÃO: um agente que falha ou não volta nunca termina
o turno em 500 mudo nem em silêncio, com ou sem flag.

    SUPERVISOR_LEGACY_500=1   volta o comportamento antigo (a exceção sobe ao handler global)
    AGENT_TIMEOUT_SECONDS=45  teto por hop de agente
    LOOP_GUARD_REPEATS=2      repetições de (agente, intenção) antes de escalar para humano
    TOOL_TIMEOUT_SECONDS=0    teto por chamada de tool (0 = desligado)
    TOOL_BREAKER=0            desliga o circuit breaker por tool (padrão: ligado)
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from time import monotonic

from . import chaos, observability

TOOL_FAILURE_THRESHOLD = 4
TOOL_OPEN_SECONDS = 30.0


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def tool_breaker_enabled() -> bool:
    """Padrão: ligado. Só abre depois de 4 falhas CONSECUTIVAS da mesma tool, então não muda
    nada em operação normal; `TOOL_BREAKER=0` desliga."""
    return _flag("TOOL_BREAKER", default=True)


def graceful_degradation() -> bool:
    """Padrão: ligada. Só `SUPERVISOR_LEGACY_500=1` devolve a falha crua ao handler global."""
    return not _flag("SUPERVISOR_LEGACY_500")


def agent_timeout_seconds() -> float:
    return float(os.getenv("AGENT_TIMEOUT_SECONDS", "45"))


def loop_guard_repeats() -> int:
    return int(os.getenv("LOOP_GUARD_REPEATS", "2"))


def tool_timeout_seconds() -> float:
    """0 (default) = sem teto por tool, exatamente como antes."""
    return float(os.getenv("TOOL_TIMEOUT_SECONDS", "0"))


class ToolOpenCircuit(RuntimeError):
    """Tool curto-circuitada: o chamador degrada em vez de pagar mais uma falha."""


class _ToolCircuit:
    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if monotonic() - self.opened_at >= TOOL_OPEN_SECONDS:
            self.opened_at = None  # meio-aberto: a próxima chamada decide
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= TOOL_FAILURE_THRESHOLD:
            self.opened_at = monotonic()


_circuits: dict[str, _ToolCircuit] = {}


def reset_circuits() -> None:
    _circuits.clear()


def circuit_state() -> dict[str, dict]:
    return {name: {"failures": circuit.failures, "open": circuit.opened_at is not None}
            for name, circuit in _circuits.items()}


async def call_tool(name: str, coro, **attributes):
    """Executa UMA tool com span, ponto de caos, circuit breaker e (opt-in) teto de tempo.

    O teto existe porque o timeout do supervisor só cobre o que acontece DENTRO de um hop de
    agente — medido com `scripts/chaos_suite.py tool_timeout`: uma consulta pendurada no
    carregamento do turno (registry, regras, memória, conversa) segurava o turno por 20s antes
    de a cadeia sequer começar. `TOOL_TIMEOUT_SECONDS` fecha essa janela.
    """
    limit = tool_timeout_seconds()

    async def body():
        # O ponto de caos entra DENTRO do corpo cronometrado e dentro do try do breaker: uma
        # falha injetada tem de contar como falha da tool e respeitar o teto, como a real.
        if chaos.enabled():
            await chaos.hook("tool", name=name)
        return await coro

    try:
        async with guarded_tool(name, **attributes):
            if limit > 0:
                return await asyncio.wait_for(body(), timeout=limit)
            return await body()
    except BaseException:
        coro.close()   # a corrotina pode nunca ter sido aguardada (caos/circuito aberto)
        raise


class _NullGuard:
    """Caminho rápido: sem tracing, sem breaker e sem caos, a fronteira não custa nada."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


_NULL_GUARD = _NullGuard()


def guarded_tool(name: str, **attributes):
    """Fronteira de UMA tool: span, ponto de caos e (opt-in) circuit breaker."""
    if not (observability.active() or tool_breaker_enabled() or chaos.enabled()):
        return _NULL_GUARD
    return _guarded_tool(name, **attributes)


@asynccontextmanager
async def _guarded_tool(name: str, **attributes):
    circuit = _circuits.setdefault(name, _ToolCircuit())
    if tool_breaker_enabled() and not circuit.allow():
        raise ToolOpenCircuit(f"tool {name} em circuito aberto")
    with observability.span(f"tool.{name}", **{"tool.name": name, **attributes}):
        try:
            yield
        except Exception:
            circuit.failure()
            raise
        else:
            circuit.success()


class LoopGuard:
    """Detecta o supervisor girando: MESMO agente com a MESMA intenção repetidas vezes.

    O teto de hops (`MAX_HOPS`) já limita o tamanho da cadeia, mas uma cadeia curta que
    repete A->B->A gasta budget e entrega a mesma coisa duas vezes. Aqui a repetição vira
    saída controlada para humano, com motivo explícito — nunca silêncio.
    """

    def __init__(self, repeats: int | None = None) -> None:
        self.limit = repeats if repeats is not None else loop_guard_repeats()
        self.seen: dict[tuple[str, str], int] = {}
        self.tripped_on: tuple[str, str] | None = None

    def visit(self, agent: str, intent: str) -> bool:
        """True quando esta visita estoura o limite (o chamador encerra a cadeia)."""
        key = (agent, intent or "")
        self.seen[key] = self.seen.get(key, 0) + 1
        if self.seen[key] > self.limit:
            self.tripped_on = key
            return True
        return False


HUMAN_HANDOFF_REPLY = (
    "Percebi que este atendimento começou a repetir a mesma etapa sem avançar, então interrompi "
    "a cadeia de agentes de propósito em vez de continuar tentando. Um atendente humano assume "
    "daqui — o histórico desta conversa já está registrado para ele."
)


def degraded_reply(agent: str) -> str:
    """Resposta de degradação graciosa: um agente falhou, o turno termina com estado explícito."""
    return (
        f"Não consegui concluir a etapa de {agent.replace('_', ' ')} agora — o serviço demorou "
        "além do limite e eu preferi interromper a esperar indefinidamente. Nada foi processado "
        "nesta etapa. Você pode tentar de novo em instantes ou pedir para falar com um atendente."
    )


async def run_with_timeout(coro, seconds: float):
    """asyncio.wait_for com a semântica que o supervisor espera: cancela e devolve TimeoutError."""
    return await asyncio.wait_for(coro, timeout=seconds)
