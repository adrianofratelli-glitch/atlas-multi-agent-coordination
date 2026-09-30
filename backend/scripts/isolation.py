"""Isolamento de banco para os scripts que escrevem dado REAL no Atlas.

Regra: `crash_resume.py` e `eval_routing.py --live` nunca escrevem no banco da demo. Eles usam
um par de bancos de teste no MESMO cluster, derivado do nome configurado:

    MONGODB_DB=multi_agent_poc         ->  MONGODB_TEST_DB=multi_agent_poc_test
    MONGODB_BRAIN_DB=multiagent_brain  ->  MONGODB_TEST_BRAIN_DB=multiagent_brain_test

Os dois nomes podem ser sobrescritos por env. O destino é conferido no startup: se apontar
para o banco da demo, o script RECUSA rodar, a menos que `ALLOW_DEMO_DB_WRITE=1` seja passado
explicitamente (escape hatch consciente, nunca default).

O banco de teste nasce vazio: `ensure_seeded` roda o seed com os índices Search/Vector reais e
espera ficarem READY (primeira execução leva minutos; as seguintes são instantâneas). Ele também
copia do cérebro da DEMO, em leitura, o que vive só no cluster e não está no git: a configuração
medida (`guardrail_policies`, `turn_classifier_config`, `scope_classifier_config`) E os probes
dos classificadores por embedding (`turn_probes`, `scope_probes`), criando os índices vetoriais
`turn_probes_vs`/`scope_probes_vs` no banco de teste. Sem isso o eval isolado mediria só o
fallback por palavra-chave, e não o mesmo caminho de embedding que a demo usa.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402
from app.database import DataStore  # noqa: E402

# Documentos de configuração MEDIDOS que vivem só no cluster. São copiados do
# cérebro da demo para o de teste em modo leitura, para o teste medir o mesmo comportamento.
BRAIN_CONFIG_COLLECTIONS = ("guardrail_policies", "turn_classifier_config", "scope_classifier_config")
# Probes dos classificadores por embedding: copiados COM o índice vetorial, senão
# `classify()` não tem vizinho para medir e o turno cai no fallback por palavra.
BRAIN_PROBE_COLLECTIONS = ("turn_probes", "scope_probes")
INDEXED_COLLECTIONS = ("products_catalog", "kb_articles", "customer_memory", "semantic_cache",
                       "short_term_memory", "guardrail_denylist", "long_term_memory")


class DemoDatabaseRefused(SystemExit):
    """Recusa explícita: o destino é o banco da demo."""


def allow_demo_write() -> bool:
    return os.getenv("ALLOW_DEMO_DB_WRITE", "").strip().lower() in {"1", "true", "yes", "on"}


def test_database_names(settings: Settings | None = None) -> tuple[str, str]:
    base = settings or get_settings()
    return (os.getenv("MONGODB_TEST_DB") or f"{base.mongodb_db}_test",
            os.getenv("MONGODB_TEST_BRAIN_DB") or f"{base.mongodb_brain_db}_test")


def test_settings(**overrides) -> Settings:
    """Settings do PoV apontando para os bancos de teste (mesmo cluster, outro banco)."""
    base = get_settings()
    main_db, brain_db = test_database_names(base)
    return base.model_copy(update={"mongodb_db": main_db, "mongodb_brain_db": brain_db, **overrides})


def guard(settings: Settings, *, what: str, hint: str = "") -> Settings:
    """Recusa rodar contra o banco da demo. Retorna as settings quando o destino é seguro."""
    demo = get_settings()
    hits = [name for name, value in (("MONGODB_DB", settings.mongodb_db),
                                     ("MONGODB_BRAIN_DB", settings.mongodb_brain_db))
            if value in (demo.mongodb_db, demo.mongodb_brain_db)]
    if not hits:
        return settings
    if allow_demo_write():
        print(f"[isolamento] AVISO: {what} vai escrever no banco da DEMO "
              f"({settings.mongodb_db}/{settings.mongodb_brain_db}) — ALLOW_DEMO_DB_WRITE=1 foi passado.")
        return settings
    # Nomes SUGERIDOS são sempre o default derivado — não o override de env que causou a recusa.
    main_db, brain_db = f"{demo.mongodb_db}_test", f"{demo.mongodb_brain_db}_test"
    raise DemoDatabaseRefused(
        f"\n[isolamento] RECUSADO: {what} escreveria no banco da demo ({', '.join(hits)} = "
        f"{settings.mongodb_db}/{settings.mongodb_brain_db}).\n"
        + (f"             {hint}\n" if hint
           else f"             Use os bancos de teste ({main_db}/{brain_db}) — é o padrão destes scripts —\n")
        + "             ou passe ALLOW_DEMO_DB_WRITE=1 se for MESMO para escrever na demo.\n")


async def _probe_indexes(store: DataStore) -> list[str]:
    """Cria `turn_probes_vs` e `scope_probes_vs` no cérebro de TESTE (idempotente)."""
    import seed_scope_probes
    import seed_turn_probes

    messages = []
    for module in (seed_turn_probes, seed_scope_probes):
        messages.append(f"índice de probes: {await module.create_index(store)}")
    return messages


async def _indexes_ready(store: DataStore, timeout_s: float = 900.0) -> str:
    """Espera os índices Search/Vector do banco de teste ficarem READY (inclui os do cérebro)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    pending = [(name, False) for name in INDEXED_COLLECTIONS] + [(name, True) for name in BRAIN_PROBE_COLLECTIONS]
    while pending and asyncio.get_event_loop().time() < deadline:
        still: list[tuple[str, bool]] = []
        for name, brain in pending:
            try:
                cursor = await store._collection(name, brain).list_search_indexes()
                rows = await cursor.to_list(None)
            except Exception:  # noqa: BLE001 — collection ainda não existe
                rows = []
            if not rows or any(row.get("status") != "READY" for row in rows):
                still.append((name, brain))
        if not still:
            return "todos READY"
        pending = still
        await asyncio.sleep(10)
    return f"ainda construindo: {', '.join(name for name, _ in pending)}" if pending else "todos READY"


