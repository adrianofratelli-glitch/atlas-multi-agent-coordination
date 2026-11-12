# Arquitetura — atlas-multi-agent-coordination

> Para "onde está a query X" veja `queries.md`. Para "como o roteamento, o tool calling, o quadro do turno, a aprovação humana e o checkpoint funcionam" veja `agent-behavior.md`. Para telas veja `ui-flows.md`. Este arquivo é a visão geral e o "porquê" das decisões estruturais.

## O que é

PoV de atendimento ao cliente multiagente onde o **MongoDB Atlas é tanto o data plane quanto o plano de coordenação**: regras de roteamento, estado dos agentes, tools liberadas por agente, políticas de aprovação humana, handoffs, quadro do turno, memória (curto e longo prazo), cache semântico e decisões de guardrail vivem todos em documentos MongoDB, consultáveis como qualquer outro dado operacional — não numa fila/engine de workflow separada.

8 agentes reais registrados em `agent_registry`: `orchestrator`, `order_agent`, `product_agent`, `support_agent`, `billing_agent`, `warranty_agent`, `loyalty_agent`, `logistics_agent`.

**Orquestração**: o turno é um `StateGraph` do LangGraph (`backend/app/orchestration_graph.py`, migrado em 2026-09-29) com `MongoDBSaver` como checkpointer (`langgraph_checkpoints`, `thread_id = <customer_key>:<conversation_id>:<turno>` — uma thread por turno —, TTL 24h). Desde 2026-11-12 (ADR-011/012) os especialistas fazem **tool calling real** sobre tools tipadas que o registry libera; em pedido composto o orquestrador faz um **plano**; cada hop é um super-step com checkpoint e uma entrada no **quadro do turno** (`turn_blackboard`); o orquestrador **consolida** uma resposta única; escrita sensível **pausa com `interrupt()`** até a decisão humana. Detalhes em `agent-behavior.md`.

## Stack

- **Frontend**: React + Vite (`frontend/src`), porta 5191 (estrita, sem fallback).
- **Backend**: FastAPI (`backend/app/main.py`), porta 8031 (estrita, sem fallback), driver `pymongo` assíncrono (`AsyncMongoClient`).
- **LLM**: Anthropic via gateway (`backend/app/llm.py`, `complete` e `complete_tools`), usado para (a) classificação de intenção quando a regra determinística empata, (b) tool calling dos especialistas, (c) plano e consolidação do orquestrador, (d) síntese no caminho determinístico, (e) classificador de guardrail semântico, extrator de memória e juiz do A/B/eval (`judge.py`). Classificações rodam com `temperature=0`.
- **Dados/coordenação**: MongoDB Atlas — dois bancos lógicos, `multi_agent_poc` (dados de negócio + estado operacional, inclusive `turn_blackboard`) e `multiagent_brain` (configuração: `agent_registry`, `routing_rules`, `guardrail_policies`, `hitl_policies`, `model_config`, `tool_registry`).
- **Busca**: Atlas Vector Search com Automated Embedding (`voyage-4`), Atlas Search (BM25/lexical), `$rankFusion` (MongoDB 8.0+, doc oficial; cluster em 9.0.4) para híbrido server-side; `$scoreFusion` (8.3+) disponível como dado (`retrieval.fusion.method: "score"`), medido pior neste KB e fora do padrão (ADR-006).
- **Interoperabilidade**: agent cards A2A 1.0 gerados do registry (`app/agent_cards.py`), servidor MCP somente leitura por stdio (`backend/mcp_server.py`), spans OpenTelemetry no padrão GenAI (ADR-013).
- **Observabilidade**: Langfuse self-host (uma trace por turno), Change Streams para live feed de handoffs, métricas in-process (`backend/app/metrics.py`).
- **Modo sem Atlas**: `DEMO_MODE=1` troca `DataStore` para um backend em memória (`backend/app/database.py`) com os mesmos contratos — usado por testes/CI, sem Vector Search/Change Streams reais.

