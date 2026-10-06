"""Reset COMPLETO da demo, num comando só, idempotente.

    ALLOW_DEMO_DB_WRITE=1 python backend/scripts/reset_demo.py            # demo (multi_agent_poc/multiagent_brain)
    MONGODB_DB=multi_agent_poc_test MONGODB_BRAIN_DB=multiagent_brain_test python backend/scripts/reset_demo.py

O que faz, nesta ordem:
  1. `seed.py`: dados de negócio, registry, regras, cenários, políticas (preservando os cortes
     medidos), índices B-tree/únicos/TTL, validadores e índices Search/Vector (autoEmbed voyage-4:
     o embedding é gerado pelo próprio Atlas, não há vetor a recalcular aqui). Também invalida
     cache semântico, curto prazo, decisões, auditoria e casos pausados.
  2. Probes dos classificadores por embedding (`scope_probes`, `turn_probes`) e seus índices.
  3. Memória que a demo escreveu em cada identidade (mesmo reset do botão da UI).
  4. Escritas operacionais acumuladas pelos ensaios: chamados de suporte, resgates, conversas,
     candidatos/eventos de guardrail e frases aprendidas pelo classificador (o beat
     "o classificador ensina o denylist" volta a acontecer na demo).
  5. Checkpoints do LangGraph órfãos (thread sem conversa viva).
  6. Espera os índices Search/Vector ficarem READY.

Não toca: a configuração medida (`*_classifier_config`, cortes de `guardrail_policies`), os traces
de observabilidade (`agent_traces`, `agent_handoffs`, TTL 30 dias), `eval_runs` nem nenhum outro banco do cluster.
Recusa o banco da demo sem `ALLOW_DEMO_DB_WRITE=1`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.database import DataStore  # noqa: E402
from app.demo_reset import reset_customer_memory  # noqa: E402

# Coleções que só a demo escreve (nenhuma vem do seed): limpas por inteiro.
DEMO_WRITES = ("support_tickets", "redemptions", "agent_conversations",
               "guardrail_candidates", "guardrail_events")


async def _orphan_checkpoints(store: DataStore) -> int:
    if store.memory:
        return 0
    db = store.client[store.settings.mongodb_db]
    live = {f"{c['customer_key']}:{c['conversation_id']}"
            for c in await db.agent_conversations.find({}, {"customer_key": 1, "conversation_id": 1}).to_list(None)}
    threads = [t for t in await db.langgraph_checkpoints.distinct("thread_id") if t not in live]
    removed = 0
    for start in range(0, len(threads), 500):
        chunk = threads[start:start + 500]
        removed += (await db.langgraph_checkpoints.delete_many({"thread_id": {"$in": chunk}})).deleted_count
        await db.langgraph_checkpoint_writes.delete_many({"thread_id": {"$in": chunk}})
    return removed


async def reset(store: DataStore, *, wait_indexes: bool = True) -> list[str]:
    import seed_scope_probes
    import seed_turn_probes
    from seed import seed

    lines = [f"seed: {line}" for line in await seed(store, create_indexes=True)]
    if not store.memory:
        for module in (seed_scope_probes, seed_turn_probes):
            lines.append(f"probes {module.__name__}: +{await module.seed_probes(store)} novos; {await module.create_index(store)}")
    for customer in await store.find_many("customers", {}, limit=100):
        summary = await reset_customer_memory(store, customer["customer_key"])
        lines.append(f"memória da demo de {customer['customer_key']}: {summary}")
    for name in DEMO_WRITES:
        lines.append(f"{name}: {await store.delete_many(name, {})} removidos")
    learned = await store.delete_many("guardrail_denylist", {"source": "semantic_llm"})
    lines.append(f"guardrail_denylist: {learned} frases aprendidas removidas (as do seed ficam)")
    lines.append(f"langgraph_checkpoints: {await _orphan_checkpoints(store)} órfãos removidos")
    if wait_indexes and not store.memory:
        from scripts.isolation import _indexes_ready
        lines.append(f"índices Search/Vector: {await _indexes_ready(store, timeout_s=600)}")
    return lines


async def main() -> None:
    from seed import refuse_demo_database

    settings = get_settings()
    refuse_demo_database(settings)
    store = DataStore(settings)
    await store.connect()
    print(f"[reset] banco={settings.mongodb_db} cérebro={settings.mongodb_brain_db}")
    try:
        for line in await reset(store):
            print(f"[reset] {line}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
