# ADR-006 — RAG do KB: `$rankFusion` com config viva no registry, `scoreDetails` e sinônimos

- Status: aceito
- Data: 2026-10-16
- Relacionado: [ADR-001](ADR-001-arquitetura-multi-agente.md) (agentes como dado), [ADR-007](ADR-007-rerank-por-agente-public-preview.md) (`$rerank`)

## Contexto

O `support_agent` já fundia vetor + BM25 com `$rankFusion` no servidor, mas pesos e profundidade
estavam fixos no código, a timeline não explicava por que um artigo veio, e a perna lexical não
entendia vocabulário de cliente: "o carregador demora muito" não trazia "Carregamento lento"
(KB-009) nem entre os 4 artigos. Os comentários diziam "MongoDB 8.1+"; a documentação oficial
diz 8.0+.

Documentação lida antes de codar:
- `$rankFusion`: https://www.mongodb.com/docs/manual/reference/operator/aggregation/rankfusion/
- Sinônimos: https://www.mongodb.com/docs/atlas/atlas-search/synonyms/ — coleção-fonte no mesmo
  banco, mapeamento com o MESMO analyzer do campo, documento inválido impede o índice, mudança na
  coleção-fonte não exige reindexar; com analyzer de idioma, entrada com stopword não casa.
- Operador `text`: https://www.mongodb.com/docs/search/query/operators-collectors/text/ —
  `fuzzy` e `synonyms` **não podem** estar na mesma cláusula; sempre usar `matchCriteria` com
  `synonyms`.

## Decisão

1. **Config viva no próprio registry.** `multiagent_brain.agent_registry.support_agent.retrieval.fusion`
   (`weights`, `per_branch_limit`, `num_candidates`, `fuzzy_max_edits`, `synonyms`,
   `score_details`). O registry já é relido a cada hop, então a mudança vale no turno seguinte
   sem restart e **sem round-trip extra** — uma collection própria (primeira versão) custou
   +~250 ms sequenciais por turno (p50 do eval 457 → 715 ms). Config ausente/malformada →
   defaults seguros + aviso no evento (`FusionConfig.from_doc`, nunca levanta).
2. **Perna lexical com três cláusulas `should`**: termo exato (título com boost 2, conteúdo),
   sinônimos (`kb_sinonimos`, `matchCriteria: "any"`) e fuzzy (`maxEdits: 1`, boost 0,5). Cláusulas
   separadas porque a doc proíbe fuzzy + synonyms juntos.
3. **Sinônimos de domínio como dado**: 13 mapeamentos `equivalent` em `kb_synonyms` (reembolso/
   estorno/ressarcimento, devolução, troca/substituição, defeito/avaria/quebrado, fone/headset,
   relógio/smartwatch, webcam/câmera, carregamento/carregador, lento/devagar/demora, rastrear/
   acompanhar, entrega/encomenda, mudo, pareamento/sincronizar), sem stopword nas entradas;
   `kb_synonym_problems` valida antes de gravar. O seed grava a coleção ANTES do índice;
   `reset_demo.py` espera READY; `scripts/isolation.py` replica no `*_test` mesmo com o banco já
   semeado; `scripts/capabilities.py` confere índice + `synonymMappingStatus`.
4. **Filtro de categoria no servidor** (opcional): `filter` do `$vectorSearch` e `compound.filter`
   do `$search`. Categoria que não é string é ignorada, nunca vira operador.
5. **Explicabilidade**: `scoreDetails: true`; cada artigo do evento `hybridSearch` leva `por_que`
   (rank, peso e contribuição por perna); a Timeline mostra "Por que estes artigos". Formato
   inesperado = "detalhe indisponível".
6. **Cascata sem flag** (`retrieval.STRATEGIES`): `rankFusion` → `rankFusion_basico` (sem sinônimos/
   fuzzy, mapeamento ausente) → `rrf_aplicacao` (duas pernas em `asyncio.gather`) → `vetor` →
   `rrf_local` (DEMO_MODE/CI). Recusa do servidor fica lembrada 5 min; timeout não marca.
7. **Funciona sem pov-shared**: `app/atlas_compat.py` usa `atlas_retrieval` (pov-shared 0.3.0) ou
   uma implementação local com o mesmo contrato; `tests/test_kb_retrieval_b1.py` cobre os dois.

## Medição (`backend/eval_kb_retrieval.py`, `eval/kb_retrieval.json`, `multi_agent_poc_test`)

33 consultas sintéticas sobre os 12 artigos (12 sinônimo, 8 paráfrase, 7 typo, 3 filtro de
categoria, 3 sem match), 3 repetições. Com 12 artigos e 4 entregues ao agente, recall@5 seria
trivial: medimos recall@1, recall@3 e MRR.

| Variante | recall@1 | recall@3 | MRR | p50 | p95 |
|---|---|---|---|---|---|
| Baseline (`main`, sem config/sinônimos/fuzzy, sem filtro de categoria) | 0,926 | 0,963 | 0,944 | 457 ms | 467 ms |
| Novo, sem sinônimos e sem fuzzy | 0,933 | 0,967 | 0,950 | 454 ms | 501 ms |
| Só fuzzy | 0,900 | 0,967 | 0,933 | 452 ms | 566 ms |
| Só sinônimos | 0,967 | 1,000 | 0,983 | 464 ms | 586 ms |
| **Sinônimos + fuzzy (default)** | **0,967** | **1,000** | **0,983** | **453 ms** | **567 ms** |
| Fallback RRF na aplicação (sem sinônimos) | 0,867 | 0,967 | 0,917 | 457 ms | 477 ms |

Nos casos de filtro de categoria, o baseline não tinha suporte; com o filtro, 3/3 corretos e todo
artigo devolvido da categoria pedida. O ganho vem dos sinônimos ("o carregador demora muito" →
KB-009 em 1º; antes fora dos 4). Efeito colateral medido: "quero a devolução do dinheiro, quanto
demora?" passou a trazer KB-009 ("Carregamento lento") em 1º e o certo (KB-011) em 2º, porque
`demora ↔ lento` é sinônimo; o artigo certo continua no contexto do agente. Fuzzy sozinho piora o recall@1 (typos já são cobertos pela perna vetorial)
e é neutro com sinônimos; fica ligado por pedido explícito e desligável por documento.

## Alternativas descartadas

- **Collection `retrieval_config` separada**: mesmo efeito, +1 round-trip sequencial por turno.
- **Sinônimos `explicit` direcionais**: os pares do domínio são simétricos; `equivalent` é o certo.
- **Expandir a consulta na aplicação** (dicionário em Python): duplicaria o que o índice faz e
  sairia do mesmo modelo de dado que a demo vende.

## Consequências

- recall@3 1,0 e MRR 0,944 → 0,983 com p50 igual; o evento do RAG explica cada artigo.
- **Demo pendente**: `kb_synonyms`, o índice com sinônimos e o bloco `retrieval` do registry só
  chegam ao banco da demo num reset controlado (`ALLOW_DEMO_DB_WRITE=1 scripts/reset_demo.py`).
  Até lá o servidor recusa a cláusula de sinônimos e o turno roda `rankFusion_basico` (medido:
  0,933/0,967/0,950), sem erro para o cliente.
- Caos LIVE (`scripts/chaos_suite.py kb_*`): sinônimos recusados, `$rankFusion` recusado, índice
  lexical em BUILDING, `$rerank` recusado e maxTimeMS — 5/5 com o artigo certo entre os 4 do agente.