## Componentes principais

| Arquivo | Responsabilidade |
|---|---|
| `backend/app/main.py` | Rotas FastAPI, auth JWT, middleware, SSE de eventos |
| `backend/app/orchestration_graph.py` | O turno como `StateGraph`: nós, `run_turn`, `resume_turn` (aprovação humana), `continue_thread` / `resume_after_crash` |
| `backend/app/orchestration.py` | `OrchestrationService` — limites da cadeia (`MAX_HOPS`, revisitas), fan-out, roteamento por LLM, conversa e trace |
| `backend/app/tools.py` | Tools tipadas dos especialistas, executores com filtro de dono e os três portões de escrita |
| `backend/app/agent_loop.py` | Loop de tool calling de um especialista (`run_tool_agent`, `transfer_to_agent`) |
| `backend/app/planner.py` / `consolidate.py` | Plano do turno pelo orquestrador / resposta única a partir do quadro |
| `backend/app/blackboard.py` | Quadro do turno (`turn_blackboard`) e o resumo só-de-fatos que vai ao próximo agente |
| `backend/app/hitl.py` | Políticas de aprovação humana (`hitl_policies`) |
| `backend/app/compare.py` / `judge.py` | A/B agente único × time na mesma pergunta / juiz LLM (completude, coerência, grounding) |
| `backend/app/agent_cards.py` / `backend/mcp_server.py` | Agent cards A2A / servidor MCP somente leitura |
| `backend/app/router.py` | Roteamento determinístico por keyword (`cheap_route`), detecção de fan-out, orquestrador fallback |
| `backend/app/agents.py` | Os 7 runners determinísticos (`RUNNERS`, fallback sem modelo) e as operações de negócio que as tools reusam |
| `backend/app/graph.py` | `$graphLookup` sobre `orders` — cadeia de reposição/troca |
| `backend/app/cascade.py` | Cascata de cache semântico (curto prazo → cache global) via `$vectorSearch`/`$unionWith` |
| `backend/app/memory.py` | Extrator LLM de fatos do cliente + dedup, supersessão e `looks_like_instruction` (`customer_memory`) |
| `backend/app/scope_classifier.py` | Classificador de escopo por embedding (`in`/`out`/`chat`, margem, faixa ambígua → LLM) — ADR-004 |
| `backend/eval_situations.py` / `generate_situations.py` | Mede o agente real em 287 situações geradas por LLM (dev vs holdout); `tests/data/situations.json` |
| `backend/app/turn_classifier.py` | Classificador vetorial "este turno depende da memória deste cliente?" (`<brain>.turn_probes`) |
| `backend/app/warmup.py` | Aquecimento automático do cache semântico (no start e quando a UI abre) |
| `backend/app/demo_reset.py` | Desfaz o que a demo gravou num cliente e reativa o que ela substituiu |
| `backend/scripts/reset_demo.py` | Reset completo da demo num comando (seed + probes + escritas de ensaio + checkpoints órfãos + espera dos índices); recusa o banco da demo sem `ALLOW_DEMO_DB_WRITE=1` |
| `backend/app/dilution.py` | Anti-diluição do guardrail de entrada: pontua mensagem inteira e cláusulas, vale o maior score |
| `backend/app/retrieval.py` | Pipelines de busca híbrida (`kb_articles`) — vetor, lexical, `$rankFusion` |
| `backend/app/guardrails.py` | 3 camadas de guardrail: denylist estático, denylist vetorial, classificador LLM |
| `backend/app/database.py` | `DataStore` — abstração Atlas real vs. in-memory; índices e validators |
| `backend/app/decisions.py` / `reviews.py` | Trilha de auditoria (`agent_decisions`) e fila de revisão humana (`pending_reviews`) |
| `backend/app/langfuse_client.py` | Uma trace Langfuse por turno inteiro (roteamento, cada hop, handoff, guardrail) |

