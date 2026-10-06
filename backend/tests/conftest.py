"""Isolamento da suíte offline em relação ao ambiente do processo que a executa.

`Settings` (pydantic-settings) lê `os.environ` antes do `.env`. Quem roda o pytest de dentro
de uma ferramenta que já exporta `ANTHROPIC_BASE_URL` (ex.: um terminal configurado para o
gateway) via a suíte trocar de rota sem perceber: o `LLMGateway` de um teste que deveria usar só
a rota Chat Completions passa a montar também o cliente Anthropic. Os testes offline não podem
depender disso; os testes LIVE (`LIVE=1`) usam o ambiente real de propósito e ficam de fora.
"""

import os

import pytest

_LEAKY_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_API_URL", "GROVE_API_KEY",
              "GROVE_ANTHROPIC_BASE_URL", "GROVE_CHAT_COMPLETIONS_URL", "GROVE_OPENAI_MODELS")


@pytest.fixture(autouse=True)
def _offline_env_isolation(monkeypatch):
    if os.getenv("LIVE") != "1":
        for name in _LEAKY_ENV:
            monkeypatch.delenv(name, raising=False)
    yield
