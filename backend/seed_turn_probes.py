"""Semeia SÓ o classificador de turno pessoal (probes + índice vetorial) — idempotente.

NÃO roda no seed.py de propósito: cria coleção e índice de busca no cluster (mudança de schema),
então é um passo explícito. Não toca em nenhum outro dado.

    python seed_turn_probes.py        # semeia os probes e cria o índice `turn_probes_vs`
    python calibrate_thresholds.py --only turn --apply   # mede e grava o limiar (depois do índice READY)

Sem o limiar medido o classificador falha fechado: nenhum turno é servido do cache semântico.
"""

import asyncio
import sys

from pymongo.operations import SearchIndexModel

from app import turn_classifier as tc
from app.config import get_settings
from app.database import DataStore, utcnow


async def seed_probes(store: DataStore) -> int:
    """Upsert dos probes semeados em <brain_db>.turn_probes. Devolve quantos são novos."""
    new = 0
    for phrase in tc.PERSONAL_PROBES:
        if await store.find_one(tc.PROBES_COLLECTION, {"phrase": phrase}, brain=True):
            continue
        await store.insert_one(tc.PROBES_COLLECTION, {"phrase": phrase, "label": "personal", "created_at": utcnow()}, brain=True)
        new += 1
    return new


async def create_index(store: DataStore) -> str:
    if store.memory:
        return "índice vetorial: ignorado em DEMO_MODE"
    model = SearchIndexModel(
        name=tc.PROBES_INDEX, type="vectorSearch",
        definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": tc.PROBES_PATH, "model": "voyage-4",
                                "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}]},
    )
    collection = store._collection(tc.PROBES_COLLECTION, brain=True)
    cursor = await collection.list_search_indexes()
    if tc.PROBES_INDEX in {item["name"] for item in await cursor.to_list(None)}:
        return f"índice '{tc.PROBES_INDEX}' já existe"
    await collection.create_search_index(model)
    return f"índice '{tc.PROBES_INDEX}' criado (aguarde ficar READY)"


async def main() -> None:
    store = DataStore(get_settings())
    await store.connect()
    try:
        if store.memory:
            sys.exit("DEMO_MODE/sem MONGODB_URI: nada a semear (o fallback usa os probes do código).")
        print(f"✓ {tc.PROBES_COLLECTION}: {await seed_probes(store)} probes novos de {len(tc.PERSONAL_PROBES)}")
        print(f"✓ {await create_index(store)}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