## Fluxo de dados de um turno (visão de 10.000 pés)

1. **Entrada** — `POST /api/chat`, JWT decodifica `customer_key` (nunca vem do payload).
2. **Guardrail de entrada** — denylist estático → denylist vetorial (Atlas Vector Search) → LLM classificador (só se necessário). O que o classificador bloqueia vira regra só no trecho malicioso, em quarentena por cliente com TTL de 7 dias; global só com 3 clientes distintos ou aprovação (`guardrail_learning.py`).
3. **Escopo** — embedding decide `in`/`out`/`chat` (ADR-004); `out` decisivo termina numa orientação determinística: sem agente, sem LLM, **0 tokens**, marcado como `out_of_scope` na timeline; a faixa ambígua vai ao LLM de roteamento. Sem veredito real, vale a lista de palavras (ADR-003).
4. **Extração de memória** — fatos em 3ª pessoa extraídos por LLM (com `max_price_brl` estruturado para orçamento), deduplicados e gravados com supersessão transacional em `customer_memory`; falha fechada, nunca derruba o turno (ADR-002).
5. **Roteamento** — regra determinística por keyword (`cheap_route`) prioritária; LLM só decide quando não há sinal de regra nenhum. Fan-out paralelo (`order_agent` + `billing_agent`) para perguntas compostas genuinamente independentes.
6. **Cascata de cache semântico** — `$vectorSearch` em `short_term_memory` unido (`$unionWith`) com `semantic_cache`; HIT retorna sem chamar LLM nenhum — mas turno pessoal não lê nem grava esse cache (ADR-002).
7. **Plano e quadro** — o quadro do turno é aberto em `turn_blackboard`; em pedido composto o orquestrador decompõe a mensagem em tarefas por especialista (o primeiro agente continua sendo o da regra determinística).
8. **Cadeia de agentes** — até `MAX_HOPS = 5` hops, um por super-step com checkpoint. Cada especialista escolhe tools tipadas que o registry libera; o executor lê o Mongo com filtro de ownership reconstruído; escrita passa por taint web → intenção explícita do cliente → política de aprovação. Handoff é a tool `transfer_to_agent`, com destino validado pelo servidor; se o agente para e o plano tem passo pendente, o servidor passa a vez. Sem modelo, o runner determinístico responde.
9. **Pausa (se houver)** — escrita sujeita a `hitl_policies` abre `pending_reviews` e o grafo para em `interrupt()`; a decisão do analista retoma a thread daquele turno e a escrita aprovada roda uma vez no nó `apply_approved`.
10. **Consolidação** — o orquestrador escreve uma resposta única a partir do quadro.
11. **Guardrail de saída** — checa vazamento de segredo/marcador interno.
12. **Persistência** — conversa (`agent_conversations`, delta via `$push`/`$slice`), handoffs (`agent_handoffs`), quadro (`turn_blackboard`), trace completo (`agent_traces` + Langfuse + spans GenAI), métricas cumulativas por collection+operação.

Diagrama de topologia e sequência mais detalhado (Mermaid) já existe em `../architecture.md` (nível de repositório) — este arquivo não duplica os diagramas, foca no "porquê".

## Decisões de arquitetura que valem citar numa call

