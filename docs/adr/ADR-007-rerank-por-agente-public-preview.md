# ADR-007 — `$rerank` por agente (Public Preview nos `rerank-3*`), decidido pela medição

- Status: aceito
- Data: 2026-10-16
- Relacionado: [ADR-006](ADR-006-kb-rankfusion-config-viva-sinonimos.md), [ADR-001](ADR-001-arquitetura-multi-agente.md)

## Contexto

O `$rerank` reordena documentos com os modelos de reranking da Voyage AI, nativo no Atlas
(https://www.mongodb.com/docs/vector-search/query/aggregation-stages/rerank/). `rerank-3` e
`rerank-3-lite` estão em **Public Preview** (MongoDB 9.0+, cluster dedicado) — não são GA e não
são apresentados como tal; `rerank-2.5`/`rerank-2.5-lite` não carregam esse rótulo na doc. O
recurso está habilitado no projeto. Regras da doc: `model` obrigatório, `numDocsToRerank` ≤ 1000,
campo de `path` presente em todo documento, estágio fora de input pipeline do `$rankFusion`, e
`$meta: "score"` substituído pelo do rerank.

## Decisão

1. **Config por agente no registry**, relida a cada hop: `agent_registry.<agente>.retrieval.rerank`
   (`enabled`, `model`, `pool`, `top_n`). Malformada → desligada (`RerankConfig.from_doc`).
2. **`support_agent` (KB): desligado.** Depois do `$rankFusion`: `$limit pool` → cópia do score
   da fusão → `$rerank` (`title` + `content`) → `$limit 4`.
3. **`product_agent` (catálogo): ligado, `rerank-2.5-lite`.** O `$rerank` recebe os 8 candidatos do
   `$vectorSearch` (preço/categoria já filtrados no servidor) e o score dele substitui a
   relevância vetorial na MESMA ponderação de negócio (relevância 0,55 + nota 0,30 + estoque 0,15).
4. **Fallback no-op**: estágio recusado → pipeline sem rerank, evento "$rerank indisponível" na
   timeline, recusa lembrada 5 min. Quando roda, a timeline mostra o modelo, o tempo e
   "(Public Preview)" se o modelo for `rerank-3*`.

## Medição (banco de teste, 3 repetições)

**KB** (`eval_kb_retrieval.py`, 12 artigos):

| Variante | recall@1 | recall@3 | MRR | p50 | p95 |
|---|---|---|---|---|---|
| `$rankFusion` (default, ADR-006) | 0,967 | 1,000 | 0,983 | 453 ms | 567 ms |
| + `rerank-2.5-lite`, pool 20 | 0,967 | 1,000 | 0,978 | 638 ms | 768 ms |
| + `rerank-2.5-lite`, pool 40 | 0,967 | 1,000 | 0,978 | 641 ms | 766 ms |
| + `rerank-2.5-lite`, pool 80 | 0,967 | 1,000 | 0,978 | 653 ms | 802 ms |
| + `rerank-3-lite` (Preview), pool 20 | 0,967 | 1,000 | 0,983 | 678 ms | 872 ms |

Nenhum ganho e +185 ms: pool 20/40/80 é igual porque a base tem 12 artigos. **Desligado.**

**Catálogo** (`eval_product_rerank.py`, 12 consultas nominais, 20 SKUs):

| Variante | recall@1 | recall@4 | MRR | p50 | p95 |
|---|---|---|---|---|---|
| Atual (relevância vetorial na ponderação) | 0,417 | 0,917 | 0,639 | 447 ms | 463 ms |
| `rerank-2.5-lite` no lugar da relevância | **0,833** | **1,000** | **0,917** | 628 ms | 734 ms |
| `rerank-3-lite` (Preview) | 0,833 | 1,000 | 0,917 | 645 ms | 728 ms |

O `search_text` do catálogo é genérico ("Produto eletrônico para uso cotidiano"): o vetor quase
não distingue "teclado mecânico" de "teclado"; o reranker lê o nome. Mesmo resultado nos dois
modelos → o não-Preview.

**Turno multiagente** (`eval_routing.py --live`, 24 casos, rerank do produto desligado vs ligado):
p50 10,9 s → 11,5 s, **p95 16,3 s → 16,2 s**, rota 100% e resolução 100% nos dois, 1.430 → 1.446
tokens/turno. O custo do hop (~+180 ms) some na latência do LLM. Relatórios em
`eval/reports/routing-live-product-rerank-{off,on}-2026-10-16.json`.

## Alternativas descartadas

- **Rerank no KB**: nenhum ganho medido nesta base; reavaliar com centenas de artigos.
- **Substituir a ponderação de negócio pelo rerank puro**: perderia nota e estoque, que são
  regra da loja, não relevância textual.

## Consequências

- Recomendação de produto acerta o item pedido em 1º no dobro dos casos medidos.
- **Demo pendente**: o bloco `retrieval` do registry chega à demo num reset controlado; até lá o
  `product_agent` roda sem rerank (comportamento anterior).
- Cada turno de produto faz uma chamada à Voyage (cobrada pela MongoDB, não pelo cluster); custo
  e latência ficam no evento da timeline.
