# Relatório de caos — `feat/resilience-observability` (22/09/2026)

A bateria está em `backend/scripts/chaos_suite.py` e roda de verdade; os mesmos cenários são
regressão permanente em `backend/tests/test_chaos.py` (só com `CHAOS=1`).

```bash
cd backend && CHAOS=1 ../.venv/bin/python scripts/chaos_suite.py            # bateria toda
cd backend && CHAOS=1 LIVE=1 ../.venv/bin/python scripts/chaos_suite.py     # inclui crash_resume (Atlas real)
cd backend && CHAOS=1 ../.venv/bin/python -m pytest tests/test_chaos.py -q  # como regressão
```

A degradação graciosa do supervisor, o timeout por agente, o detector de laço e o circuit breaker
por tool são o comportamento PADRÃO — a bateria não liga flag nenhuma para obtê-los; a única flag
de supervisor que aparece aqui é `SUPERVISOR_LEGACY_500=1`, no cenário que prova o modo antigo.

Injeção fica em `app/chaos.py`, atrás de `CHAOS=1`: pontos de falha no caminho REAL (gateway de
LLM, fronteira de tool, execução de agente, handoff), não mocks espalhados pelos testes. Sem a
variável, cada ponto é uma leitura de ambiente e um `return`.

## Resultado (última execução: 11/11)

| Cenário | O que injeta | Assertion | Resultado |
|---|---|---|---|
| `tool_timeout` | consulta ao Mongo pendurada 20s | turno termina em < 5s, degradação explícita, timeline registra | PASS (2,23s) |
| `agent_hang` | agente que não retorna (60s) | supervisor interrompe em ~`AGENT_TIMEOUT_SECONDS` | PASS (1,01s) |
| `llm_429_before_first_token` | 429 antes do primeiro token, sempre | turno responde mesmo assim; 3 tentativas registradas | PASS (0,76s) |
| `llm_500_mid_stream` | falha depois de o provedor responder | nunca meia resposta; custo perdido registrado em `llm_calls` | PASS (0,76s) |
| `llm_error_between_handoffs` | 503 entre dois agentes, handoff já gravado | handoff não duplica; estado explícito | PASS |
| `tool_malformed_payload` | tool devolve lista vazia / documento nulo | resposta honesta de "sem dado", nada inventado, sem 500 | PASS |
| `tool_circuit_breaker` | tool falhando sem parar | abre o circuito em 4 falhas e curto-circuita | PASS |
| `loop_guard` | mesmo agente + mesma intenção além do limite | corta a cadeia e escala para humano com motivo | PASS |
| `concurrent_same_conversation` | 5 requests simultâneos na mesma `conversation_id` | 12 mensagens, 1 documento, nenhuma exceção | PASS |
| `legacy_500_flag` | falha de tool com `SUPERVISOR_LEGACY_500=1` | a exceção volta a subir (prova que o default novo é o que degrada) | PASS |
| `crash_resume` | `SIGKILL` no meio da conversa (banco de teste isolado) | depois do restart, `/api/conversations/latest` devolve a conversa | PASS (27,3s) |

## Bugs REAIS revelados e corrigidos

1. **O timeout do supervisor não cobria as tools fora do hop de agente.**
   `tool_timeout` media 21,2s com `AGENT_TIMEOUT_SECONDS=1`: uma consulta pendurada no
   carregamento do turno (registry, regras, memória, conversa) segurava tudo antes de a cadeia
   começar. Correção: teto por tool na fronteira única (`app/resilience.py:call_tool`,
   `TOOL_TIMEOUT_SECONDS`, default 0 = comportamento anterior). Agora: 2,23s.
2. **Falha injetada na tool não contava como falha da tool.** O ponto de caos estava ANTES do
   `try` do circuit breaker, então 5 turnos seguidos de erro deixavam o contador em 0 e o
   circuito nunca abria — exatamente o que um erro real de driver faria se o breaker tivesse
   sido escrito assim. Correção: caos e teto dentro do corpo protegido; `coro.close()` no
   caminho de exceção (sem isso, `RuntimeWarning: coroutine was never awaited`).
3. **O agente "travado" não provava nada.** O ponto de caos ficava fora da corrotina
   cronometrada, então o cenário esperava 60s e o supervisor não interrompia. Correção: o hook
   passou para dentro da corrotina que o `wait_for` cronometra.

Nenhum dos três exigiu mudança de arquitetura; todos estão cobertos por teste.

## Limitações conhecidas (o que este relatório NÃO afirma)

* **Não há streaming** neste PoV. O cenário "no meio de um stream" foi implementado como a
  falha equivalente: provedor já respondeu, conexão cai antes de o turno usar o texto.
* `crash_resume` precisa de Atlas real (`LIVE=1`); sem cluster ele se declara `skipped`, nunca
  PASS. Sobe o backend numa porta própria (`CRASH_RESUME_PORT`, default 8041), sem chave de LLM
  (custo zero em tokens) e apaga a conversa que criou.
* **Isolamento de banco:** `crash_resume`, `eval_routing.py --live` e `eval.py` (modo live)
  escrevem nos bancos de teste (`<banco>_test`, `backend/scripts/isolation.py`) e recusam o banco
  da demo sem `ALLOW_DEMO_DB_WRITE=1`. O provisionamento copia da demo, em leitura, a configuração
  medida E os probes dos classificadores (`turn_probes` 44, `scope_probes` 214) com seus índices
  vetoriais, então a medição isolada exercita o mesmo caminho de embedding; o relatório do eval
  imprime o veredito (`embedding_classifiers`) em vez de deixar isso implícito.
* A degradação graciosa é o **padrão** desde 22/09/2026 — não depende de flag. `SUPERVISOR_LEGACY_500=1`
  devolve o comportamento antigo (falha sobe para o handler global, 500 com `request_id`), e o
  cenário `legacy_500_flag` existe justamente para provar que essa flag ainda funciona.
* Concorrência foi medida em 5 requests simultâneos no mesmo turno de conversa, em processo
  único. Não é teste de carga nem de múltiplas instâncias.
