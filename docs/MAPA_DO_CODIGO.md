# Mapa do código — atlas-multi-agent-coordination

> Também conhecida como: **Multi-Agent** (pasta antiga `multiagente-atendimento`). Portas: frontend 5191 / backend 8031.
> Em uma frase: um atendimento ao cliente com 8 agentes (1 orquestrador + 7 especialistas) em que o MongoDB Atlas é também o plano de coordenação: registro de agentes, regras de roteamento, handoffs, memória, cache, decisões e checkpoints do turno são documentos.

Contexto geral nos briefings: `docs/briefing/architecture.md`, `docs/briefing/queries.md`, `docs/briefing/agent-behavior.md`, `docs/briefing/ui-flows.md`. Este mapa não os repete; ele diz onde cada coisa está no código.

## Como ler este código em 30 segundos

- **Quem decide o quê (agentes):** cada especialista faz tool calling (`backend/app/agent_loop.py::run_tool_agent`) sobre as tools tipadas de `backend/app/tools.py` (`TOOLS`), filtradas pelo `allowed_tools` do `agent_registry`. Sem modelo, o runner determinístico do agente responde: `backend/app/agents.py`, dicionário `RUNNERS` (`agents.py:1020`).
- **Quem encadeia os agentes (nós do grafo):** `backend/app/orchestration_graph.py` (LangGraph). `n_chain_prepare` abre o quadro do turno e o plano; `n_agent_hop` roda UM agente por super-step e valida o próximo destino; `n_approval` pausa com `interrupt()` e `n_apply_approved` executa a escrita aprovada uma vez; `n_consolidate` escreve a resposta única; até 5 saltos (`MAX_HOPS`, `orchestration.py:67`).
- **Onde o código fala com o MongoDB:** quase tudo passa pela classe `DataStore` (`backend/app/database.py:137`), com métodos `find_one`, `find_many`, `aggregate`, `insert_one`, `update_one`, `replace_one`, `delete_many`, `reserve_quota`, `watch_handoffs`. Procure `store.<método>(` ou, nos poucos casos de busca sofisticada, `store._collection(...).aggregate(`. A seção 3 lista todos.
- **Modo sem Atlas:** com `DEMO_MODE=1` o `DataStore` usa um backend em memória (testes e CI); `$vectorSearch`, `$rankFusion`, `$rerank` e change stream reais só existem com Atlas.

## 1. Árvore anotada

