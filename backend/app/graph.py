"""Travessia de grafo sobre `orders`: cadeia de trocas de um pedido.

Um pedido trocado gera um pedido de reposição, que pode ser trocado de novo. Cada elo é um
documento comum de `orders` — o relacionamento é o campo `replacement_order_id`. A pergunta
"quantas vezes este pedido já foi trocado, e sempre pelo mesmo produto?" não se responde
olhando um documento por vez: é preciso seguir a cadeia até o fim, sem saber de antemão
quantos saltos ela tem. `$graphLookup` faz esse loop dentro do servidor, em uma agregação.

O sinal de negócio: reposição repetida do MESMO produto não é azar do cliente, é defeito de
lote — trocar pela quarta vez só reproduz o problema. Aí o caso vai para revisão humana de
qualidade em vez de virar mais uma troca automática.
"""

RECURRENCE_THRESHOLD = 3


def build_order_chain_pipeline(order_id: str, customer_key: str, *, max_depth: int = 6) -> list[dict]:
    """Segue replacement_order_id -> order_id a partir de um pedido, até `max_depth` saltos.

    O filtro de dono entra no `$match` inicial: a travessia parte apenas de um pedido que
    pertence a quem perguntou. `restrictSearchWithMatch` repete o filtro em cada salto, para
    que nem um dado de outro cliente seja alcançado por um campo mal preenchido.
    """
    return [
        {"$match": {"order_id": order_id, "owner_customer_key": customer_key}},
        {"$graphLookup": {
            "from": "orders",
            "startWith": "$replacement_order_id",
            "connectFromField": "replacement_order_id",
            "connectToField": "order_id",
            "as": "chain",
            "maxDepth": max_depth,
            "depthField": "depth",
            "restrictSearchWithMatch": {"owner_customer_key": customer_key},
        }},
        {"$project": {
            "_id": 0, "order_id": 1, "product": 1, "status": 1, "replacement_order_id": 1,
            "chain": {"$map": {"input": {"$sortArray": {"input": "$chain", "sortBy": {"depth": 1}}},
                               "as": "link",
                               "in": {"order_id": "$$link.order_id", "product": "$$link.product",
                                      "status": "$$link.status", "depth": "$$link.depth",
                                      "reason": "$$link.replacement_reason"}}},
        }},
    ]


def summarize_order_chain(document: dict | None) -> dict:
    """Traduz a cadeia crua em sinais de negócio. Sem LLM: é aritmética sobre o array."""
    document = document or {}
    chain = document.get("chain") or []
    root_product = document.get("product")
    products = [root_product] + [link.get("product") for link in chain]
    replacements = len(chain)
    same_product = [item for item in products if item and item == root_product]
    recurring = len(same_product) >= RECURRENCE_THRESHOLD
    return {
        "root_order_id": document.get("order_id"),
        "product": root_product,
        "replacements": replacements,
        "chain_depth": max((link.get("depth", 0) for link in chain), default=-1) + 1,
        "same_product_count": len(same_product),
        "recurring_defect": recurring,
        "distinct_products": len({item for item in products if item}),
        "path": ([document["order_id"]] if document.get("order_id") else []) + [link.get("order_id") for link in chain],
        "reasons": [link.get("reason") for link in chain if link.get("reason")],
        # Trocar de novo um produto que já falhou 3x reproduz o defeito: vai para revisão humana.
        "needs_quality_review": recurring,
    }


def traverse_order_chain_in_memory(orders: list[dict], order_id: str, customer_key: str,
                                   *, max_depth: int = 6) -> dict | None:
    """Mesma travessia em Python, para DEMO_MODE/CI — é literalmente o loop que o
    `$graphLookup` evita: uma varredura por salto. Existe só para o fallback sem Atlas."""
    by_id = {item["order_id"]: item for item in orders if item.get("owner_customer_key") == customer_key}
    root = by_id.get(order_id)
    if root is None:
        return None
    chain: list[dict] = []
    seen = {order_id}
    current, depth = root.get("replacement_order_id"), 0
    while current and depth <= max_depth and current not in seen:
        link = by_id.get(current)
        if link is None:
            break
        seen.add(current)
        chain.append({"order_id": link["order_id"], "product": link.get("product"),
                      "status": link.get("status"), "depth": depth,
                      "reason": link.get("replacement_reason")})
        current, depth = link.get("replacement_order_id"), depth + 1
    return {"order_id": root["order_id"], "product": root.get("product"), "status": root.get("status"),
            "replacement_order_id": root.get("replacement_order_id"), "chain": chain}
