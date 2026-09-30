"""Reset da memória DA DEMO de um cliente: deixa o usuário como estava antes de a demo escrever nele.

A demo grava de verdade (fatos extraídos, episódios, curto prazo, cache do cliente). Para repetir o roteiro sem
que a segunda rodada pareça "não acontecer nada" (dedup: o fato já existe), este reset desfaz só o que a demo criou
e reativa o que ela substituiu — o teto legado, por exemplo. Escopo estrito: um único customer_key (o do JWT).
Não toca no cache GLOBAL (aquecido, compartilhado), no legado migrado nem em nenhum outro cliente.
"""

from .database import DataStore, utcnow


async def reset_customer_memory(store: DataStore, customer_key: str) -> dict:
    created = await store.find_many("customer_memory", {"customer_key": customer_key, "source": "extractor"}, limit=1000)
    created_ids = {doc["_id"] for doc in created}
    restored = 0
    # o que esses fatos substituíram volta a valer (só documentos deste cliente)
    for doc in await store.find_many("customer_memory", {"customer_key": customer_key, "active": False}, limit=1000):
        if doc.get("superseded_by") in created_ids:
            await store.update_one("customer_memory", {"_id": doc["_id"], "customer_key": customer_key},
                                   {"$set": {"active": True, "superseded_by": None, "updated_at": utcnow()}})
            restored += 1
    removed = await store.delete_many("customer_memory", {"customer_key": customer_key, "source": "extractor"})
    return {
        "facts_removed": removed,
        "facts_restored": restored,
        "short_term_removed": await store.delete_many("short_term_memory", {"customer_key": customer_key}),
        "episodes_removed": await store.delete_many("long_term_memory", {"customer_key": customer_key, "kind": "episode"}),
        "customer_cache_removed": await store.delete_many("semantic_cache", {"customer_key": customer_key, "scope": "customer"}),
    }