```
backend/
  run.py                        Sobe o uvicorn (porta vem de API_PORT, padrão 8031)
  seed.py                       Seed idempotente: dados, agent_registry, routing_rules, índices (B-tree, TTL, Search e Vector); recusa banco da demo sem ALLOW_DEMO_DB_WRITE=1
  seed_scope_probes.py / seed_turn_probes.py / seed_tool_registry.py   Seeds extras: frases-sonda de escopo, de turno pessoal e registro da busca web
  sync_demo_scenarios.py        Sincroniza os cenários de demo (seed_data.DEMO_SCENARIOS) com o banco
  restore_demo_fixtures.py      Restaura dados de demo alterados em ensaio
  warmup.py                     Script do aquecimento do cache (o serviço está em app/warmup.py)
  calibrate_thresholds.py       Mede os thresholds semânticos (cache, denylist, turno) contra probes rotulados
  eval.py, eval_routing.py, eval_situations.py, eval_kb_retrieval.py, eval_product_rerank.py, generate_situations.py   Avaliações (ver eval/ e a seção 6)
  eval_answers.py               Eval de resposta: trajetória, efeito no banco, juiz LLM, pass@k / pass^k (eval/answer_dataset.json)
  mcp_server.py                 Servidor MCP (stdio) somente leitura: tools de leitura + recursos de handoffs e quadro, identidade do JWT
  migrate_legacy_memory.py      Migração de memória antiga para o formato atual
  scripts/                      reset_demo.py (reset completo), capabilities.py (checa o cluster, só leitura), chaos_suite.py, crash_resume.py, isolation.py, trace_query.py
  app/
    main.py                     Todas as rotas /api/*, middlewares, auth, SSE, lifespan
    orchestration_graph.py      O turno como grafo LangGraph: um nó por etapa, run_turn / resume_turn / continue_thread (ver seção 2.1)
    orchestration.py            OrchestrationService: limites da cadeia, roteamento por LLM, fan-out, gravar conversa e trace
    router.py                   Roteamento determinístico (cheap_route, detect_fanout, deterministic_orchestrator, matched_agents)
    tools.py                    Tools tipadas dos especialistas (TOOLS), executores com filtro de dono, portões de escrita
    agent_loop.py               Loop de tool calling de um especialista (run_tool_agent, transfer_to_agent)
    planner.py                  Plano do turno pelo orquestrador (pedido composto -> tarefas por especialista)
    blackboard.py               Quadro do turno (turn_blackboard): abrir, $push por hop, status, resumo só-de-fatos
    consolidate.py              Resposta única a partir do quadro (LLM ou junção determinística)
    hitl.py                     Políticas de aprovação humana (hitl_policies) e o casamento tool+fatos
    compare.py / judge.py       A/B agente único x time na mesma pergunta / juiz LLM (completude, coerência, grounding)
    agent_cards.py              Agent cards A2A 1.0 gerados do agent_registry
    agents.py                   Os 7 runners determinísticos (fallback sem modelo) + operações de negócio que as tools reusam
    guardrails.py               Guardrail de entrada/saída: denylist estático, vetorial e classificador LLM
    dilution.py                 Anti-diluição: pontua mensagem inteira e cláusulas, vale o maior score
    guardrail_learning.py       Aprendizado do guardrail: candidatas em quarentena por cliente, promoção global
    scope_classifier.py         Classificador de escopo por embedding (in / out / chat), 0 tokens quando recusa
    turn_classifier.py          Classificador "este turno é pessoal?" (decide se memória/cache entram)
    cascade.py                  Cascata de cache semântico (curto prazo -> global) + contexto de memória longa
    memory.py                   Extrator LLM de fatos do cliente, dedup, supersessão transacional (customer_memory)
    retrieval.py                Busca híbrida na base de conhecimento ($rankFusion, vetor, lexical, rerank)
    atlas_compat.py             Helpers de $rankFusion/$rerank: usa o pov-shared se existir, senão versão local
    graph.py / replacement.py   $graphLookup da cadeia de trocas / regra de qualidade que decide trocar ou escalar
    policies.py                 Filtros de ownership reconstruídos no backend (o LLM nunca escolhe o filtro)
    decisions.py / reviews.py   Trilha de auditoria (agent_decisions) e fila de revisão humana (pending_reviews)
    web_search.py               Busca web (Tavily) governada pelo Atlas: registro, cota mensal, cache, auditoria
    guidance.py / clock.py      Textos de orientação ("não encontrei...") e relógio de referência
    llm.py                      Gateway de LLM (Grove), custo por chamada
    budget.py / economics.py / usage_ts.py   Orçamento de tokens do turno, custo, e série temporal de uso (llm_usage_ts)
    live_events.py              Eventos ao vivo (timeline) para o SSE
    database.py                 DataStore, transações, change stream de handoffs, validators $jsonSchema, índices
    seed_data.py                Dados de demo: clientes, pedidos, agentes, regras de roteamento, cenários (DEMO_SCENARIOS), casos do eval
    models.py, config.py        Modelos Pydantic de entrada/saída; configuração (Settings, lê o .env)
    security.py, rate_limit.py  JWT, admin key, mascara de PII, rate limit
    observability.py, langfuse_client.py, metrics.py   Tracing, uma trace Langfuse por turno, métricas
    resilience.py, chaos.py     Timeout/retry/breaker em volta de LLM/Atlas; injeção de falhas
    edge_guardrails.py          Máscara de PII e validação de saída (usa pov-shared quando instalado)
    demo_reset.py               "Reiniciar memória da demo": desfaz o que a demo gravou para um cliente
    warmup.py                   Aquecimento automático do cache semântico
  tests/                        pytest (offline) e tests/adversarial/ (ver seção 6)
frontend/
  src/App.jsx                   Estado da sessão e navegação: abas Chat / A/B / Agentes / Decisões / Métricas
  src/api.js                    Todas as chamadas HTTP e o stream SSE (URL base: VITE_API_URL, padrão 127.0.0.1:8031)
  src/graphModel.js             Puro: layout do grafo e derivação do turno (arestas, plano, escritas, pausa HITL) a partir dos eventos
  src/mongoOps.js, costs.js, redact.js   Puros: feed "Por baixo do capô", custo por agente, máscara de filtros
  src/agentic.css               Estilos das telas de coordenação (carregado depois do pov-signature.css)
  src/components/ChatStage.jsx  Tela de Chat: conversa fixa, grafo compacto + narrativa (abas Quadro/Custo/Memória), capô ligado à narrativa
  src/components/AgentGraph.jsx Grafo SVG do turno (orquestrador no centro, especialistas do registry em volta)
  src/components/UnderTheHood.jsx   "Por baixo do capô": uma linha por evento (operação MongoDB com o filtro real, fonte externa, evento sem coleção)
  src/components/BlackboardView.jsx Quadro do turno (`turn_blackboard`): plano + entradas por hop
  src/components/ChatPanel.jsx  Conversa, cartão "Aguardando analista" (HITL), roteiro rápido
  src/components/CostPanel.jsx  Custo do turno por agente (`llm_calls`) e por janela (`/api/economics`)
  src/components/TypedMemory.jsx    Memória semântica / episódica / procedural (`/api/memory-typed`)
  src/components/CrashResume.jsx    Resultado de "Simular queda e retomar do checkpoint"
  src/components/AgentsPage.jsx Aba "Agentes": registry editável (ativo, modelo, tools), políticas HITL, Guardrails
  src/components/GuardrailsPanel.jsx    Eventos, candidatos e denylist de guardrail (admin)
  src/components/ComparePage.jsx    Aba "A/B": generalista × time na mesma pergunta (`POST /api/compare`)
  src/components/MetricsPage.jsx    Aba "Métricas": cartões, economia por agente, ledger por collection, eval
  src/components/EventDetail.jsx   "ver detalhe" de uma linha do capô (título, agente, duração, resultado mascarado, query)
  src/components/QueryDetails.jsx   Detalhe da consulta MongoDB de um evento
  src/components/ReplacementChain.jsx   Desenha a cadeia de trocas do $graphLookup
  src/components/CompliancePage.jsx     Aba "Decisões": trilha de auditoria e fila de revisão humana (aprovar/recusar escrita pausada)
  src/components/Inspector.jsx  Painel lateral "inspetor MongoDB" (não importado em nenhum lugar hoje; ver Dúvidas)
  src/execution.js, kbExplain.js, richtext.js   Funções puras de apoio
  tests/*.test.mjs              Testes JS (node --test)
eval/                           Datasets: routing_dataset.json, answer_dataset.json, kb_retrieval.json, product_retrieval.json, FORMAT.md; reports/
docs/                           adr/ (13 decisões), briefing/, architecture.md, chaos-report.md, agent-quality-economics.md
start.sh                        Sobe backend e frontend
```

## 2. "Apertei o botão X, o que roda?"

Todas as chamadas HTTP do navegador saem de `frontend/src/api.js` (objeto `api`, linha 37). A identidade (Ana, Bruno, Carla, Diego, `IDENTITIES` em `App.jsx:12`) vira JWT em `POST /api/auth/token`; o `customer_key` só existe no token.

