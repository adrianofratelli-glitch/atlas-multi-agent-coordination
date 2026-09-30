"""Seed idempotente do plano de dados e do plano de coordenação."""

import asyncio
import sys
from pathlib import Path

from pymongo.operations import SearchIndexModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.database import DataStore  # noqa: E402
from app.seed_data import (  # noqa: E402
    AGENTS,
    DEMO_SCENARIOS,
    GUARDRAIL_POLICIES,
    ROUTING_RULES,
    seed_documents,
)


KEYS = {
    "customers": "customer_key",
    "orders": "order_id",
    "invoices": "invoice_id",
    "products_catalog": "sku",
    "kb_articles": "article_id",
    "warranty_policies": "category",
    "loyalty_accounts": "customer_key",
    "shipments": "order_id",
    "guardrail_denylist": "phrase_norm",
    "semantic_cache": "question_norm",
}


async def seed(store: DataStore, *, create_indexes: bool = True) -> list[str]:
    messages: list[str] = []
    # O seed redefine o mundo (status de pedido, fatura, saldo de pontos). Toda resposta
    # em cache foi derivada do mundo ANTERIOR, então precisa morrer junto: sem isso o
    # turno seguinte serve um HIT dizendo "seu pedido está em troca_solicitada" enquanto
    # a collection já voltou para "processando" — a demo se contradiz na tela, e o pior é
    # que ela se contradiz com confiança. Descoberto quebrando a PoV de propósito.
    for cache_collection in ("semantic_cache", "short_term_memory"):
        try:
            removed = await store.delete_many(cache_collection, {})
            messages.append(f"{cache_collection}: {removed} entradas invalidadas (dados de negócio foram redefinidos)")
        except Exception as exc:  # noqa: BLE001 — cache vazio/ausente não impede o seed
            messages.append(f"{cache_collection}: limpeza best-effort ({exc})")
    # Mesmo argumento, um nível acima: o seed devolve os pedidos ao estado inicial, então
    # as decisões e os casos pausados que falam SOBRE esses pedidos também precisam morrer.
    # Sem isso a trilha de conformidade mostra "reembolso aprovado para PED-3001" ao lado de
    # um PED-3001 que acabou de voltar a "troca_solicitada", e a fila do analista abre a demo
    # já cheia de casos do ensaio anterior.
    #
    # Isto NÃO contradiz a imutabilidade de agent_decisions: imutável significa que o sistema
    # em execução nunca reescreve uma decisão. Redefinir o mundo inteiro é outra operação —
    # deliberada, fora do fluxo, e que em produção não existiria.
    for reset_collection in ("agent_decisions", "agent_audit_events", "pending_reviews"):
        try:
            removed = await store.delete_many(reset_collection, {})
            messages.append(f"{reset_collection}: {removed} registros do ensaio anterior removidos")
        except Exception as exc:  # noqa: BLE001 — collection ausente não impede o seed
            messages.append(f"{reset_collection}: limpeza best-effort ({exc})")
    for collection, documents in seed_documents().items():
        key = KEYS[collection]
        for document in documents:
            await store.replace_one(collection, {key: document[key]}, document, upsert=True)
        messages.append(f"{collection}: {len(documents)} documentos")

    for agent in AGENTS:
        await store.replace_one(
            "agent_registry", {"agent_key": agent["agent_key"]}, agent, brain=True, upsert=True
        )
    messages.append(f"agent_registry: {len(AGENTS)} agentes, todos reais")
    for rule in ROUTING_RULES:
        await store.replace_one(
            "routing_rules", {"intent": rule["intent"]}, rule, brain=True, upsert=True
        )
    for scenario in DEMO_SCENARIOS:
        await store.replace_one(
            "demo_scenarios", {"scenario_id": scenario["scenario_id"]}, scenario, brain=True, upsert=True
        )
    for policy in GUARDRAIL_POLICIES:
        await store.replace_one(
            "guardrail_policies", {"area": policy["area"]}, policy, brain=True, upsert=True
        )
    await store.replace_one(
        "model_config",
        {"key": "default"},
        {"key": "default", "default_model": "claude-haiku-4-5", "global_turn_tokens": 26000},
        brain=True,
        upsert=True,
    )
    messages.append(f"ai_brain: registry, routing, policies, model_config e {len(DEMO_SCENARIOS)} cenários")

    if create_indexes:
        try:
            await store.create_standard_indexes()
            messages.append("índices B-tree, únicos e TTL: prontos")
        except Exception as exc:  # índice existente com outra definição ou permissão limitada
            messages.append(f"índices padrão: aviso ({exc})")
        messages.extend(await store.create_schema_validators())
        messages.extend(await create_search_indexes(store))
    return messages


