"""Migra documentos legados de `customer_memory` (fact_type/value) para o formato do extrator (fact/...).

Aditivo e idempotente: só ACRESCENTA campos (`fact`, `fact_norm`, `category`, `superseded_by`,
`migrated_from`) e nunca remove `fact_type`/`value`, então dá para reverter. `price_sensitive` NÃO
ganha `max_price_brl`: uma versão anterior aplicava o teto fixo de R$ 350 que o código antigo usava, mas isso
bloqueava o cache de produto e impunha um limite que o cliente nunca declarou. Orçamento agora só existe
quando o cliente diz um número (extrator) — `clear_legacy_price_cap` desfaz a versão anterior.

    python migrate_legacy_memory.py           # dry-run: só conta
    python migrate_legacy_memory.py --apply   # grava
"""

import asyncio
import sys

from app.config import get_settings
from app.database import DataStore, utcnow
from app.memory import _fact_norm

CATEGORY_BY_TYPE = {"price_sensitive": "preferencia", "product_complaint": "historico"}


async def migrate(store: DataStore, *, apply: bool) -> int:
    """Devolve quantos documentos legados foram (ou seriam) migrados."""
    legacy = [d for d in await store.find_many("customer_memory", {"value": {"$exists": True}}, limit=10_000)
              if not d.get("fact")]
    if not apply:
        return len(legacy)
    for doc in legacy:
        update = {"fact": doc["value"], "fact_norm": _fact_norm(doc["value"]),
                  "category": CATEGORY_BY_TYPE.get(doc.get("fact_type"), "contexto"),
                  "superseded_by": None, "migrated_from": doc.get("fact_type"), "updated_at": utcnow()}
        await store.update_one("customer_memory", {"_id": doc["_id"]}, {"$set": update})
    return len(legacy)


async def clear_legacy_price_cap(store: DataStore) -> int:
    """Remove o teto de R$ 350 que a migração anterior pôs nos fatos price_sensitive (o fato em si permanece)."""
    docs = await store.find_many("customer_memory", {"migrated_from": "price_sensitive", "max_price_brl": {"$gt": 0}}, limit=10_000)
    for doc in docs:
        await store.update_one("customer_memory", {"_id": doc["_id"]}, {"$unset": {"max_price_brl": ""}, "$set": {"updated_at": utcnow()}})
    return len(docs)


async def main() -> None:
    apply = "--apply" in sys.argv
    store = DataStore(get_settings())
    await store.connect()
    try:
        if store.memory:
            sys.exit("DEMO_MODE/sem MONGODB_URI: nada a migrar.")
        if apply:
            print(f"teto legado removido de {await clear_legacy_price_cap(store)} documento(s)")
        count = await migrate(store, apply=apply)
        print(f"{'migrados' if apply else 'seriam migrados (dry-run)'}: {count} documento(s)")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