| Tela / botão | Componente | Chamada HTTP | Handler (`main.py`) | Função de negócio | MongoDB |
|---|---|---|---|---|---|
| Abrir o app | `App.jsx:108` (useEffect) -> `api.warmup()` + `switchIdentity` (86) | `POST /api/warmup`, `POST /api/auth/token`, `GET /api/health`, `/api/agents`, `/api/graph/topology`, `/api/memory-typed`, `/api/metrics`, `/api/conversations/latest`, `/api/demo-scenarios` | `warmup`, `create_token`, `health`, `agents`, `graph_topology`, `typed_memory`, `get_metrics`, `latest_conversation`, `demo_scenarios` | `warmup.WarmupService.run` (`warmup.py:62`); `security.issue_token` | `customers` find_one (`main.py::create_token`); `agent_registry` (brain) find; `customer_memory`, `long_term_memory`, `hitl_policies`; `agent_conversations`, `agent_traces` |
| Seletor "identidade" | `App.jsx::switchIdentity` | idem acima | idem | idem | idem |
| Chat: enviar (Enter / botão) e chips de sugestão | `App.jsx::send` (chips: `ChatPanel.jsx`, `onSuggestion`); depois do turno `App.jsx::afterTurn` lê `GET /api/blackboard/latest`, `/api/memory-typed`, `/api/metrics` | `POST /api/chat/stream` (SSE, `api.js:chatStream`; cai para `POST /api/chat`) | `chat_stream` / `chat` -> `_run_chat` | `orchestration_graph.py::run_turn` (o grafo, ver 2.1) | ver 2.1 |
| Chat: select "roteiro rápido" (cenários de demo) | `ChatPanel.jsx` (só preenche a caixa de texto com `demo.message`; quem envia é o botão enviar) | nenhuma ao escolher; a lista veio de `GET /api/demo-scenarios` | `demo_scenarios` | lê os cenários semeados de `seed_data.DEMO_SCENARIOS` | `find_many` em `demo_scenarios` |
| Chat: sugestões de próximos passos | resposta do turno traz `suggestions` | no mesmo turno | n/a | `orchestration_graph.py::_next_steps` / `orchestration.py::_next_steps` | `find_many` em pedidos/faturas (`guidance.py::customer_snapshot`) |
| "Nova conversa" | `App.jsx::newConversation` | nenhuma | n/a | novo `conversation_id` no navegador | nenhum |
| "Reiniciar memória da demo" | `App.jsx::resetDemoMemory` | `POST /api/demo/reset` | `demo_reset` | `demo_reset.py:12` `reset_customer_memory` | `customer_memory` (find_many, update_one, delete_many), `short_term_memory`, `long_term_memory`, `semantic_cache` |
| Pílula "ao vivo" (Change Stream) | `App.jsx` (`connect` -> `api.streamEvents`; cada evento vai para `streamHandlerRef`: painel do capô e, se `from_agent` = `human_reviewer`, `syncResumedFromStore`) | `GET /api/events/stream` (SSE) | `events_stream` -> `_handoff_event_stream` | `database.py:466` `_watch_handoffs_atlas` | `watch` em `agent_handoffs`, filtrado por `customer_key` (`database.py:460`, `handoff_pipeline`) |
| Grafo ao vivo e "Por baixo do capô" | `AgentGraph.jsx`, `UnderTheHood.jsx` (derivados por `graphModel.js::deriveTurn` e `mongoOps.js::hoodRows`) | nenhuma: eventos SSE do turno + resultado final (`agent_path`, `awaiting_approval`) | n/a | n/a | n/a (mostra o filtro real de cada operação) |
| Abas sob a narrativa: Quadro, Custo, Memória | `ChatStage.jsx` -> `BlackboardView.jsx`, `CostPanel.jsx::TurnCost`, `TypedMemory.jsx` | `GET /api/blackboard/latest?conversation_id=`, `GET /api/memory-typed` | `latest_blackboard`, `typed_memory` | leitura direta | `turn_blackboard` find (último por `created_at`); `customer_memory`, `long_term_memory`, `agent_registry`, `routing_rules`, `hitl_policies` |
| Escrita pausada: cartão "Aguardando analista" | `ChatPanel.jsx` (resultado com `awaiting_approval`) | nenhuma ao exibir; a retomada chega pelo Change Stream ou pelo `resumed_turn` do resolve | n/a | `orchestration_graph.py::_paused_output` | `pending_reviews` (aberta no hop), `turn_blackboard.status = awaiting_approval` |
| "Simular queda e retomar do checkpoint" | `App.jsx::simulateCrash`, diálogo `CrashResume.jsx` | `POST /api/demo/crash-resume`, depois `GET /api/handoffs?conversation_id=` (conferência de duplicados) | `demo_crash_resume`, `handoffs` | `orchestration_graph.py::run_turn` (`crash_before_hop=1`, `turn_key`) + `resume_after_crash` (-> `continue_thread`) | `langgraph_checkpoints`, `agent_handoffs` find_many |
| Aba "A/B" | `ComparePage.jsx` | `POST /api/compare` | `compare_architectures` | `compare.py::compare` (escritas simuladas, juiz LLM `judge.py::judge`) | leitura de `agent_registry`, `routing_rules`, `hitl_policies`, `agent_conversations`, `tool_registry` |
| Detalhe de um evento do turno ("ver detalhe" no capô) | `EventDetail.jsx`, `QueryDetails.jsx` | nenhuma (dados já vieram no turno) | n/a | n/a | n/a |
| Cadeia de trocas | `ReplacementChain.jsx`, usado em `BlackboardView.jsx` | nenhuma (vem no turno) | n/a | `graph.py:17` `build_order_chain_pipeline` | `$graphLookup` em `orders` |
| Aba "Métricas" | `MetricsPage.jsx` | `GET /api/metrics`, `GET /api/economics?hours=N`; com modo admin `GET /api/eval/runs` | `get_metrics`, `get_economics`, `eval_runs` | `metrics`; `usage_ts.py::usage_report` | `llm_usage_ts` (agregação), `eval_runs` find_many; contadores em memória |
| Aba "Agentes": liga/desliga agente, modelo, tools | `AgentsPage.jsx` (`patch`, `AgentCard`) | `GET /api/tools`, `PATCH /api/admin/agents/{agent_key}` (precisa `X-Admin-Key`) | `tool_catalog`, `update_agent` | `tools.py::tools_for` | `agent_registry` update_one (brain) + `admin_audit` insert_one |
| Aba "Agentes": políticas de aprovação humana | `AgentsPage.jsx` | `GET`/`PATCH /api/admin/hitl-policies` | `hitl_policies`, `update_hitl_policy` | `hitl.py` | `hitl_policies` find / update_one (brain) + `admin_audit` |
| Aba "Decisões": trilha de auditoria | `CompliancePage.jsx` (`loadTrail`) | `GET /api/decisions` | `decisions` | `decisions.py:149` `decision_trail` | `agent_decisions`, `agent_audit_events` find_many |
| Aba "Decisões": fila de revisão humana (modo admin) e botões de resolver | `CompliancePage.jsx` (lista e resolver; escrita pausada, `evidence.kind = hitl_interrupt`, mostra só "Aprovar escrita"/"Recusar"; o `resumed_turn` é aplicado no chat pelo `App.jsx`) | `GET /api/admin/reviews`, `POST /api/admin/reviews/{id}/resolve`; cliente vê as suas em `GET /api/reviews` | `pending_reviews`, `resolve_pending_review`, `my_reviews`; repetição: `continue_review_turn` (`POST /api/admin/reviews/{id}/continue`) | `reviews.py:111` `resolve_review`; pausa real: `orchestration_graph.py::resume_turn` (thread gravada em `evidence.thread_id`, conferência do `review_id`) e `continue_thread` | `pending_reviews` (find_one, `update_one` com claim, `applied_at`), `agent_decisions`/`agent_audit_events` insert, `agent_handoffs` insert (revisor e "turno retomado"), `admin_audit`, checkpoint da thread do turno |
| Caixa "modo admin" (abas Agentes, Decisões e Métricas) | `CompliancePage.jsx`, `AgentsPage.jsx` | nenhuma (guarda a chave admin do `.env` do frontend) | n/a | n/a | n/a |
| Guardrails (aba Agentes, admin): eventos, candidatos, denylist; aprovar candidato segue só via API (a listagem não devolve `_id`) | `GuardrailsPanel.jsx` | `GET /api/guardrails/{view}`, `POST /api/admin/guardrails/candidates/{id}/approve` | `guardrails`, `approve_candidate` | `guardrail_learning.py` | `guardrail_candidates`, `guardrail_denylist` (`replace_one` upsert) |
| Agente externo (A2A) | n/a | `GET /.well-known/agent-card.json`, `GET /api/agents/{agent_key}/card` | `a2a_agent_card`, `a2a_single_agent_card` | `agent_cards.py::system_card`, `agent_card` | `agent_registry` find / find_one (brain) |
| Agente externo (MCP, stdio) | n/a | `backend/mcp_server.py` com `MCP_CUSTOMER_TOKEN` | n/a | `mcp_server.py::create_server`; `tools.py::execute` | `agent_registry` a cada chamada; tools de leitura; `agent_handoffs`, `turn_blackboard` (recursos) |

