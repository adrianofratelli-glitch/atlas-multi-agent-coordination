# ADR-010 — Por que a memória do cliente não usa o `MongoDBStore` do LangGraph

- Status: aceito
- Data: 2026-10-16
- Relacionado: [ADR-002](ADR-002-memoria-llm-e-turno-pessoal.md)

## Contexto

A orquestração já usa o `MongoDBSaver` do LangGraph para o checkpoint do turno. A memória do
cliente (`customer_memory`, `long_term_memory`) poderia ir para o store do LangGraph
(`BaseStore`, implementação MongoDB em `langgraph-store-mongodb`). Contrato conferido no
`langgraph` instalado: `put(namespace, key, value, index=None, *, ttl=...)`,
`search(namespace_prefix, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None)`,
`get`, `delete`, `batch`. A implementação MongoDB não foi instalada nem medida: a decisão é sobre
o contrato.

## Decisão

Manter a memória governada pelo servidor (`app/memory.py`), porque o contrato não expressa:

1. **Supersessão transacional** — fato que contradiz outro é inserido e o antigo vira
   `active: false` + `superseded_by` numa transação; `put` sobrescreve por chave e `batch` não é
   atômico entre operações.
2. **Defesa contra envenenamento** — `looks_like_instruction` recusa fato com formato de ordem
   mesmo quando o LLM extrator o devolve; o store devolve o que foi gravado.
3. **Isolamento que recusa** — `customer_key` vem do JWT e entra em todo filtro, inclusive no
   pré-filtro do `$vectorSearch`; no store o isolamento é o `namespace_prefix` passado pelo
   chamador, e um prefixo vazio é uma busca válida e mais ampla.
4. **Orçamento como filtro de servidor** — `max_price_brl` vira `filter: {price: {$lte: N}}` no
   `$vectorSearch` do catálogo; no store seria um campo de valor sem semântica de consulta.

## Consequências

- Checkpoint continua no `MongoDBSaver` (estado do grafo: o lugar certo do contrato do LangGraph).
- Reavaliar se o store ganhar escrita condicional/transacional e recusa de namespace vazio. A PoV
  irmã chegou à mesma decisão (`../singleagent/docs/adr/ADR-012-por-que-nao-mongodbstore-langgraph.md`).
