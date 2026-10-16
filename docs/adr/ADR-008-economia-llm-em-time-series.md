# ADR-008 — Economia de LLM em time series (`llm_usage_ts`)

- Status: aceito
- Data: 2026-10-16
- Relacionado: [ADR-001](ADR-001-arquitetura-multi-agente.md)

## Contexto

O custo por turno já ficava no `agent_traces` (`economics`, `llm_calls`), mas as perguntas de
gestão — custo por agente por hora, quanto o cache economizou na semana — exigiam varrer traces
inteiros (timeline, mensagens) e os contadores de `/api/metrics` viviam só em processo
(zeravam no restart). Documentação lida: https://www.mongodb.com/docs/manual/core/timeseries/timeseries-procedures/
(`timeField` BSON date obrigatório, `metaField` para o que identifica a série e quase não muda,
`granularity` `seconds|minutes|hours`, `expireAfterSeconds`, índice meta + tempo automático).

## Decisão

1. **Coleção `llm_usage_ts`** (banco principal): `timeField: at`, `metaField: meta`
   (`{kind, agent, model, area}`), `granularity: "minutes"`, `expireAfterSeconds` 90 dias.
   - *granularity minutes*: na demo, chamadas da mesma série (agente+modelo+área) chegam com
     minutos de intervalo; `seconds` criaria buckets quase vazios, `hours` juntaria a janela.
   - *TTL 90 dias*: economia é tendência; a auditoria de cada turno continua no `agent_traces`.
2. **Uma medição por chamada de LLM** (tokens in/out/cache, custo estimado, latência, status) e
   **uma por HIT da cascata** (`tokens_saved`), gravadas a partir de `_persist_trace` — o ponto de
   saída de todo turno — sem alterar `agent_traces` nem `agent_handoffs`. No Atlas, a gravação é
   uma task fora do caminho crítico (não soma round-trip à resposta); em DEMO_MODE é síncrona.
   Custo desconhecido fica `null` e marca a hora como `cost_complete: false` (nunca vira zero).
3. **Leitura agregada no servidor**: `GET /api/economics?hours=N` (1..720) e `llm_usage` dentro de
   `/api/metrics` — um `$facet` com custo/tokens por agente por hora (`$dateTrunc`) e economia do
   cache (hits, chamadas, taxa, tokens economizados). Falha de leitura → caminho anterior
   (contadores em processo), marcado `source: "in_process"`.
4. **DEMO_MODE**: mesma gravação no armazenamento em memória e a MESMA agregação em Python
   (`source: "memory"`), testada contra o mesmo contrato.
5. A coleção é criada por `seed.py` (com índices) e por `scripts/isolation.py`; se um insert
   chegasse antes, o MongoDB criaria uma coleção comum com o mesmo nome — `ensure_collection`
   avisa nesse caso.

## Alternativas descartadas

- **Agregar `agent_traces`**: documentos grandes (timeline inteira), sem bucketing por tempo.
- **Prometheus/painel externo**: outro sistema para o que é uma query de agregação no mesmo
  cluster — exatamente a tese da PoV.

## Consequências

- Medido no `multi_agent_poc_test` com o `eval_routing --live`: 28 medições, custo por agente por
  hora e hit rate do cache (3 HITs, 625 tokens economizados) numa agregação.
- **Demo pendente**: a coleção só existe na demo depois de um reset controlado
  (`ALLOW_DEMO_DB_WRITE=1 scripts/reset_demo.py`); até lá a leitura falha e o endpoint devolve os
  contadores em processo.