### 2.1 O turno, nó por nó (`orchestration_graph.py::_build_graph`)

Ordem: `ingest -> guardrail -> (blocked | memory_extract) -> fanout_check -> (fanout | decide) -> (out_of_scope | cache_lookup) -> (cache_hit | chain_prepare) -> agent_hop -> (advance -> agent_hop | approval -> apply_approved -> (advance | consolidate) | consolidate) -> finalize -> END`.

| Nó | Quem faz o quê | Onde toca o MongoDB |
|---|---|---|
| `orchestration_graph.py::n_ingest` | Carrega o contexto em paralelo: agentes ativos, regras, memória do cliente, conversa, config da web, orçamento global | `agent_registry`, `routing_rules`, `model_config` (brain), `customer_memory`, `agent_conversations` (`find_many`/`find_one`); `web_search.get_config` -> `tool_registry` |
| `orchestration_graph.py::n_guardrail` | `guardrails.check_input` (`guardrails.py:147`): denylist estático, `$vectorSearch` no denylist (`semantic_denylist`, linha 40; por cláusula, `dilution.py`), classificador LLM só se necessário; ao bloquear, `guardrail_learning.learn` (`guardrail_learning.py:172`) | `$vectorSearch` em `guardrail_denylist`; `find_many` do denylist e `guardrail_policies` (`guardrails.py:324`, `_load_denylist_and_policy`); `insert_one` em `guardrail_events`/`guardrail_candidates` |
| `orchestration_graph.py::n_blocked` | Resposta de bloqueio e registro | `guardrails.log_event` (`guardrails.py:346`) |
| `orchestration_graph.py::n_memory_extract` | `memory.extract_and_store` (`memory.py:142`): LLM extrai fatos; dedup e supersessão em transação | `customer_memory` `find_many`, `insert_one` + `update_one` na transação |
| `orchestration_graph.py::n_fanout_check` / `n_fanout` | `router.detect_fanout`: pergunta composta pedido + fatura roda os dois agentes em paralelo (`orchestration.py:150` `_run_fanout`); status sujeito a política de aprovação nunca vai ao fan-out (precisa da pausa da cadeia) | `routing_rules` (`find_many`); `hitl_policies` (brain) |
| `orchestration_graph.py::n_decide` | Roteamento: `router.cheap_route` (por keyword/prioridade), `scope_classifier.classify` (`scope_classifier.py:150`), `_route_with_llm` (`orchestration.py:123`) só quando não há sinal de regra | `$vectorSearch` em `scope_probes` (`scope_classifier.py:125`, `best_scores`); config em `scope_classifier_config` |
| `orchestration_graph.py::n_out_of_scope` | Fora de escopo decisivo: orientação fixa, 0 tokens, sem agente | `find_many` de pedidos para a orientação (`guidance.py`) |
| `orchestration_graph.py::n_cache_lookup` | `turn_classifier.classify` (turno pessoal?) e `cascade.cascade_lookup` (`cascade.py:57`) | `$vectorSearch` em `turn_probes`; `short_term_memory` + `$unionWith` `semantic_cache` (`cascade.py:83`); fallback exato `cascade.py:156` |
| `orchestration_graph.py::n_cache_hit` | Devolve a resposta em cache, sem LLM; grava curto prazo | `cascade_store_short_term` (`cascade.py:218`, `replace_one`) |
| `orchestration_graph.py::n_chain_prepare` | MISS: contexto de longo prazo, especialidades detectadas (`router.matched_agents`), abre o quadro, plano do orquestrador em pedido composto (`planner.py::plan_turn`), carrega `hitl_policies` | `cascade_long_term_context` (`cascade.py:174`, `$vectorSearch` em `long_term_memory`); `routing_rules`; `turn_blackboard` `insert_one` + `update_one` (plano); `hitl_policies` find (brain) |
| `orchestration_graph.py::n_agent_hop` | UM agente: portões (taint web, agente desativado, `LoopGuard`), `agent_loop.run_tool_agent` (ou o runner de `RUNNERS` sem modelo), entrada no quadro, revisão pausada se uma tool pediu aprovação, validação do próximo destino (`transfer_to_agent` ou passo pendente do plano) e `_record_handoff` | `agent_registry` find_one (agentes com escrita); executores de `tools.py` (seção 2.2); `turn_blackboard` `$push`; `pending_reviews` (`reviews.open_review`); `agent_handoffs` `insert_one` |
| `orchestration_graph.py::n_advance` | Passa o bastão: destino validado vira o agente corrente, com a tarefa (`blackboard.safe_task`) | nenhum |
| `orchestration_graph.py::n_approval` | `interrupt()`: o turno para no checkpoint; sem efeito colateral (roda de novo na retomada) e devolve `approval_decision` | nenhum (estado no checkpoint) |
| `orchestration_graph.py::n_apply_approved` | Executa a escrita aprovada uma vez pelo mesmo executor (`tools.execute` com `approved_action`) ou registra a recusa; idempotente por `applied_at`; segue o handoff pendente | `pending_reviews` find_one / `update_one` (`applied_at`); escrita da tool aprovada; `turn_blackboard` `$push`; `agent_handoffs` |
| `orchestration_graph.py::n_consolidate` | `consolidate.consolidate`: resposta única a partir do quadro (LLM; com 1 entrada ou sem modelo, junção determinística) | `turn_blackboard` `update_one` (`status: consolidated`, `final_response`) |
| `orchestration_graph.py::n_finalize` | `guardrails.check_output`, cache/episódio, conversa, trace, sugestões | `cascade_store_turn` (`cascade.py:243`), `cascade_store_episode` (199); `_update_conversation` (`orchestration.py:206`, `update_one` em `agent_conversations`); `_persist_trace` (`orchestration.py:254`, `insert_one` em `agent_traces`); `usage_ts.record_turn` (`llm_usage_ts`, série temporal) |

