# ADR-009 — Endurecimento do change stream de `agent_handoffs`

- Status: aceito
- Data: 2026-10-16
- Relacionado: [ADR-001](ADR-001-arquitetura-multi-agente.md)

## Contexto

`GET /api/events/stream` abre um change stream por assinante. Problemas encontrados no código:
- o `$match` tinha um ramo `fullDocument.customer_key: {$exists: false}`, que trazia para o app
  handoffs sem dono de qualquer cliente, filtrados depois com um `find_one` por evento;
- queda de conexão encerrava o gerador: sem retomada, sem resume token, sem backoff;
- `invalidate`/ChangeStreamHistoryLost viravam erro genérico e fim do feed;
- nenhum teto de assinantes: cada aba aberta era um cursor no servidor.

Os dois escritores de handoff (`orchestration_graph`, `reviews`) gravam `customer_key`.

## Decisão

1. **Filtro de dono só no servidor**: `$match` com `operationType` em `insert`/`invalidate` e
   `fullDocument.customer_key` igual ao do JWT; o ramo legado saiu. O handler ainda descarta (e
   registra) um documento fora do escopo, como asserção — nunca é a camada de isolamento.
2. **`ChangeStreamWorker` do pov-shared por assinante**: resume token em memória (vale pela vida
   da conexão SSE), reabertura com backoff e com o token, `max_await_time_ms=5000`, token do
   cursor salvo antes de reabrir; `invalidate`/286/280/260 ou token recusado →
   evento SSE `event: resync` (a UI relê `/api/handoffs`) e reabertura sem token. Sem pov-shared,
   o watch direto de antes (sem retomada).
3. **Cursor fechado ao desconectar**: o gerador é encerrado pelo `StreamingResponse`; o `finally`
   para o worker.
4. **Teto de assinantes**: `MAX_HANDOFF_SUBSCRIBERS` (100) e `MAX_HANDOFF_SUBSCRIBERS_PER_CUSTOMER`
   (5); acima disso, HTTP 429. A liberação é idempotente (fim do gerador E `BackgroundTask` da
   resposta), porque um cliente que cai antes do primeiro byte nunca roda o `finally`.
5. **DEMO_MODE**: o poll de sempre, agora filtrado por `customer_key` na própria consulta.

## Consequências

- Caos LIVE (`scripts/chaos_suite.py`): `handoff_stream_isolation` (18 assinantes de 6 clientes,
  0 vazamentos), `handoff_stream_drop` (cursor fechado no meio → retoma com token, sem
  duplicar), `handoff_history_lost` (→ `resync` e o feed volta) — 3/3.
- Assinante lento: a fila do assinante (200) descarta o mais antigo em vez de travar o stream.
