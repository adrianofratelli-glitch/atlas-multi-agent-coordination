"""Semeia SÓ o classificador de escopo (probes rotulados in/out/chat + índice vetorial) — idempotente.

NÃO roda no seed.py de propósito: cria coleção e índice de busca no cluster (mudança de schema), então é um passo
explícito. Não toca em nenhum outro dado.

    python seed_scope_probes.py                       # semeia os probes e cria o índice `scope_probes_vs`
    python calibrate_thresholds.py --only scope --apply   # mede as margens (depois do índice READY)

Sem margens medidas o classificador se abstém e o roteamento segue pela lista de palavras (comportamento anterior).
"""

import asyncio
import sys

from pymongo.operations import SearchIndexModel

from app import scope_classifier as sc
from app.config import get_settings
from app.database import DataStore, utcnow


async def seed_probes(store: DataStore) -> int:
    """Upsert dos probes em <brain_db>.scope_probes. Devolve quantos são novos."""
    new = 0
    for label, phrases in (("in", sc.IN_SCOPE_PROBES), ("out", sc.OUT_OF_SCOPE_PROBES), ("chat", sc.CHAT_PROBES)):
        for phrase in phrases:
            if await store.find_one(sc.PROBES_COLLECTION, {"phrase": phrase}, brain=True):
                continue
            await store.insert_one(sc.PROBES_COLLECTION, {"phrase": phrase, "label": label, "created_at": utcnow()}, brain=True)
            new += 1
    return new


async def create_index(store: DataStore) -> str:
    if store.memory:
        return "índice vetorial: ignorado em DEMO_MODE"
    model = SearchIndexModel(
        name=sc.PROBES_INDEX, type="vectorSearch",
        definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": sc.PROBES_PATH, "model": "voyage-4",
                                "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}]},
    )
    collection = store._collection(sc.PROBES_COLLECTION, brain=True)
    cursor = await collection.list_search_indexes()
    if sc.PROBES_INDEX in {item["name"] for item in await cursor.to_list(None)}:
        return f"índice '{sc.PROBES_INDEX}' já existe"
    await collection.create_search_index(model)
    return f"índice '{sc.PROBES_INDEX}' criado (aguarde ficar READY)"


async def main() -> None:
    store = DataStore(get_settings())
    await store.connect()
    try:
        if store.memory:
            sys.exit("DEMO_MODE/sem MONGODB_URI: nada a semear (o fallback mantém a lista de palavras).")
        total = len(sc.IN_SCOPE_PROBES) + len(sc.OUT_OF_SCOPE_PROBES) + len(sc.CHAT_PROBES)
        print(f"✓ {sc.PROBES_COLLECTION}: {await seed_probes(store)} probes novos de {total}")
        print(f"✓ {await create_index(store)}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
