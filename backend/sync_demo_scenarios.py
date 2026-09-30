"""Sincroniza SÓ os roteiros de demo (brain.demo_scenarios) com app/seed_data.py — idempotente (upsert por scenario_id).

Diferente do seed.py, não devolve nenhum outro dado ao estado inicial. Rode depois de mudar DEMO_SCENARIOS.

    python sync_demo_scenarios.py
"""

import asyncio
import sys

from app.config import get_settings
from app.database import DataStore
from app.seed_data import DEMO_SCENARIOS


async def sync(store: DataStore) -> int:
    for scenario in DEMO_SCENARIOS:
        await store.replace_one("demo_scenarios", {"scenario_id": scenario["scenario_id"]}, scenario, brain=True, upsert=True)
    return len(DEMO_SCENARIOS)


async def main() -> None:
    store = DataStore(get_settings())
    await store.connect()
    try:
        if store.memory:
            sys.exit("DEMO_MODE/sem MONGODB_URI: o seed em memória já carrega os roteiros.")
        print(f"✓ demo_scenarios: {await sync(store)} roteiros sincronizados")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
