"""A guarda de banco é o que impede um script de teste de escrever na demo — então ela mesma
precisa de teste. Tudo offline: `guard` só compara nomes, não abre conexão."""

import pytest

from app.config import Settings
from scripts import isolation


@pytest.fixture
def demo(monkeypatch):
    settings = Settings(demo_mode=True, mongodb_uri="", mongodb_db="loja", mongodb_brain_db="cerebro")
    monkeypatch.setattr(isolation, "get_settings", lambda: settings)
    for name in ("MONGODB_TEST_DB", "MONGODB_TEST_BRAIN_DB", "ALLOW_DEMO_DB_WRITE"):
        monkeypatch.delenv(name, raising=False)
    return settings


def test_default_target_is_the_test_database(demo):
    assert isolation.test_database_names(demo) == ("loja_test", "cerebro_test")


def test_guard_accepts_the_isolated_database(demo):
    target = demo.model_copy(update={"mongodb_db": "loja_test", "mongodb_brain_db": "cerebro_test"})
    assert isolation.guard(target, what="teste").mongodb_db == "loja_test"


def test_guard_refuses_the_demo_database(demo):
    with pytest.raises(SystemExit) as excinfo:
        isolation.guard(demo, what="teste")
    assert "RECUSADO" in str(excinfo.value) and "loja_test" in str(excinfo.value)


def test_guard_refuses_when_only_the_brain_points_at_the_demo(demo):
    target = demo.model_copy(update={"mongodb_db": "loja_test"})   # cérebro continua o da demo
    with pytest.raises(SystemExit):
        isolation.guard(target, what="teste")


def test_explicit_escape_hatch_allows_the_demo_database(demo, monkeypatch):
    monkeypatch.setenv("ALLOW_DEMO_DB_WRITE", "1")
    assert isolation.guard(demo, what="teste").mongodb_db == "loja"


def test_env_override_wins_over_the_suffix(demo, monkeypatch):
    monkeypatch.setenv("MONGODB_TEST_DB", "outro_banco")
    assert isolation.test_database_names(demo) == ("outro_banco", "cerebro_test")