async def ensure_seeded(store: DataStore, *, wait_indexes: bool = True) -> list[str]:
    """Semeia o banco de teste (idempotente) e copia a configuração medida do cérebro da demo."""
    from seed import seed

    messages: list[str] = []
    customers = await store.count("customers")
    if customers == 0:
        messages += [f"seed: {line}" for line in await seed(store, create_indexes=True)]
    else:
        messages.append(f"seed: já havia {customers} clientes no banco de teste")

    demo = get_settings()
    if store.client is not None and demo.mongodb_brain_db != store.settings.mongodb_brain_db:
        source = store.client[demo.mongodb_brain_db]
        target = store.client[store.settings.mongodb_brain_db]
        for name in BRAIN_CONFIG_COLLECTIONS:
            documents = await source[name].find({}).to_list(length=None)   # leitura, nunca escrita na demo
            if not documents:
                continue
            await target[name].delete_many({})
            await target[name].insert_many(documents)
            messages.append(f"config medida copiada: {name} ({len(documents)} doc)")
        for name in BRAIN_PROBE_COLLECTIONS:
            # Probes são imutáveis na prática (frases medidas); copia só o que falta, para não
            # recriar documento e forçar o índice autoEmbed a reindexar tudo a cada execução.
            existing = {doc.get("phrase") for doc in await target[name].find({}, {"phrase": 1}).to_list(length=None)}
            documents = [doc for doc in await source[name].find({}).to_list(length=None)
                         if doc.get("phrase") not in existing]
            if documents:
                await target[name].insert_many(documents)
            messages.append(f"probes copiados: {name} (+{len(documents)}, total {len(existing) + len(documents)})")
        messages += await _probe_indexes(store)

    if wait_indexes:
        messages.append(f"índices Search/Vector: {await _indexes_ready(store)}")
    return messages


async def open_test_store(*, what: str, seed_if_empty: bool = True, wait_indexes: bool = True):
    """Abre um DataStore já isolado e semeado. Único caminho usado pelos scripts que escrevem."""
    settings = guard(test_settings(), what=what)
    store = DataStore(settings)
    await store.connect()
    print(f"[isolamento] {what}: banco={settings.mongodb_db} cérebro={settings.mongodb_brain_db}")
    if seed_if_empty:
        for line in await ensure_seeded(store, wait_indexes=wait_indexes):
            print(f"[isolamento] {line}")
    return store, settings


if __name__ == "__main__":
    async def main() -> None:
        store, _ = await open_test_store(what="provisionamento do banco de teste")
        await store.close()

    asyncio.run(main())