Entradas do grafo: `orchestration_graph.py::run_turn` (turno normal, uma thread por turno `<customer_key>:<conversation_id>:<turno>`, `checkpoint_thread_id`; com `crash_before_hop` na demo de queda), `resume_turn` (`Command(resume=...)` na thread gravada na revisão, só se a pausa pendente é dela) e `continue_thread` / `resume_after_crash` (`ainvoke(None)` na mesma thread). Turno pausado responde por `_paused_output`.

### 2.2 Qual agente faz o quê (`backend/app/tools.py` e `backend/app/agents.py`)

Com modelo, cada especialista chama as tools da coluna "Tools" (`tools.py::TOOLS`, liberadas por `allowed_tools`) e transfere com `transfer_to_agent` (`agent_loop.py::_transfer_tool`); sem modelo, o runner determinístico responde e decide o handoff por código.

| Agente (chave) | Tools (efeito) | Runner determinístico | Escreve? | Collections |
|---|---|---|---|---|
| Orquestrador (`orchestrator`) | nenhuma; roteia (`router.py::cheap_route`, `deterministic_orchestrator`, `orchestration.py:123` `_route_with_llm`), planeja (`planner.py::plan_turn`) e consolida (`consolidate.py::consolidate`) | não é runner | Não | `routing_rules`, `agent_registry` (brain), `turn_blackboard` |
| Pedidos (`order_agent`) | `get_order` (leitura), `request_order_status_change` (escrita) | `agents.py::run_order_agent` | Sim: status de lista fechada; troca via `replacement.apply_replacement`; reembolso pausa (`HITL-REEMBOLSO`) | `orders` (`agents.py::load_order`, `change_order_status`) |
| Produtos (`product_agent`) | `search_catalog` (leitura) | `agents.py::run_product_agent` | Não | `products_catalog`: `$vectorSearch` + `$rerank` opcional (`agents.py::build_product_pipeline`, `build_product_rerank_pipeline`, `search_products_ranked`) |
| Suporte (`support_agent`) | `search_knowledge_base` (leitura), `open_support_ticket` (escrita) | `agents.py::run_support_agent` | Sim: `support_tickets` (um por conversa), só com pedido explícito | `kb_articles` via `agents.py::search_kb` -> `retrieval.search_kb_explained`; `support_tickets` |
| Cobrança (`billing_agent`) | `get_invoice` (leitura) | `agents.py::run_billing_agent` | Não | `invoices` |
| Garantia (`warranty_agent`) | `check_warranty` (leitura) | `agents.py::run_warranty_agent` | Não | `orders`, `products_catalog`, `warranty_policies`; `$graphLookup` (`agents.py::order_replacement_chain`) |
| Fidelidade (`loyalty_agent`) | `get_loyalty_account` (leitura), `redeem_reward` (escrita) | `agents.py::run_loyalty_agent` | Sim, em transação: debita pontos e grava `redemptions`; 500+ pontos pode pausar (`HITL-RESGATE-ALTO`, desligada no seed) | `loyalty_accounts`, `redemptions` (`agents.py::redeem_reward`) |
| Logística (`logistics_agent`) | `get_shipment` (leitura), `request_delivery_reschedule` (escrita), `search_carrier_news` (externa) | `agents.py::run_logistics_agent` | Sim: `reschedule_requested` em `shipments` | `shipments` (`agents.py::load_shipment`); `web_search.search` via `agents.py::carrier_news` |

Regras de segurança que valem para todos: os filtros de leitura/escrita são reconstruídos em `policies.py` (`safe_order_read_filter` 29, `safe_order_update` 41, `safe_invoice_filter` 69, `safe_shipment_filter` 76); toda tool de escrita passa por `tools.py::_write_gate` (taint web, pedido de ação explícito na mensagem do cliente via `_wants`/`_wants_status`, pedido citado) e `tools.py::_needs_approval` (`hitl_policies`); no caminho determinístico o LLM só redige a frase final (`agents.py::llm_synthesize`) sobre o documento já buscado.

## 3. Onde estão as queries MongoDB

Banco de negócio: `multi_agent_poc`; banco de configuração ("brain"): `multiagent_brain` (`config.py`). Métodos `brain=True` do `DataStore` apontam para o segundo. Visão geral por texto em `docs/briefing/queries.md`.

**Pipelines (aggregate / Search):**

| O que faz | arquivo::função (linha) | operador / pipeline principal | collection |
|---|---|---|---|
| Cadeia de trocas | `graph.py::build_order_chain_pipeline` (17), operador na 26; chamada em `agents.py::order_replacement_chain` (tool `check_warranty` e runner da garantia) | `$match` por dono + `$graphLookup` com `restrictSearchWithMatch`, `max_depth=6` | `orders` |
| Busca de produtos | `agents.py::build_product_pipeline`, `search_products_ranked` (tool `search_catalog`) | `$vectorSearch` índice `products_autoembed_v1` (autoEmbed voyage-4) com filtro de preço/categoria; `$rerank` opcional em `build_product_rerank_pipeline` | `products_catalog` |
| RAG híbrido da KB | `retrieval.py::build_kb_rank_fusion_pipeline`, `build_kb_vector_pipeline`, `build_kb_lexical_pipeline`, `search_kb_explained` (tool `search_knowledge_base`), `_app_rrf` (fallback no app) | `$rankFusion` com perna vetorial (`kb_autoembed_v1`) e `$search` lexical (`kb_lexical_v1`, sinônimos, fuzzy), `$rerank` opcional; `scoreDetails` | `kb_articles` |
| Fusão por score (opcional, fora do padrão) | `retrieval.py::build_score_fusion`, escolhida por `retrieval.fusion.method: "score"` no registry | `$scoreFusion` (8.3+, `minMaxScaler`) com as mesmas duas pernas; medido pior que `$rankFusion` (ADR-006) | `kb_articles` |
| Helper de `$rankFusion`/`$rerank` | `atlas_compat.py` (linhas 111, 181) | monta os estágios; usa `atlas_retrieval` do pov-shared quando instalado | n/a |
| Denylist vetorial do guardrail | `guardrails.py::semantic_denylist` (40), `semantic_denylist_by_clause` (73) | `$vectorSearch` no índice do denylist (`DENYLIST_INDEX`) | `guardrail_denylist` |
| Classificador de escopo | `scope_classifier.py::best_scores` (125, `$vectorSearch` na 130) | `$vectorSearch` índice `scope_probes_vs` | `scope_probes`; config `scope_classifier_config` |
| Classificador de turno pessoal | `turn_classifier.py::classify` (96, `$vectorSearch` na 117) | `$vectorSearch` índice `turn_probes_vs` | `turn_probes`; config `turn_classifier_config` |
| Cascata de cache | `cascade.py::_cascade_lookup_raw` (83; `$vectorSearch` 93 e 102) | `$vectorSearch` em curto prazo `$unionWith` `$vectorSearch` em cache (índices `short_term_autoembed_v1`, `cache_autoembed_v1`) | `short_term_memory`, `semantic_cache` |
| Cascata: fallback sem vetor | `cascade.py::_cascade_lookup_fallback` (156; 161, 164, 168) | `find_one` por `question_norm` | `short_term_memory`, `semantic_cache` |
| Memória longa (contexto) | `cascade.py::cascade_long_term_context` (174; `$vectorSearch` 187, fallback `find_many` 184/195) | `$vectorSearch` índice `long_term_autoembed_v1`, filtro `customer_key` | `long_term_memory` |
| Uso de LLM como série temporal | `usage_ts.py::ensure_collection` (70, `timeseries` na 79), `record_turn` (83), `usage_pipeline` (109), `usage_report` (155) | coleção time series com TTL; agregação por janela | `llm_usage_ts` |
| Change stream de handoffs | `database.py::watch_handoffs` (424), `handoff_pipeline` (460), `_watch_handoffs_atlas` (466) | `watch` com `$match` por `customer_key`; resume token via pov-shared quando disponível | `agent_handoffs` |

