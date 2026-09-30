"""Injeção de falha controlada — DESLIGADA salvo `CHAOS=1` no ambiente.

O PoV precisa tentar se quebrar antes do cliente. Em vez de mocks espalhados pelos testes,
os pontos de falha ficam no caminho real (gateway de LLM, fronteira de tool, execução de
agente) e só fazem alguma coisa quando o ambiente pede. Sem `CHAOS=1` cada `hook()` é uma
leitura de variável de ambiente e um `return` — nenhum caminho de produção muda.

Variáveis (lidas a CADA chamada de propósito: o teste troca o cenário entre casos):

    CHAOS=1                enable geral
    CHAOS_SCENARIO         timeout | status | hang | none
    CHAOS_TARGET           substring casada contra "<ponto>:<nome>" ("" = todos)
    CHAOS_PHASE            fase exigida ("" = qualquer): before_first_token, mid_stream,
                           between_handoffs
    CHAOS_STATUS           código HTTP simulado do provedor (default 429)
    CHAOS_DELAY            segundos de atraso para timeout/hang (default 30)
    CHAOS_COUNT            dispara só nas N primeiras vezes ("" = sempre)

`malformed` não é um cenário de `hook()`: payload corrompido tem que sair de onde o dado
nasce, então é `mangle()`, aplicado no retorno da tool.
"""

from __future__ import annotations

import asyncio
import os

_fired: dict[str, int] = {}


def enabled() -> bool:
    return os.getenv("CHAOS", "").strip().lower() in {"1", "true", "yes", "on"}


def reset() -> None:
    """Zera o contador de disparos (um teste por cenário)."""
    _fired.clear()


def _armed(point: str, name: str, phase: str) -> bool:
    if not enabled():
        return False
    target = os.getenv("CHAOS_TARGET", "").strip()
    if target and target not in f"{point}:{name}":
        return False
    wanted_phase = os.getenv("CHAOS_PHASE", "").strip()
    if wanted_phase and wanted_phase != phase:
        return False
    limit = os.getenv("CHAOS_COUNT", "").strip()
    key = f"{point}:{name}:{phase}"
    count = _fired.get(key, 0)
    if limit and count >= int(limit):
        return False
    _fired[key] = count + 1
    return True


class ChaosProviderError(Exception):
    """Erro de provedor simulado; carrega `status_code` como o SDK real carrega."""

    def __init__(self, status_code: int):
        super().__init__(f"chaos: provedor simulado devolveu {status_code}")
        self.status_code = status_code


async def hook(point: str, *, name: str = "", phase: str = "") -> None:
    """Ponto de injeção. Fora de CHAOS=1 não faz nada."""
    scenario = os.getenv("CHAOS_SCENARIO", "").strip().lower()
    if scenario in ("", "none") or not _armed(point, name, phase):
        return
    if scenario in ("timeout", "hang"):
        await asyncio.sleep(float(os.getenv("CHAOS_DELAY", "30")))
        return
    if scenario == "status":
        raise ChaosProviderError(int(os.getenv("CHAOS_STATUS", "429")))


def mangle(point: str, name: str, value):
    """Corrompe o retorno de uma tool quando o cenário `malformed` está armado.

    Lista vira lista vazia, documento vira `None`, número vira string — as três formas que
    realmente chegam de um driver/índice com problema.
    """
    if os.getenv("CHAOS_SCENARIO", "").strip().lower() != "malformed" or not _armed(point, name, ""):
        return value
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return None
    if isinstance(value, int):
        return "?"
    return value
