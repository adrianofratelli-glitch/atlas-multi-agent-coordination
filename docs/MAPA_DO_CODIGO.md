# Mapa do código — atlas-multi-agent-coordination

> Também conhecida como: **Multi-Agent** (pasta antiga `multiagente-atendimento`). Portas: frontend 5191 / backend 8031.
> Em uma frase: um atendimento ao cliente com 8 agentes (1 orquestrador + 7 especialistas) em que o MongoDB Atlas é também o plano de coordenação: registro de agentes, regras de roteamento, handoffs, memória, cache, decisões e checkpoints do turno são documentos.

Contexto geral nos briefings: `docs/briefing/architecture.md`, `docs/briefing/queries.md`, `docs/briefing/agent-behavior.md`, `docs/briefing/ui-flows.md`. Este mapa não os repete; ele diz onde cada coisa está no código.

## Como ler este código em 30 segundos

- **Quem decide o quê (agentes):** `backend/app/agents.py` tem um "runner" por agente. O dicionário `RUNNERS` (linha 952) liga a chave do agente à função.
- **Quem encadeia os agentes (nós do grafo):** `backend/app/orchestration_graph.py` (LangGraph). O nó `n_handoff_chain` (linha 444) chama o runner, lê `result.handoff_to` e passa para o próximo agente, até 5 saltos (`MAX_HOPS`, `orchestration.py:67`).
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
  migrate_legacy_memory.py      Migração de memória antiga para o formato atual
  scripts/                      reset_demo.py (reset completo), capabilities.py (checa o cluster, só leitura), chaos_suite.py, crash_resume.py, isolation.py, trace_query.py
  app/
    main.py                     Todas as rotas /api/*, middlewares, auth, SSE, lifespan
    orchestration_graph.py      O turno como grafo LangGraph: um nó por etapa (ver seção 2.1)
    orchestration.py            OrchestrationService: roteamento por LLM, fan-out, gravar conversa e trace
    router.py                   Roteamento determinístico (cheap_route, detect_fanout, deterministic_orchestrator)
    agents.py                   Os 7 runners (order, billing, warranty, loyalty, logistics, product, support) + busca de produtos e KB
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
  src/App.jsx                   Tela inteira: abas Chat / Decisões / Métricas, chat, painel de agentes, métricas
  src/api.js                    Todas as chamadas HTTP e o stream SSE (URL base: VITE_API_URL, padrão 127.0.0.1:8031)
  src/components/Timeline.jsx   Linha do tempo do turno (cada evento mostra agente, collection, operação)
  src/components/QueryDetails.jsx   Detalhe da consulta MongoDB de um evento
  src/components/ReplacementChain.jsx   Desenha a cadeia de trocas do $graphLookup
  src/components/CompliancePage.jsx     Aba "Decisões": trilha de auditoria e fila de revisão humana
  src/components/Inspector.jsx  Painel lateral "inspetor MongoDB" (não importado em nenhum lugar hoje; ver Dúvidas)
  src/execution.js, kbExplain.js, richtext.js   Funções puras de apoio
  tests/*.test.mjs              Testes JS (node --test)
eval/                           Datasets: routing_dataset.json, kb_retrieval.json, product_retrieval.json, FORMAT.md
docs/                           adr/ (10 decisões), briefing/, architecture.md, chaos-report.md, agent-quality-economics.md
start.sh                        Sobe backend e frontend
```

## 2. "Apertei o botão X, o que roda?"

Todas as chamadas HTTP do navegador saem de `frontend/src/api.js` (objeto `api`, linha 32). A identidade (Ana, Bruno, Carla, Diego, `IDENTITIES` em `App.jsx:13`) vira JWT em `POST /api/auth/token`; o `customer_key` só existe no token.

| Tela / botão | Componente | Chamada HTTP | Handler | Função de negócio | MongoDB |
|---|---|---|---|---|---|
| Abrir o app | `App.jsx:306` (useEffect) -> `api.warmup()` + `switchIdentity` (282) | `POST /api/warmup`, `POST /api/auth/token`, `GET /api/health`, `/api/agents`, `/api/memory/{key}`, `/api/guardrails/events`, `/api/metrics`, `/api/conversations/latest`, `/api/demo-scenarios` | `main.py:241`, `139`, `528`, `266`, `337`, `388`, `500`, `286`, `272` | `warmup.WarmupService.run` (`warmup.py:62`); `security.issue_token` | `customers` find_one (`main.py:142`); `agent_registry` (brain) find; `customer_memory`; `guardrail_events`; `agent_conversations`, `agent_traces` |
| Seletor "identidade" | `App.jsx:416` `switchIdentity` | idem acima | idem | idem | idem |
| Chat: enviar (Enter / botão) e chips de sugestão | `App.jsx:377` `send` (chips: `ChatPanel` 89, `onSuggestion`) | `POST /api/chat/stream` (SSE, `api.js:chatStream`; cai para `POST /api/chat`) | `main.py:195` `chat_stream` / `main.py:188` `chat` -> `_run_chat` (160) | `orchestration_graph.py:697` `run_turn` (o grafo, ver 2.1) | ver 2.1 |
| Chat: select "roteiro rápido" (cenários de demo) | `App.jsx:126-136` (só preenche a caixa de texto com `demo.message`; quem envia é o botão enviar) | nenhuma ao escolher; a lista veio de `GET /api/demo-scenarios` | `main.py:272` `demo_scenarios` | lê os cenários semeados de `seed_data.DEMO_SCENARIOS` | `find_many` em `demo_scenarios` (`main.py:274`) |
| Chat: sugestões de próximos passos | `App.jsx` (resposta do turno traz `suggestions`) | no mesmo turno | `orchestration.py::_next_steps` (104) / `orchestration_graph.py:357` | monta passos pelo que ainda não foi coberto | `find_many` em pedidos/faturas (`guidance.py:37-60`) |
| "Nova conversa" | `App.jsx:359` `newConversation` | nenhuma | n/a | novo `conversation_id` no navegador | nenhum |
| "Reiniciar memória da demo" | `App.jsx:365` `resetDemoMemory` | `POST /api/demo/reset` | `main.py:250` `demo_reset` | `demo_reset.py:12` `reset_customer_memory` | `customer_memory` (find_many, update_one, delete_many), `short_term_memory`, `long_term_memory`, `semantic_cache` (`delete_many`, `demo_reset.py:22-28`) |
| Pílula "ao vivo" (Change Stream) | `App.jsx:319` `connect` -> `api.streamEvents` | `GET /api/events/stream` (SSE) | `main.py:485` `events_stream` -> `_handoff_event_stream` (444) | `database.py:466` `_watch_handoffs_atlas` | `watch` em `agent_handoffs`, filtrado por `customer_key` (`database.py:460`) |
| Timeline de um turno (clicar em um evento) | `Timeline.jsx`, `QueryDetails.jsx` | nenhuma (dados já vieram no turno) | n/a | n/a | n/a |
| Cadeia de trocas | `ReplacementChain.jsx`, usado em `App.jsx` | nenhuma (vem no turno) | n/a | `graph.py:17` `build_order_chain_pipeline` | `$graphLookup` em `orders` |
| Chat: handoffs da conversa da conversa | `App.jsx` (carrega depois do turno) | `GET /api/handoffs?conversation_id=` | `main.py:304` `handoffs` | leitura direta | `agent_handoffs` find_many (`main.py:308`) |
| Aba "Métricas" | `App.jsx:173` `MetricsPage` | `GET /api/metrics`; com modo admin `GET /api/eval/runs` | `main.py:500`, `601` | `metrics`; economia por turno vem no próprio resultado | `eval_runs` find_many (`main.py:603`); contadores em memória |
| Liga/desliga agente (`AgentsPage`, hoje NÃO renderizada na UI; só via API) | `App.jsx:141` `AgentsPage`, `toggle` (142) | `PATCH /api/admin/agents/{agent_key}` (precisa `X-Admin-Key`) | `main.py:546` `update_agent` | atualiza o agente | `agent_registry` update_one (brain) (`main.py:550`) + `admin_audit` insert_one (553) |
| Aba "Decisões": trilha de auditoria | `CompliancePage.jsx:198` `loadTrail` | `GET /api/decisions` | `main.py:313` `decisions` | `decisions.py:149` `decision_trail` | `agent_decisions`, `agent_audit_events` find_many (`decisions.py:155-156`) |
| Aba "Decisões": fila de revisão humana (modo admin) e botões de resolver | `CompliancePage.jsx:30` (lista), `45`/`118` (resolver) | `GET /api/admin/reviews`, `POST /api/admin/reviews/{id}/resolve`; cliente vê as suas em `GET /api/reviews` | `main.py:558`, `568`, `327` | `reviews.py:111` `resolve_review` | `pending_reviews` (find_one, `update_one` com claim `reviews.py:131`), `agent_decisions`/`agent_audit_events` insert, `agent_handoffs` insert (`reviews.py:178`), `admin_audit` |
| Caixa "modo admin" (aba Decisões) | `CompliancePage.jsx:222` | nenhuma (guarda a chave admin do `.env` do frontend) | n/a | n/a | n/a |
| Candidatas do guardrail (sem botão na UI; só API) | `api.js` não expõe | `GET /api/guardrails/candidates`, `POST /api/admin/guardrails/candidates/{id}/approve` | `main.py:388`, `589` | `guardrail_learning.py` | `guardrail_candidates`, `guardrail_denylist` (`replace_one` upsert, `main.py:594`) |

### 2.1 O turno, nó por nó (`orchestration_graph.py`, `_build_graph` linha 632)

Ordem: `ingest -> guardrail -> (blocked | memory_extract) -> fanout_check -> (fanout | decide) -> (out_of_scope | cache_lookup) -> (cache_hit | handoff_chain) -> END`.

| Nó (linha) | Quem faz o quê | Onde toca o MongoDB |
|---|---|---|
| `n_ingest` (113) | Carrega o contexto em paralelo: agentes ativos, regras, memória do cliente, conversa, config da web | `agent_registry` e `routing_rules` (brain), `customer_memory`, `agent_conversations` (`find_many`/`find_one`, linhas 122-125); `web_search.get_config` -> `tool_registry` |
| `n_guardrail` (181) | `guardrails.check_input` (`guardrails.py:147`): denylist estático, `$vectorSearch` no denylist (`semantic_denylist`, linha 40; por cláusula, `dilution.py`), classificador LLM só se necessário; ao bloquear, `guardrail_learning.learn` (`guardrail_learning.py:172`) | `$vectorSearch` em `guardrail_denylist`; `find_many` do denylist e `guardrail_policies` (`guardrails.py:327-329`); `insert_one` em `guardrail_events`/`guardrail_candidates` (`guardrails.py:198-272`) |
| `n_blocked` (207) | Resposta de bloqueio e registro | `guardrails.log_event` (`guardrails.py:346`) |
| `n_memory_extract` (232) | `memory.extract_and_store` (`memory.py:142`): LLM extrai fatos; dedup e supersessão em transação | `customer_memory` `find_many`, `insert_one` + `update_one` na transação (`memory.py:208-210`) |
| `n_fanout_check` / `n_fanout` (245 / 258) | `router.detect_fanout` (63): pergunta composta pedido + fatura roda os dois agentes em paralelo (`orchestration.py:150` `_run_fanout`) | `routing_rules` (`find_many`, 247) |
| `n_decide` (266) | Roteamento: `router.cheap_route` (por keyword/prioridade), `scope_classifier.classify` (`scope_classifier.py:150`), `_route_with_llm` (`orchestration.py:123`) só quando não há sinal de regra | `$vectorSearch` em `scope_probes` (`scope_classifier.py:129`); config em `scope_classifier_config` |
| `n_out_of_scope` (318) | Fora de escopo decisivo: orientação fixa, 0 tokens, sem agente | `find_many` de pedidos para a orientação (`guidance.py`) |
| `n_cache_lookup` (365) | `turn_classifier.classify` (turno pessoal?) e `cascade.cascade_lookup` (`cascade.py:57`) | `$vectorSearch` em `turn_probes`; `short_term_memory` + `$unionWith` `semantic_cache` (`cascade.py:93-114`); fallback exato `cascade.py:161-168` |
| `n_cache_hit` (395) | Devolve a resposta em cache, sem LLM; grava curto prazo | `cascade_store_short_term` (`cascade.py:218`, `replace_one`) |
| `n_handoff_chain` (444) | Para cada salto: `RUNNERS[agente]` (`agents.py:952`); se o runner pede handoff, grava e segue; fim: `guardrails.check_output`, grava cache/episódio, conversa, trace | `agent_registry` find_one (506); `agent_handoffs` `insert_one` (567); `cascade_long_term_context` (`cascade.py:174`, `$vectorSearch` em `long_term_memory`); `cascade_store_turn` (`cascade.py:243`), `cascade_store_episode` (199); `_update_conversation` (`orchestration.py:206`, `update_one` em `agent_conversations`); `_persist_trace` (`orchestration.py:252`, `insert_one` em `agent_traces`); `usage_ts.record_turn` (`llm_usage_ts`, série temporal) |

### 2.2 Qual agente faz o quê (`backend/app/agents.py`)

| Agente (chave) | Runner (linha) | Faz | Escreve? | Collections | Pode passar a vez para |
|---|---|---|---|---|---|
| Orquestrador (`orchestrator`) | não é runner: `router.py` (`cheap_route` 20, `deterministic_orchestrator` 166) e `orchestration.py:123` `_route_with_llm` | Classifica e roteia; nunca responde ao cliente | Não | `routing_rules`, `agent_registry` (brain) | n/a |
| Pedidos (`order_agent`) | `run_order_agent` (107) | Status, troca, reembolso do pedido do titular | Sim: status do pedido (lista fechada de valores) e troca via `replacement.apply_replacement` (88) | `orders`; leitura `find_one`/`find_many` (114/121); `update_one` (187) | `billing_agent`, `logistics_agent` |
| Produtos (`product_agent`) | `run_product_agent` (745); busca em `search_products_ranked` (701) | Recomenda produtos só com resultado do catálogo; orçamento vem da memória | Não | `products_catalog`: `$vectorSearch` + `$rerank` opcional (`build_product_pipeline` 663, `build_product_rerank_pipeline` 686) | `order_agent` (confirmar troca, linha 819) |
| Suporte (`support_agent`) | `run_support_agent` (875); `search_kb` (823) | Dúvida técnica com evidência da base de conhecimento; abre chamado humano | Sim: `support_tickets` (idempotente por `ticket_id`) | `kb_articles` via `retrieval.search_kb_explained`; `support_tickets` (918-935) | transfere quando necessário |
| Cobrança (`billing_agent`) | `run_billing_agent` (231) | Explica fatura do titular | Não | `invoices` (238/241) | n/a |
| Garantia (`warranty_agent`) | `run_warranty_agent` (284) | Cobertura por categoria e data de compra | Não | `orders`, `products_catalog`, `warranty_policies` (291-304) | `product_agent` (371) |
| Fidelidade (`loyalty_agent`) | `run_loyalty_agent` (387) | Saldo, tier, resgate de pontos | Sim, em transação: debita pontos e grava `redemptions` | `loyalty_accounts` (390, 477), `redemptions` (472, 481) | `product_agent` (514) |
| Logística (`logistics_agent`) | `run_logistics_agent` (518) | Transportadora, rastreio, previsão; reagendar entrega; busca web da transportadora | Sim: `reschedule_requested` em `shipments` (560) | `shipments` (525/528); `web_search.search` (584) | `order_agent` (572, 607) |

Regras de segurança que valem para todos: os filtros de leitura/escrita são reconstruídos em `policies.py` (`safe_order_read_filter` 29, `safe_order_update` 41, `safe_invoice_filter` 69, `safe_shipment_filter` 76); o LLM só redige a frase final (`agents.py:49` `llm_synthesize`) sobre o documento já buscado.

## 3. Onde estão as queries MongoDB

Banco de negócio: `multi_agent_poc`; banco de configuração ("brain"): `multiagent_brain` (`config.py`). Métodos `brain=True` do `DataStore` apontam para o segundo. Visão geral por texto em `docs/briefing/queries.md`.

**Pipelines (aggregate / Search):**

| O que faz | arquivo::função (linha) | operador / pipeline principal | collection |
|---|---|---|---|
| Cadeia de trocas | `graph.py::build_order_chain_pipeline` (17), operador na 26; chamada em `agents.py::order_replacement_chain` (272, linha 276) | `$match` por dono + `$graphLookup` com `restrictSearchWithMatch`, `max_depth=6` | `orders` |
| Busca de produtos | `agents.py::build_product_pipeline` (663), `search_products_ranked` (701; execução 714/726) | `$vectorSearch` índice `products_autoembed_v1` (autoEmbed voyage-4) com filtro de preço/categoria; `$rerank` opcional em `build_product_rerank_pipeline` (686) | `products_catalog` |
| RAG híbrido da KB | `retrieval.py::build_kb_rank_fusion_pipeline` (124), `build_kb_vector_pipeline` (78), `build_kb_lexical_pipeline` (97), `search_kb_explained` (187; execução 207), `_app_rrf` (290; fallback no app, 293) | `$rankFusion` com perna vetorial (`kb_autoembed_v1`) e `$search` lexical (`kb_lexical_v1`, sinônimos, fuzzy), `$rerank` opcional; `scoreDetails` | `kb_articles` |
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
| Registro de agentes e regras de roteamento | `orchestration_graph.py::n_ingest` (122-123), `n_decide` (247), `main.py::agents` (267), `update_agent` (550) | `find_many`, `update_one` | `agent_registry`, `routing_rules` (brain) |
| Política do guardrail | `guardrails.py::_load_denylist_and_policy` (324; 327-329) | `find_many`, `find_one` | `guardrail_denylist`, `guardrail_policies` (brain) |
| Eventos do guardrail, candidatas, aprendizado | `guardrails.py::log_event` (346), 198-272; `guardrail_learning.py::learn` (172; 194, 203, 220) | `insert_one`, `replace_one` upsert | `guardrail_events`, `guardrail_candidates`, `guardrail_denylist` |
| Memória de fatos do cliente | `memory.py::extract_and_store` (142; 196, 208, 210), `_active_docs` (137), `active_budget` (223) | `count`, `insert_one`, `update_one` em transação (`run_in_transaction_with_retry`, `database.py`) | `customer_memory` |
| Memória de curto prazo, cache e episódios | `cascade.py::cascade_store_short_term` (218), `cascade_store_turn` (243), `cascade_store_episode` (199) | `replace_one` (209, 232, 272, 287, 295) | `short_term_memory`, `semantic_cache`, `long_term_memory` |
| Conversa, trace, handoffs | `orchestration.py::_update_conversation` (206; update 245), `_persist_trace` (252; insert 253), `orchestration_graph.py` (567) | `update_one`, `insert_one` | `agent_conversations`, `agent_traces`, `agent_handoffs` |
| Checkpoint do turno | `orchestration_graph.py::get_graph` (686), `checkpoint_thread_id` (693) | `MongoDBSaver` (langgraph), TTL 24h | `langgraph_checkpoints` |
| Negócio dos agentes | `agents.py` (linhas na tabela 2.2), `replacement.py::apply_replacement` (88; update 122) | `find_one`, `find_many`, `update_one`, `insert_one` (dentro de transação quando há escrita) | `orders`, `invoices`, `shipments`, `loyalty_accounts`, `redemptions`, `support_tickets`, `warranty_policies`, `products_catalog` |
| Decisões e auditoria | `decisions.py::record_decision` (99; 118, 137), `decision_trail` (149) | `insert_one`, `find_many` | `agent_decisions`, `agent_audit_events` |
| Revisão humana | `reviews.py::open_review` (40; 55, 83), `resolve_review` (111; claim 131, 167, 178), `list_reviews` (102) | `find_one`, `insert_one`, `update_one` (claim atômico) | `pending_reviews`, `agent_handoffs` |
| Busca web governada | `web_search.py::get_config` (106), `_run` (174; cache 188/190, cota 196, 208), `_audit` (122) | `find_one`, `reserve_quota` (`database.py:369`), `update_one`, `insert_one` | `tool_registry` (brain), `web_search_quota`, `web_search_cache`, `external_calls` |
| Login e identidade | `main.py::create_token` (139; 142), `security.py::current_customer` (27; 35, 48) | `find_one` | `customers` |
| Telas de leitura | `main.py::latest_conversation` (286), `memory` (337), `inspector` (356), `guardrails` (388), `eval_runs` (601), `health` (528) | `find_many`, `count` | `agent_conversations`, `agent_traces`, `customer_memory`, `semantic_cache`, `short_term_memory`, `long_term_memory`, `guardrail_*`, `eval_runs`, `agent_registry` |
| Reset da demo de um cliente | `demo_reset.py::reset_customer_memory` (12) | `find_many`, `update_one`, `delete_many` | `customer_memory`, `short_term_memory`, `long_term_memory`, `semantic_cache` |
| Seed, índices e validators | `seed.py::seed` (38), `create_search_indexes` (176; nomes `products_autoembed_v1`, `kb_autoembed_v1`, `kb_lexical_v1`, `memory_autoembed_v1`, `cache_autoembed_v1`, `short_term_autoembed_v1`, `denylist_autoembed_v1`, `long_term_autoembed_v1`), `database.py::create_schema_validators` (513), `create_standard_indexes` (610; TTL na 672) | `create_search_index` (vetorial autoEmbed e lexical), `create_index`, `$jsonSchema` | todas |

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
- LLM via gateway Grove: `backend/app/llm.py` (`LLMGateway`, validação do endpoint na linha 56). Usos: roteamento (`orchestration.py:123`), síntese (`agents.py:49`), extração de memória (`memory.py:142`), classificador do guardrail (`guardrails.py`).
- Voyage: não há chamada em Python; os índices vetoriais são autoEmbed (`model: voyage-4` nos pipelines `$vectorSearch`, ex. `agents.py:673`) e o `$rerank` roda no servidor (`atlas_compat.py:181`).
- Langfuse: `app/langfuse_client.py` (fail-open), uma trace por turno; URL devolvida em `langfuse_trace_url`.
- Tavily: `app/web_search.py::_tavily` (144).

## 5. "Quero mexer em..." (atalhos)

- Texto/visual do chat -> `frontend/src/App.jsx` (`ChatPanel`, linha 89) e `frontend/src/theme.css`.
- Cenários de demo (select "demos") e casos do eval -> `DEMO_SCENARIOS` e `EVAL_CASES` em `backend/app/seed_data.py`; depois `backend/sync_demo_scenarios.py`.
- Quem atende o quê (palavras-chave, prioridade) -> `ROUTING_RULES` em `backend/app/seed_data.py:133` (vira dado em `routing_rules`; em produção seria `update_one`) e `backend/app/router.py`.
- Persona/modelo/limite de tokens de um agente -> `AGENTS` em `backend/app/seed_data.py:113` (linhas 114-130), ou `PATCH /api/admin/agents/{key}` com a chave admin (não há botão ativo na UI).
- Lógica de um agente -> a função `run_<agente>_agent` correspondente em `backend/app/agents.py` (tabela 2.2).
- Adicionar um agente novo -> runner em `agents.py` + entrada em `RUNNERS` (952) + documento em `agent_registry` e regra em `routing_rules` (seed).
- Handoffs permitidos / limite de saltos -> `MAX_HOPS`, `ALLOWED_REVISITS`, `MAX_VISITS_PER_AGENT`, `FALLBACK_AGENTS` em `backend/app/orchestration.py:67-72` e `n_handoff_chain` (`orchestration_graph.py:444`).
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
| `test_live*.py` | Contra Atlas e LLM reais (só `LIVE=1`) |

Avaliações (não são pytest): `python eval.py` (golden dataset, grava em `eval_runs`), `eval_routing.py`, `eval_situations.py`, `eval_kb_retrieval.py`, `eval_product_rerank.py`; formato em `eval/FORMAT.md`.

## Observações sobre código sem uso na UI

- `frontend/src/components/Inspector.jsx`, `api.inspector` e a rota `GET /api/inspector/{view}` (`main.py:356`, collections em `INSPECTOR_COLLECTIONS`, 347) existem, mas `App.jsx` não importa o `Inspector`; hoje só a API responde.
- `AgentsPage` e `DataPage` estão definidas em `App.jsx` (141, 169) e não são renderizadas; por isso o liga/desliga de agente só funciona por `PATCH /api/admin/agents/{agent_key}`.
- `GET /api/economics` (`main.py:512`) não é chamado pelo frontend; o custo por turno vem no `run.economics` do próprio resultado.