**Leituras e escritas (via `DataStore`):**

| O que faz | arquivo::função (linha) | operação | collection |
|---|---|---|---|
| Registro de agentes e regras de roteamento | `orchestration_graph.py::n_ingest`, `n_fanout_check`, `n_chain_prepare`, `main.py::agents`, `update_agent` | `find_many`, `update_one` | `agent_registry`, `routing_rules` (brain) |
| Política do guardrail | `guardrails.py::_load_denylist_and_policy` (324; 327-329) | `find_many`, `find_one` | `guardrail_denylist`, `guardrail_policies` (brain) |
| Eventos do guardrail, candidatas, aprendizado | `guardrails.py::log_event` (346), 198-272; `guardrail_learning.py::learn` (172; 194, 203, 220) | `insert_one`, `replace_one` upsert | `guardrail_events`, `guardrail_candidates`, `guardrail_denylist` |
| Memória de fatos do cliente | `memory.py::extract_and_store` (142; 196, 208, 210), `_active_docs` (137), `active_budget` (223) | `count`, `insert_one`, `update_one` em transação (`run_in_transaction_with_retry`, `database.py`) | `customer_memory` |
| Memória de curto prazo, cache e episódios | `cascade.py::cascade_store_short_term` (218), `cascade_store_turn` (243), `cascade_store_episode` (199) | `replace_one` (209, 232, 272, 287, 295) | `short_term_memory`, `semantic_cache`, `long_term_memory` |
| Conversa, trace, handoffs | `orchestration.py::_update_conversation` (206), `_persist_trace` (254), `orchestration_graph.py::_record_handoff` | `update_one`, `insert_one` | `agent_conversations`, `agent_traces`, `agent_handoffs` |
| Checkpoint do turno | `orchestration_graph.py::get_graph`, `checkpoint_thread_id` (uma thread por turno); pausa/retomada em `resume_turn` e `continue_thread` | `MongoDBSaver` (langgraph), TTL 24h; `aget_state` na demo de queda | `langgraph_checkpoints`, `langgraph_checkpoint_writes` |
| Quadro do turno | `blackboard.py::open_board`, `append_entry`, `set_status`; leitura `main.py::latest_blackboard` e recurso MCP | `insert_one`, `update_one` com `$push: {entries}` / `$set: {status}`, `find` último por `created_at` | `turn_blackboard` (TTL 24h) |
| Políticas de aprovação humana | `hitl.py::load_policies`; `main.py::hitl_policies`, `update_hitl_policy`; seed em `seed.py::seed` | `find_many({})` filtrado por `active` na aplicação, `update_one({$set: {active}})`, `replace_one` upsert | `hitl_policies` (brain) |
| Revisão pausada (interrupt) | `orchestration_graph.py::n_agent_hop` -> `reviews.py::open_review` (`evidence.kind = "hitl_interrupt"`); `main.py::resolve_pending_review` -> `orchestration_graph.py::resume_turn`; `n_apply_approved` | `find_one` idempotente (por turno) + `insert_one` em transação; claim `update_one`; `applied_at` | `pending_reviews`, `agent_decisions` |
| Agent cards A2A, catálogo de tools, topologia | `main.py::a2a_agent_card`, `a2a_single_agent_card`, `tool_catalog`, `graph_topology`; `mcp_server.py::CoordinationMCPServer` (`exposed`) | `find_many`/`find_one` sem `_id` | `agent_registry` (brain) |
| Orçamento global do turno | `orchestration_graph.py::n_ingest` | `find_one({key: "default"})` | `model_config` (brain) |
| Negócio dos agentes | `agents.py` (linhas na tabela 2.2), `replacement.py::apply_replacement` (88; update 122) | `find_one`, `find_many`, `update_one`, `insert_one` (dentro de transação quando há escrita) | `orders`, `invoices`, `shipments`, `loyalty_accounts`, `redemptions`, `support_tickets`, `warranty_policies`, `products_catalog` |
| Decisões e auditoria | `decisions.py::record_decision` (99; 118, 137), `decision_trail` (149) | `insert_one`, `find_many` | `agent_decisions`, `agent_audit_events` |
| Revisão humana | `reviews.py::open_review` (40; 55, 83), `resolve_review` (111; claim 131, 167, 178), `list_reviews` (102) | `find_one`, `insert_one`, `update_one` (claim atômico) | `pending_reviews`, `agent_handoffs` |
| Busca web governada | `web_search.py::get_config` (106), `_run` (174; cache 188/190, cota 196, 208), `_audit` (122) | `find_one`, `reserve_quota` (`database.py:369`), `update_one`, `insert_one` | `tool_registry` (brain), `web_search_quota`, `web_search_cache`, `external_calls` |
| Login e identidade | `main.py::create_token`, `security.py::current_customer` (35), `security.py::decode_customer_key` (mesma checagem no servidor MCP) | `find_one` | `customers` |
| Telas de leitura | `main.py::latest_conversation`, `memory`, `typed_memory`, `inspector`, `guardrails`, `eval_runs`, `health` | `find_many`, `count` | `agent_conversations`, `agent_traces`, `customer_memory`, `semantic_cache`, `short_term_memory`, `long_term_memory`, `guardrail_*`, `eval_runs`, `agent_registry` |
| Reset da demo de um cliente | `demo_reset.py::reset_customer_memory` (12) | `find_many`, `update_one`, `delete_many` | `customer_memory`, `short_term_memory`, `long_term_memory`, `semantic_cache` |
| Seed, índices e validators | `seed.py::seed` (38), `create_search_indexes` (181; nomes `products_autoembed_v1`, `kb_autoembed_v1`, `kb_lexical_v1`, `memory_autoembed_v1`, `cache_autoembed_v1`, `short_term_autoembed_v1`, `denylist_autoembed_v1`, `long_term_autoembed_v1`), `database.py::create_schema_validators` (513), `create_standard_indexes` (610; TTL de `turn_blackboard` em 24h) | `create_search_index` (vetorial autoEmbed e lexical), `create_index`, `$jsonSchema` | todas |

