# Arquitetura — multiagente-atendimento

> Para "onde está a query X" veja `queries.md`. Para "como o roteamento/checkpoint dos agentes funciona" veja `agent-behavior.md`. Para telas veja `ui-flows.md`. Este arquivo é a visão geral e o "porquê" das decisões estruturais.

## O que é

PoV de atendimento ao cliente multiagente onde o **MongoDB Atlas é tanto o data plane quanto o plano de coordenação**: regras de roteamento, estado dos agentes, handoffs, memória (curto e longo prazo), cache semântico e decisões de guardrail vivem todos em documentos MongoDB, consultáveis como qualquer outro dado operacional — não numa fila/engine de workflow separada.

8 agentes reais registrados em `agent_registry`: `orchestrator`, `order_agent`, `product_agent`, `support_agent`, `billing_agent`, `warranty_agent`, `loyalty_agent`, `logistics_agent`.

**Orquestração**: o turno é um `StateGraph` do LangGraph (`backend/app/orchestration_graph.py`, migrado em 2026-09-29) com `MongoDBSaver` como checkpointer (`langgraph_checkpoints`, `thread_id = <customer_key>:<conversation_id>`, TTL 24h). Detalhes em `agent-behavior.md`.

## Stack

- **Frontend**: React + Vite (`frontend/src`), porta 5191 (estrita, sem fallback).
- **Backend**: FastAPI (`backend/app/main.py`), porta 8031 (estrita, sem fallback), driver `pymongo` assíncrono (`AsyncMongoClient`).
- **LLM**: Anthropic (`backend/app/llm.py`), usado para (a) classificação de intenção quando a regra determinística empata, (b) síntese da resposta final sobre documentos já buscados no Mongo, (c) classificador de guardrail semântico.
- **Dados/coordenação**: MongoDB Atlas — dois bancos lógicos, `multi_agent_poc` (dados de negócio + estado operacional) e `multiagent_brain` (configuração: `agent_registry`, `routing_rules`, `guardrail_policies`).
- **Busca**: Atlas Vector Search com Automated Embedding (`voyage-4`), Atlas Search (BM25/lexical), `$rankFusion` (MongoDB 8.1+) para híbrido server-side.
- **Observabilidade**: Langfuse self-host (uma trace por turno), Change Streams para live feed de handoffs, métricas in-process (`backend/app/metrics.py`).
- **Modo sem Atlas**: `DEMO_MODE=1` troca `DataStore` para um backend em memória (`backend/app/database.py`) com os mesmos contratos — usado por testes/CI, sem Vector Search/Change Streams reais.

## Componentes principais

| Arquivo | Responsabilidade |
|---|---|
| `backend/app/main.py` | Rotas FastAPI, auth JWT, middleware, SSE de eventos |
| `backend/app/orchestration.py` | `OrchestrationService.run_turn` — o "grafo" de handoff, cache, memória, trace |
| `backend/app/router.py` | Roteamento determinístico por keyword (`cheap_route`), detecção de fan-out, orquestrador fallback |
| `backend/app/agents.py` | Os 7 runners de agente (`RUNNERS`), lógica de negócio de cada um |
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
7. **Loop de agentes (handoff)** — até `MAX_HOPS = 5` agentes em cadeia, cada um podendo pedir handoff explícito para outro. Cada runner lê o Mongo com filtro de ownership reconstruído, e opcionalmente sintetiza a resposta final via LLM sobre o documento já buscado (nunca o LLM decide o que buscar).
8. **Guardrail de saída** — checa vazamento de segredo/marcador interno.
9. **Persistência** — conversa (`agent_conversations`, delta via `$push`/`$slice`), handoffs (`agent_handoffs`), trace completo (`agent_traces` + Langfuse), métricas cumulativas por collection+operação.

Diagrama de topologia e sequência mais detalhado (Mermaid) já existe em `../architecture.md` (nível de repositório) — este arquivo não duplica os diagramas, foca no "porquê".

