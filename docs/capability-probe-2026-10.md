# Sonda de capability do cluster — 2026-10-16 (multiagente)

Gerada por `backend/scripts/capabilities.py` no banco de teste `multi_agent_poc_test` (só leitura).
Reexecute depois de mudar a versão do cluster ou o índice do KB.

| Item | Resultado |
|---|---|
| `buildInfo.version` | 9.0.4 (`enterprise`) |
| `$rankFusion` (vector + lexical com sinônimos e fuzzy) + `scoreDetails` no `kb_articles` | aceito, 497 ms (cliente → Atlas) |
| `$rankFusion` sem sinônimos/fuzzy (fallback `rankFusion_basico`) | aceito, 447 ms |
| `$rerank` `rerank-2.5-lite` depois da fusão | aceito, 641 ms |
| `kb_lexical_v1` com mapeamento `kb_sinonimos` (`lucene.portuguese`, fonte `kb_synonyms`) | `READY`, `synonymMappingStatus: READY` |

Versão mínima conferida na documentação oficial: `$rankFusion` exige **MongoDB 8.0+**
(https://www.mongodb.com/docs/manual/reference/operator/aggregation/rankfusion/). Os comentários
"8.1+" do código e dos briefings foram corrigidos. `$rerank`: MongoDB 9.0+ e Native Reranking
habilitado no projeto; `rerank-3`/`rerank-3-lite` em Public Preview
(https://www.mongodb.com/docs/vector-search/query/aggregation-stages/rerank/). Sinônimos:
https://www.mongodb.com/docs/atlas/atlas-search/synonyms/ e operador `text` (fuzzy e synonyms
não podem estar na mesma cláusula; `matchCriteria` sempre com synonyms). Medições de `$rankFusion`,
`scoreDetails` (`$meta: "scoreDetails"`, rank `"NA"`) e `$rerank` dentro da fusão (rejeitado,
code 12108704) estão no probe irmão: `../singleagent/docs/capability-probe-2026-10.md`.