# `price` é campo `filter`: é o que permite ao servidor injetar o teto de orçamento do cliente como
# pré-filtro NATIVO do $vectorSearch (agents.py:build_product_pipeline). Há um teste que impede o
# pipeline de filtrar por um campo que o índice não declara.
PRODUCTS_VECTOR_INDEX_DEFINITION = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": "search_text", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"},
        {"type": "filter", "path": "category"},
        {"type": "filter", "path": "active"},
        {"type": "filter", "path": "price"},
    ]
}


async def create_search_indexes(store: DataStore) -> list[str]:
    if store.memory:
        return ["índices Search/Vector: ignorados em DEMO_MODE"]
    definitions = [
        (
            "products_catalog",
            SearchIndexModel(
                name="products_autoembed_v1",
                type="vectorSearch",
                definition=PRODUCTS_VECTOR_INDEX_DEFINITION,
            ),
        ),
        (
            "kb_articles",
            SearchIndexModel(
                name="kb_autoembed_v1",
                type="vectorSearch",
                definition={
                    "fields": [
                        {"type": "autoEmbed", "modality": "text", "path": "content", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"},
                        {"type": "filter", "path": "category"},
                    ]
                },
            ),
        ),
        (
            "kb_articles",
            SearchIndexModel(
                name="kb_lexical_v1",
                definition={"mappings": {"dynamic": False, "fields": {"title": {"type": "string", "analyzer": "lucene.portuguese"}, "content": {"type": "string", "analyzer": "lucene.portuguese"}, "category": {"type": "token"}}}},
            ),
        ),
        (
            "customer_memory",
            SearchIndexModel(
                name="memory_autoembed_v1",
                type="vectorSearch",
                definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": "fact", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}, {"type": "filter", "path": "customer_key"}, {"type": "filter", "path": "active"}]},
            ),
        ),
        (
            "semantic_cache",
            SearchIndexModel(
                name="cache_autoembed_v1",
                type="vectorSearch",
                definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": "question_text", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}, {"type": "filter", "path": "agent"}, {"type": "filter", "path": "area"}, {"type": "filter", "path": "customer_key"}, {"type": "filter", "path": "scope"}]},
            ),
        ),
        (
            "short_term_memory",
            SearchIndexModel(
                name="short_term_autoembed_v1",
                type="vectorSearch",
                definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": "question_text", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}, {"type": "filter", "path": "session_id"}, {"type": "filter", "path": "customer_key"}, {"type": "filter", "path": "agent"}]},
            ),
        ),
        (
            # Denylist semântico: a lista lexical só pega substring exata; este índice é o que
            # bloqueia uma paráfrase sem depender do classificador LLM (que é pulado quando a
            # mensagem já bateu numa regra de roteamento, e não existe em DEMO_MODE).
            "guardrail_denylist",
            SearchIndexModel(
                name="denylist_autoembed_v1",
                type="vectorSearch",
                # `layer` é filtro porque as duas camadas NÃO podem se misturar: as entradas
                # lexicais são fragmentos curtos ("sem nota fiscal") que, como vetor, ficam
                # colados em pedido legítimo ("pode me enviar a nota fiscal da minha compra?")
                # — medido em 0.8263, acima de ataque real. Elas servem para substring; a busca
                # vetorial só percorre as frases escritas como intenção completa.
                definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": "phrase", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}, {"type": "filter", "path": "area"}, {"type": "filter", "path": "active"}, {"type": "filter", "path": "layer"}]},
            ),
        ),
        (
            "long_term_memory",
            SearchIndexModel(
                name="long_term_autoembed_v1",
                type="vectorSearch",
                definition={"fields": [{"type": "autoEmbed", "modality": "text", "path": "text", "model": "voyage-4", "numDimensions": 1024, "similarity": "cosine", "indexingMethod": "flat"}, {"type": "filter", "path": "customer_key"}]},
            ),
        ),
    ]
    messages: list[str] = []
    for collection, model in definitions:
        try:
            db = store.client[store.settings.mongodb_db]
            try:
                await db.create_collection(collection)
            except Exception:
                pass  # já existe
            cursor = await store._collection(collection).list_search_indexes()
            existing = {item["name"] for item in await cursor.to_list(None)}
            if model.document["name"] not in existing:
                await store._collection(collection).create_search_index(model)
            else:
                await store._collection(collection).update_search_index(
                    model.document["name"],
                    model.document["definition"],
                )
            messages.append(f"{collection}.{model.document['name']}: solicitado")
        except Exception as exc:
            messages.append(f"{collection}.{model.document['name']}: best-effort ({exc})")
    return messages


async def main() -> None:
    store = DataStore(get_settings())
    await store.connect()
    try:
        for message in await seed(store):
            print(f"[seed] {message}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