## Decisões de arquitetura que valem citar numa call

- **MongoDB como plano de coordenação, não só de dados**: routing rules, registry de agentes, handoffs, cache e decisões são documentos comuns — dá para consultar "por que o agente X decidiu Y" com um `find`, sem instrumentar nada à parte. Ver ADRs em `../adr/`.
- **LLM nunca escolhe o que buscar**: toda leitura no Mongo é construída em Python com filtro de ownership; o LLM só redige a frase final sobre o documento já retornado (`agents.py:llm_synthesize`). Isso mantém a segurança de dados fora do raciocínio do modelo.
- **Roteamento determinístico é a regra, LLM é a exceção**: `cheap_route` (keyword + prioridade seedada) resolve a maioria; o LLM só decide quando não há nenhum sinal de regra, e nunca sobrescreve uma decisão determinística já confiante — sampling variance tornava o roteamento não-reprodutível quando podia.
- **Handoff sequencial vs. fan-out paralelo**: cadeias com dependência real (diagnosticar → recomendar → efetivar) são sequenciais; perguntas genuinamente independentes (status do pedido + valor da fatura) rodam em paralelo via `asyncio.gather`, restrito ao par `order_agent`/`billing_agent` de propósito.
- **Limiar é medido, nunca escolhido**: os três cortes semânticos (denylist, bloqueio direto do denylist, classificador de turno) saem de `calibrate_thresholds.py` contra probes rotulados — e os probes de teste são distintos dos semeados, senão o que se mede é o índice, não a separação. Ver ADR-002 e ADR-003.
- **Falha fechada, mas só onde há risco**: sem veredito do classificador de turno, descarta-se o HIT de cache **global**; o da própria sessão continua servindo, porque não sai do dono. Fechar tudo quebrava o cache sem ganho de segurança.
- **Escrita é exceção, não regra**: das 7 agentes só 4 têm qualquer efeito de escrita (`order_agent`, `loyalty_agent`, `logistics_agent`, `support_agent`), e cada write é restrito a um allowlist de valores/campos aprovados — nunca um `$set` arbitrário vindo do LLM.
- **Grafo é LangGraph, política é nossa**: o `StateGraph` cuida de fluxo e checkpoint; roteamento, guardrails, cache e budget ficam em módulos próprios, chamados pelos nós. O MongoDB segue sendo o plano de coordenação (registry, handoffs, traces, checkpoints).

## Segurança e isolamento

- `customer_key` só existe no JWT; todo filtro de leitura/escrita é reconstruído no backend, nunca aceito do payload (`policies.py`).
- Transações multi-documento (quando o cluster é replica set) amarram escrita de negócio + registro de decisão — `run_in_transaction_with_retry` repete em `TransientTransactionError`.
- `$jsonSchema` validators em `agent_handoffs`, `pending_reviews`, `agent_decisions`, `agent_audit_events`, `agent_traces` — o próprio MongoDB rejeita documento malformado, não só a camada Python.

## Gaps conhecidos (verificados no código, não só na doc)

- Métricas (`metrics.py`) são in-process, resetam a cada restart — sem agregação entre instâncias.
- LLM synthesis e o guardrail semântico custam tokens Anthropic reais por turno — ok para demo, precisaria de cache/sampling antes de volume alto de produção.
- Sem containerização (sem Dockerfile/docker-compose) — dev local via venv + npm.
- `DEMO_MODE` **esconde** classes inteiras de bug do driver real (datetime sem fuso, `ObjectId` cru na timeline, documento legado sem o campo novo) — os três foram achados só em modo LIVE, com a suíte offline verde. Por isso existem as suítes `tests/test_live*.py` e o hook `pre-push`.
- A orientação de fora de escopo é texto fixo, igual para qualquer assunto — resposta mais natural custaria uma chamada de LLM por pergunta alheia.