- **MongoDB como plano de coordenação, não só de dados**: routing rules, registry de agentes, handoffs, cache e decisões são documentos comuns — dá para consultar "por que o agente X decidiu Y" com um `find`, sem instrumentar nada à parte. Ver ADRs em `../adr/`.
- **O LLM propõe, o servidor dispõe**: no tool calling o modelo escolhe a tool e um id validado/enum fechado; toda leitura no Mongo é construída em Python com filtro de ownership, e toda escrita passa por três portões no servidor (taint web, pedido de ação explícito na mensagem do cliente — pergunta não conta, e status só no pedido que o cliente citou —, política de aprovação humana). No caminho determinístico o LLM só redige a frase final sobre o documento já retornado (`agents.py::llm_synthesize`). A segurança de dados fica fora do raciocínio do modelo.
- **Quadro do turno em vez de contexto compartilhado**: o próximo agente vê só fatos gerados pelo servidor (tools e escritas efetivadas), nunca o texto de outro modelo — evita prompt injection entre agentes e mesmo assim acaba com a contradição medida em 2026-11-12 ("não encontrei a troca" dois parágrafos antes de a troca ser registrada). O custo é medido e mostrado: no cenário principal ~US$ 0,04 → ~US$ 0,22–0,27 por turno e 4 → 10–14 chamadas ao modelo; no A/B, agente único ~9 s/~US$ 0,06 contra o time ~40 s/~US$ 0,27 (ADR-011).
- **Pausa é checkpoint**: aprovação humana usa `interrupt()` do LangGraph sobre o checkpoint no Atlas — sem conexão aberta, sobrevive a restart, retoma uma vez (ADR-012).
- **Roteamento determinístico é a regra, LLM é a exceção**: `cheap_route` (keyword + prioridade seedada) resolve a maioria; o LLM só decide quando não há nenhum sinal de regra, e nunca sobrescreve uma decisão determinística já confiante — sampling variance tornava o roteamento não-reprodutível quando podia.
- **Handoff sequencial vs. fan-out paralelo**: cadeias com dependência real (diagnosticar → recomendar → efetivar) são sequenciais; perguntas genuinamente independentes (status do pedido + valor da fatura) rodam em paralelo via `asyncio.gather`, restrito ao par `order_agent`/`billing_agent` de propósito.
- **Limiar é medido, nunca escolhido**: os três cortes semânticos (denylist, bloqueio direto do denylist, classificador de turno) saem de `calibrate_thresholds.py` contra probes rotulados — e os probes de teste são distintos dos semeados, senão o que se mede é o índice, não a separação. Ver ADR-002 e ADR-003.
- **Falha fechada, mas só onde há risco**: sem veredito do classificador de turno, descarta-se o HIT de cache **global**; o da própria sessão continua servindo, porque não sai do dono. Fechar tudo quebrava o cache sem ganho de segurança.
- **A web é mais uma fonte de recuperação determinística, não um agente autônomo** (ADR-005): o servidor decide buscar (entrega vencida + ferramenta ligada para a área) e monta a consulta a partir do documento. O Atlas governa a ferramenta: registro por `update_one` (`tool_registry`), cota mensal atômica (`web_search_quota`), cache de resultado com TTL (`web_search_cache`) e auditoria (`external_calls`). Fora de escopo continua com 0 tokens.
- **Escrita é exceção, não regra**: dos 7 especialistas só 4 têm qualquer tool de escrita (`order_agent`, `loyalty_agent`, `logistics_agent`, `support_agent`), e cada write é restrito a um allowlist de valores/campos aprovados — nunca um `$set` arbitrário vindo do LLM. No A/B as escritas são simuladas.
- **Grafo é LangGraph, política é nossa**: o `StateGraph` cuida de fluxo, checkpoint e `interrupt()`; roteamento, guardrails, cache, budget, portões de escrita e políticas de aprovação ficam em módulos próprios, chamados pelos nós. O MongoDB segue sendo o plano de coordenação (registry, handoffs, quadro do turno, traces, checkpoints).
- **Interoperabilidade sem abrir escrita**: o card A2A sai do registry a cada requisição (desligar um agente o tira do card, sem modelo/orçamento no payload); o servidor MCP expõe só tools de leitura, com identidade do JWT, porque o portão de escrita exige a mensagem do próprio cliente (ADR-013).

## Decisões de desenho

Detalhes de implementação que vieram de medição ou de bug real e que não estão nos ADRs:

- **Datas do seed são relativas ao dia em que o seed/reset roda.** `seed_documents(today)` em `backend/app/seed_data.py` ancora pedidos, faturas e envios em `clock.local_now()` (o roteiro original deslocado em bloco). Com datas fixas a demo envelhecia: em novembro a logística dizia "116 dias além da data prevista". Vereditos preservados para qualquer dia (`backend/tests/test_seed_dates.py`): os quatro envios não entregues já venceram (PED-1001 há 5 dias, o que dispara a busca web), todos os pedidos seguem na garantia, faturas abertas vencem no futuro e a cadeia de trocas mantém a ordem. Os dias de atraso crescem até o próximo reset.
- **O registry tem só os 8 agentes reais.** Se o `agent_registry` diz N agentes, N respondem. Uma iteração anterior inflou a contagem com documentos inertes para afirmar "100+ agentes"; isso foi revertido de propósito e não deve voltar.
- **O handoff pós-escrita do `order_agent` roda mesmo sem mudança de status no turno.** Depender da mudança de estado dentro do turno fazia um "quero trocar" repetido (depois de outro turno ou caso de eval já ter mudado o pedido) perder o handoff para `billing_agent`/`logistics_agent` em silêncio.
- **O `support_agent` só abre chamado em pedido explícito de escalonamento** ("atendente", "chamado", "escalar"), nunca por "sem evidência na KB": no `DEMO_MODE` o ranking local de fallback sempre devolve algum artigo, qualquer que seja a relevância.
- **Colisão de palavra-chave no `cheap_route`.** No desempate por `priority`, uma palavra de um agente que vem *depois* na cadeia (ex.: "transportadora") pode vencer o gatilho do agente que vem *antes* (ex.: "troca") e pular o hop. Mensagens compostas da demo evitam a colisão ("entrega" em vez de "transportadora"). A checagem por substring exige as duas concordâncias de gênero ("parecido" e "parecida").
- **Prompts de demo por identidade** (`DEMO_SCENARIOS` em `backend/app/seed_data.py`, servidos por `GET /api/demo-scenarios`) sempre acionam 2+ agentes, uma escrita real ou um guardrail — nunca uma leitura simples de um agente só.

## Segurança e isolamento

- `customer_key` só existe no JWT; todo filtro de leitura/escrita é reconstruído no backend, nunca aceito do payload (`policies.py`).
- Transações multi-documento (quando o cluster é replica set) amarram escrita de negócio + registro de decisão — `run_in_transaction_with_retry` repete em `TransientTransactionError`.
- `customer_key` do servidor MCP vem só do JWT em `MCP_CUSTOMER_TOKEN`, revalidado a cada chamada (`security.py::decode_customer_key`, a mesma checagem da API).
- `$jsonSchema` validators em `agent_handoffs`, `pending_reviews`, `agent_decisions`, `agent_audit_events`, `agent_traces` — o próprio MongoDB rejeita documento malformado, não só a camada Python.

## Gaps conhecidos (verificados no código, não só na doc)

- Métricas (`metrics.py`) são in-process, resetam a cada restart — sem agregação entre instâncias.
- Tool calling, plano, consolidação e o guardrail semântico custam tokens reais por turno — o turno multiagente completo custa ~5x o anterior e leva ~40 s; os passos independentes do plano ainda rodam em sequência (paralelizá-los é o próximo passo de latência).
- Container único (nginx + uvicorn, `Dockerfile`); dev local via venv + npm.
- `DEMO_MODE` **esconde** classes inteiras de bug do driver real (datetime sem fuso, `ObjectId` cru na timeline, documento legado sem o campo novo) — os três foram achados só em modo LIVE, com a suíte offline verde. Por isso existem as suítes `tests/test_live*.py` e o hook `pre-push`.
- A orientação de fora de escopo é texto fixo, igual para qualquer assunto — resposta mais natural custaria uma chamada de LLM por pergunta alheia.