## 4. Compartilhado × só desta PoV

**Vem de `_shared` (pov-shared), sempre opcional: sem ele o app funciona com equivalentes locais.**

| Módulo | Para que serve aqui | Import exato |
|---|---|---|
| `guardrails` | divisão de cláusulas e pontuação por cláusula (anti-diluição); máscara de PII e validação de saída | `from guardrails import ascore_by_clause as _shared_ascore, split_intents as _shared_split` (`dilution.py:30`); `from guardrails import mask_pii` (`edge_guardrails.py:30`), `validate_output` (42) |
| `atlas_retrieval` | `$rankFusion`/`$rerank` e `ChangeStreamWorker` (resume token dos handoffs) | `import atlas_retrieval as _shared` (`atlas_compat.py:20`); `import atlas_retrieval as ar` (`database.py:477`) |
| `tracing` | tracing distribuído opcional | `from tracing import init_tracing` (`observability.py:49`) |

`_shared/grove_client.py` **não** é usado: o gateway é `backend/app/llm.py` (`LLMGateway`), com a mesma ideia (Grove) mas código próprio; `resilience.py` diz isso explicitamente.

**Variáveis de ambiente (só nomes).** Comuns (vêm de `PoVs/.env.shared` via `_shared/sync_env.py`): `MONGODB_URI`, `GROVE_API_KEY`, `GROVE_ANTHROPIC_BASE_URL`, `GROVE_CHAT_COMPLETIONS_URL`, `GROVE_OPENAI_MODELS`, `LLM_PRICES`, `LLM_BLENDED_PRICES`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, `TAVILY_API_KEY`, `TRACE_SINK`. Locais da PoV: `MONGODB_DB`, `MONGODB_BRAIN_DB`, `MONGODB_TEST_DB`, `MONGODB_TEST_BRAIN_DB` (bancos), `API_HOST`, `API_PORT`, `FRONTEND_PORT`, `CORS_ORIGINS` (portas/CORS), `JWT_SECRET`, `JWT_TTL_MINUTES`, `AUTH_REQUIRED`, `DEMO_TOKEN_ISSUANCE_ENABLED`, `ADMIN_API_KEY`, `RATE_LIMIT_*`, `DEMO_MODE` (sem Atlas), `SHORT_TERM_CACHE_THRESHOLD`, `GLOBAL_CACHE_THRESHOLD`, `LONG_TERM_MEMORY_LIMIT`, `GLOBAL_TURN_TOKEN_BUDGET`, `TURN_DEADLINE_SECONDS`, `WARMUP_*`, `AGENT_TIMEOUT_SECONDS`, `TOOL_TIMEOUT_SECONDS`, `TOOL_BREAKER`, `LOOP_GUARD_REPEATS`, `GUARDRAILS_EDGE`, `CHAOS`, `ALLOW_DEMO_DB_WRITE`. No frontend: `VITE_API_URL`, `VITE_ADMIN_KEY`. A lista completa (com placeholders) está em `.env.example`. A classe que lê tudo é `Settings` em `backend/app/config.py:11`.

**Bancos e coleções.** O banco "brain" tem nome próprio (`multiagent_brain`) de propósito: o comentário em `config.py` explica que o cluster é compartilhado com a PoV de agente único, que também tem um `ai_brain` de schema diferente, e o seed de uma sobrescrevia a outra. Ou seja: o cluster é comum; os bancos desta PoV (`multi_agent_poc`, `multiagent_brain`) são exclusivos dela. Dentro do código não há coleção usada em comum com outra PoV. Testes live usam `*_test`.

**Chamadas externas (sem host/chave).**
- LLM via gateway Grove: `backend/app/llm.py` (`LLMGateway`, validação do endpoint na linha 56). Usos: roteamento (`orchestration.py:123`), tool calling dos especialistas (`llm.py` `complete_tools`, chamado por `agent_loop.py::run_tool_agent`), plano (`planner.py::plan_turn`), consolidação (`consolidate.py::consolidate`), síntese no caminho determinístico (`agents.py:49`), extração de memória (`memory.py:142`), classificador do guardrail (`guardrails.py`), juiz do A/B e do eval (`judge.py::judge`). Spans no padrão OTel GenAI: `chat {modelo}` (`llm.py`), `invoke_agent` e `execute_tool`.
- Voyage: não há chamada em Python; os índices vetoriais são autoEmbed (`model: voyage-4` nos pipelines `$vectorSearch`, ex. `agents.py:673`) e o `$rerank` roda no servidor (`atlas_compat.py:181`).
- Langfuse: `app/langfuse_client.py` (fail-open), uma trace por turno; URL devolvida em `langfuse_trace_url`.
- Tavily: `app/web_search.py::_tavily` (144).

## 5. "Quero mexer em..." (atalhos)

