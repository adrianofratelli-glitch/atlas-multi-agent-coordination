"""Semeia SÓ o registro de ferramentas externas + índices da busca web — idempotente (ADR-005).

Não toca em nenhum outro dado da PoV (diferente de seed.py, que devolve a demo ao estado inicial) e NÃO zera
a cota do mês (`<brain>.web_search_quota`). Também é chamado pelo reset completo (`scripts/reset_demo.py`) e
pelo provisionamento do banco de teste (`scripts/isolation.py`), para o liga/desliga ao vivo voltar ao padrão.

    python backend/seed_tool_registry.py                       # banco de teste (MONGODB_DB=*_test ...)
    ALLOW_DEMO_DB_WRITE=1 python backend/seed_tool_registry.py # banco da demo, de propósito
"""

import asyncio

from app import web_search
from app.config import get_settings
from app.database import DataStore, utcnow

# Financeiro fica SEM busca web de propósito: mostra o controle por área num `update_one`, sem deploy.
WEB_SEARCH_TOOL = {
    "_id": web_search.TOOL_NAME,
    "kind": "external",
    "provider": web_search.PROVIDER,
    "enabled": True,
    "disabled_areas": ["financeiro"],
    "monthly_limit": web_search.DEFAULTS["monthly_limit"],
    "max_results": web_search.DEFAULTS["max_results"],
    "cache_ttl_seconds": web_search.DEFAULTS["cache_ttl_seconds"],
    "search_depth": web_search.DEFAULTS["search_depth"],
    "description": ("Busca web (Tavily): situação pública da transportadora quando a entrega está com a previsão "
                    "vencida. Consulta montada pelo servidor; o turno não escreve depois de usá-la."),
}


async def seed_tool_registry(store: DataStore) -> str:
    await store.replace_one(web_search.REGISTRY_COLLECTION, {"_id": WEB_SEARCH_TOOL["_id"]},
                            {**WEB_SEARCH_TOOL, "updated_at": utcnow()}, brain=True, upsert=True)
    # só os TTL da busca web (idempotente); o resto dos índices é do seed completo
    await store.create_standard_indexes(only={web_search.CACHE_COLLECTION, web_search.AUDIT_COLLECTION})
    return (f"tool_registry: {WEB_SEARCH_TOOL['_id']} ligada (desligada em: "
            f"{', '.join(WEB_SEARCH_TOOL['disabled_areas'])}; cota {WEB_SEARCH_TOOL['monthly_limit']}/mês)")


async def main() -> None:
    from seed import refuse_demo_database

    settings = get_settings()
    refuse_demo_database(settings)
    store = DataStore(settings)
    await store.connect()
    try:
        print(f"[tool_registry] cérebro={settings.mongodb_brain_db}: {await seed_tool_registry(store)}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