- Texto/visual do chat -> `frontend/src/components/ChatPanel.jsx`, `ChatStage.jsx` e `frontend/src/agentic.css` (telas de coordenação) / `theme.css`.
- Grafo ao vivo e capô -> `frontend/src/graphModel.js` e `mongoOps.js` (puros, testados em `frontend/tests/`), desenhados por `AgentGraph.jsx` e `UnderTheHood.jsx`.
- Cenários de demo (select "demos") e casos do eval -> `DEMO_SCENARIOS` e `EVAL_CASES` em `backend/app/seed_data.py`; depois `backend/sync_demo_scenarios.py`.
- Quem atende o quê (palavras-chave, prioridade) -> `ROUTING_RULES` em `backend/app/seed_data.py:153` (vira dado em `routing_rules`; em produção seria `update_one`) e `backend/app/router.py`.
- Persona/modelo/limite de tokens/tools de um agente -> `AGENTS` em `backend/app/seed_data.py:133`, ou `PATCH /api/admin/agents/{key}` com a chave admin (aba Agentes, modo admin). Orçamento global do turno: `model_config.global_turn_tokens` (seed em `backend/seed.py`).
- Tool nova -> `ToolSpec` em `tools.py::TOOLS` (executor reusa a operação segura de `agents.py`; escrita passa por `_write_gate` e `_needs_approval`) + nome em `allowed_tools` do agente. Leitura aparece sozinha no servidor MCP e no card A2A.
- Política de aprovação humana -> `hitl.py::DEFAULT_POLICIES` (seed) ou `PATCH /api/admin/hitl-policies/{policy_id}`.
- Plano e consolidação -> `planner.py::PLANNER_RULES`, `consolidate.py::CONSOLIDATE_RULES`; regras do especialista em `agent_loop.py::TOOL_RULES`.
- Lógica de um agente -> a função `run_<agente>_agent` correspondente em `backend/app/agents.py` (tabela 2.2).
- Adicionar um agente novo -> tools em `tools.py`, runner determinístico em `agents.py` + entrada em `RUNNERS` (1020), rótulo em `agent_loop.py::AGENT_LABELS` + documento em `agent_registry` e regra em `routing_rules` (seed).
- Handoffs permitidos / limite de saltos -> `MAX_HOPS`, `ALLOWED_REVISITS`, `MAX_VISITS_PER_AGENT`, `FALLBACK_AGENTS` em `backend/app/orchestration.py:67-72` e `orchestration_graph.py::n_agent_hop` (`_handoff_targets`).
- Mudar uma query (busca de produto, KB, cache, memória) -> funções da seção 3; thresholds em `backend/app/config.py` (`short_term_cache_threshold`, `global_cache_threshold`) e rodar `backend/calibrate_thresholds.py` antes.
- Guardrail (regras, denylist, diluição) -> `backend/app/guardrails.py`, `dilution.py`, frases em `seed_data.py`; aprendizado em `guardrail_learning.py`.
- Adicionar uma rota -> `backend/app/main.py` + método em `frontend/src/api.js`.
- Mudar porta -> `API_PORT` e `FRONTEND_PORT` no `.env`; frontend: `frontend/package.json` (script `dev`, 5191) e `VITE_API_URL`.
- Mudar o seed -> `backend/app/seed_data.py` e `backend/seed.py`; reset completo: `backend/scripts/reset_demo.py` (recusa o banco da demo sem `ALLOW_DEMO_DB_WRITE=1`).

## 6. Testes

Backend: `cd backend && pytest -q` (offline, sem rede). Frontend: `cd frontend && npm test` (`node --test tests/*.test.mjs`). Testes com Atlas e LLM reais só com `LIVE=1` (ver README). Caos: `CHAOS=1 python -m pytest tests/test_chaos.py -q`.

| Arquivo (`backend/tests/`) | O que cobre |
|---|---|
| `test_router.py`, `test_scope_classifier.py`, `test_scope_integration.py`, `test_scope_resilience.py`, `test_out_of_scope.py` | Roteamento determinístico e classificador de escopo |
| `test_policies.py`, `test_isolation.py`, `test_cache_isolation.py`, `test_security_config.py` | Filtros de ownership, isolamento entre clientes, configuração de segurança |
| `test_cache_eligibility.py`, `test_personal_turn_cache.py`, `test_turn_classifier.py`, `test_auto_warmup.py` | Quando o cache vale ou não, turno pessoal, aquecimento |
| `test_memory_extractor.py`, `test_fuzz_memory.py`, `test_migrate_legacy_memory.py` | Extração de fatos, supersessão, migração |
| `test_retrieval.py`, `test_kb_retrieval_b1.py`, `test_product_rerank.py` | RAG híbrido e rerank |
| `test_graph.py`, `test_reviews.py`, `test_decisions.py`, `test_budget_policy.py` | Cadeia de trocas, revisão humana, auditoria, orçamento |
| `test_guardrail_band.py`, `adversarial/test_guardrail_dilution_adversarial.py`, `adversarial/test_pii_mask_adversarial.py`, `adversarial/test_learning_poison_adversarial.py`, `adversarial/test_inter_agent_injection_adversarial.py` | Guardrail em duas faixas, diluição, PII, envenenamento do aprendizado, injeção entre agentes |
| `adversarial/test_checkpoint_isolation_adversarial.py`, `adversarial/test_http_hostile_adversarial.py`, `adversarial/test_reset_guard_adversarial.py`, `adversarial/test_double_click_live_adversarial.py`, `adversarial/test_grove_resilience_adversarial.py` | Checkpoint, HTTP hostil, trava do reset, duplo clique, resiliência do gateway |
| `test_live_events.py`, `test_handoff_stream.py`, `test_concurrency.py`, `test_limits.py` | Eventos ao vivo, stream de handoffs, concorrência, limites |
| `test_chaos.py`, `test_resilience_e2e.py`, `test_economics_gateway.py`, `test_usage_ts.py` | Falhas injetadas, resiliência, custo e série temporal |
| `test_situations_offline.py`, `test_eval_situations_contract.py` | Contrato do eval de situações (`tests/data/situations.json`) |
| `test_agentic_core.py` | Tool calling, portões de escrita, plano, quadro, consolidação, HITL com interrupt/retomada, crash/resume, A/B |
| `test_mcp_and_cards.py`, `test_otel_genai.py` | Servidor MCP (identidade, só leitura, filtro do registry), agent cards A2A, spans GenAI |
| `test_eval_answers.py`, `test_seed_dates.py` | Harness do eval de resposta; datas do seed relativas ao dia |
| `test_live*.py` | Contra Atlas e LLM reais (só `LIVE=1`) |

Avaliações (não são pytest): `python eval.py` (golden dataset por rota e trajetória, grava em `eval_runs`), `eval_answers.py` (resposta: juiz, efeito, pass^k), `eval_routing.py`, `eval_situations.py`, `eval_kb_retrieval.py`, `eval_product_rerank.py`; formato em `eval/FORMAT.md`.

## Observações sobre código sem uso na UI

- `frontend/src/components/Inspector.jsx`, `api.inspector` e a rota `GET /api/inspector/{view}` (`main.py:357`, collections em `INSPECTOR_COLLECTIONS`, 348) existem, mas `App.jsx` não importa o `Inspector`; hoje só a API responde.
- A aprovação de candidato de guardrail não tem botão: `GET /api/guardrails/candidates` remove o `_id`, que `POST /api/admin/guardrails/candidates/{id}/approve` exige.
- `GET /api/economics` (`main.py:515`) é usado pela aba Métricas (`CostPanel.jsx::EconomicsPanel`); o custo por turno vem no `run.economics` do próprio resultado.
