# Queries, aggregation pipelines e índices

> Números de linha podem ficar defasados após mudanças: a fonte atualizada é [MAPA_DO_CODIGO.md](../MAPA_DO_CODIGO.md).

Levantamento feito direto no código (`grep .aggregate(`, `.find(`, `create_index`, `find_one`, `find_many` em `backend/app/`), não de memória. As referências usam `arquivo::função` sempre que possível, porque números de linha envelhecem.

A maior parte das leituras/escritas simples passa pela abstração `DataStore` (`database.py`), que despacha para `pymongo` real ou para um backend em memória (`DEMO_MODE`) com o mesmo contrato. Esta seção documenta as **queries de negócio construídas em cima dessa abstração** e os **pipelines de agregação e busca** do sistema (`store.aggregate()`, `$vectorSearch`, `$search`, `$graphLookup`, `$rankFusion`, `$scoreFusion`, `$unionWith`, `$rerank`), além das escritas de coordenação do núcleo agêntico (quadro do turno, políticas de aprovação, revisão pausada — seção 1.8). As seções abaixo detalham as principais; o conjunto completo (cadeia de trocas, busca de produtos, RAG híbrido, denylist do guardrail, classificadores de escopo e de turno, cascata de cache com `$unionWith`, memória longa, série temporal de uso e change stream de handoffs) está na tabela de pipelines do [MAPA_DO_CODIGO.md](../MAPA_DO_CODIGO.md).

---

## 1. Aggregation pipelines

### 1.1 `$graphLookup` — cadeia de reposição de pedido
**Onde**: `backend/app/graph.py::build_order_chain_pipeline`, chamada em `backend/app/agents.py::order_replacement_chain` (tool `check_warranty` e runner da garantia).

**O que faz**: parte de um pedido do cliente autenticado e segue `replacement_order_id → order_id` recursivamente (até `max_depth=6` saltos) para montar a cadeia inteira de trocas/reposições daquele pedido.

**Exemplo** (sem dado sensível):
```python
[
  {"$match": {"order_id": "PED-1001", "owner_customer_key": "<customer_key>"}},
  {"$graphLookup": {
      "from": "orders",
      "startWith": "$replacement_order_id",
      "connectFromField": "replacement_order_id",
      "connectToField": "order_id",
      "as": "chain",
      "maxDepth": 6,
      "depthField": "depth",
      "restrictSearchWithMatch": {"owner_customer_key": "<customer_key>"},
  }},
  {"$project": {"_id": 0, "order_id": 1, "product": 1, "status": 1,
                "replacement_order_id": 1, "chain": {"$map": {...}}}},
]
```

**Por que existe**: "quantas vezes esse pedido já foi trocado, e sempre pelo mesmo produto?" não dá para responder olhando um documento por vez — é uma travessia de profundidade desconhecida. `$graphLookup` faz o loop dentro do servidor numa única agregação. O sinal de negócio: reposição repetida do MESMO produto 3+ vezes (`RECURRENCE_THRESHOLD = 3`) indica defeito de lote, não azar — o caso vai para `pending_reviews` (revisão humana) em vez de virar mais uma troca automática. `restrictSearchWithMatch` repete o filtro de ownership em CADA salto, para que um campo mal preenchido nunca vaze dado de outro cliente durante a travessia.

**Fallback**: `graph.py::traverse_order_chain_in_memory` — mesmo algoritmo em Python puro, usado só em `DEMO_MODE`/CI, sem Atlas.

**Índice de suporte**: `orders` tem índice composto `(owner_customer_key, replacement_order_id)` — sem ele a travessia vira collection scan a cada salto (ver seção 3).

---

### 1.2 `$vectorSearch` + `$unionWith` — cascata de cache semântico
**Onde**: `backend/app/cascade.py::cascade_lookup` (pipeline em `_cascade_lookup_raw`).

**O que faz**: uma única consulta decide HIT/MISS de cache antes de qualquer chamada ao LLM. Busca vetorial em `short_term_memory` (memória de curto prazo da sessão atual, threshold permissivo — pega reformulação de pergunta) unida com busca vetorial em `semantic_cache` (cache global/por-cliente, threshold rígido — só pergunta já vista e considerada estável).

```python
[
  {"$vectorSearch": {"index": "short_term_autoembed_v1", "path": "question_text",
                      "query": {"text": message}, "model": "voyage-4",
                      "filter": {"session_id": session_id, "customer_key": customer_key, "agent": target},
                      "numCandidates": 50, "limit": 5}},
  {"$addFields": {"score": {"$meta": "vectorSearchScore"}, "fonte": "curto_prazo"}},
  {"$match": {"score": {"$gte": settings.short_term_cache_threshold}}},
  {"$sort": {"score": -1}}, {"$limit": 1},
  {"$unionWith": {"coll": "semantic_cache", "pipeline": [
      {"$vectorSearch": {"index": "cache_autoembed_v1", "path": "question_text",
                          "query": {"text": message}, "model": "voyage-4",
                          "filter": {"$or": [{"scope": "global", "area": area, "agent": target},
                                             {"scope": "customer", "customer_key": customer_key, "agent": target}]},
                          "numCandidates": 100, "limit": 50}},
      {"$addFields": {"score": {"$meta": "vectorSearchScore"}, "fonte": "cache"}},
      {"$match": {"cache_policy": "stable_v1", "score": {"$gte": settings.global_cache_threshold}}},
      {"$sort": {"score": -1}}, {"$limit": 1},
  ]}},
  {"$sort": {"score": -1}}, {"$limit": 1},
]
```

**Por que existe**: economia de custo/latência de LLM. Cada perna é filtrada pelo próprio threshold ANTES do `$unionWith` — senão um score de cache abaixo do corte rígido podia vencer o sort só por ser maior que o corte (mais permissivo) do curto prazo. Um HIT nunca chama o Anthropic — a resposta e a timeline inteira do turno original são replicadas (`replayed: true`).

**Fallback exato**: se MISS vetorial, `cascade.py::_cascade_lookup_fallback` ainda tenta `question_norm` idêntico por `find_one` — texto literalmente igual sempre dá HIT, porque medições mostraram perguntas curtas repetidas pontuando entre 0.81–0.92 no índice vetorial, abaixo do corte fixo em alguns casos.

---

### 1.3 `$vectorSearch` — memória de longo prazo
**Onde**: `backend/app/cascade.py::cascade_long_term_context` (chamada em `orchestration_graph.py::n_chain_prepare`).

```python
[
  {"$vectorSearch": {"index": "long_term_autoembed_v1", "path": "text",
                      "query": {"text": message}, "model": "voyage-4",
                      "filter": {"customer_key": customer_key}, "numCandidates": 50,
                      "limit": settings.long_term_memory_limit}},
  {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
]
```

**Por que existe**: recupera episódios passados relevantes do MESMO cliente (cross-sessão) para enriquecer o prompt do LLM — não é resposta pronta, por isso não conta como cache hit. Roda só em MISS da cascata acima. Falha do índice (construindo/indisponível) cai para `find_many` simples por `customer_key`, sem derrubar o turno.

---

### 1.4 `$vectorSearch` — recomendação de produtos
**Onde**: `backend/app/agents.py::build_product_pipeline` e `search_products_ranked`, usados pela tool `search_catalog` e por `run_product_agent`.

```python
[
  {"$vectorSearch": {"index": "products_autoembed_v1", "path": "search_text",
                      "query": {"text": message}, "model": "voyage-4",
                      "filter": {"active": True, "price": {"$lt": max_price}, "category": category},
                      "numCandidates": 50, "limit": 8}},
  {"$addFields": {"relevance": {"$meta": "vectorSearchScore"}}},
  {"$addFields": {"weighted_score": {"$add": [
      {"$multiply": [0.55, "$relevance"]},
      {"$multiply": [0.30, {"$divide": ["$rating", 5.0]}]},
      {"$multiply": [0.15, {"$divide": [{"$min": ["$stock", 20]}, 20.0]}]},
  ]}}},
  {"$sort": {"weighted_score": -1}}, {"$limit": 4},
  {"$project": {"_id": 0, "sku": 1, "name": 1, "category": 1, "price": 1, "rating": 1, "stock": 1, "relevance": 1, "weighted_score": 1}},
]
```

**Por que existe**: relevância semântica sozinha não é um bom ranking de e-commerce — um produto muito relevante mas sem estoque ou mal avaliado não deveria vencer. O peso 0.55/0.30/0.15 (relevância/nota/estoque) é calculado dentro da própria agregação, sem round-trip extra. Filtro de preço/categoria entra como pré-filtro nativo do índice vetorial (`filter`), não pós-processamento em Python.

**Fallback**: `agents.py::_local_rank` — ranking local por interseção de palavras (stem simples pt-BR) usado em `DEMO_MODE`.

---

### 1.4b Pré-filtro de orçamento no catálogo (o servidor aplica, não o modelo)

`agents.py:build_product_pipeline` — quando o cliente tem `max_price_brl` ativo em `customer_memory`, o teto entra no `filter` do próprio `$vectorSearch` (pré-filtro NATIVO: o grafo ANN só percorre vetores que passam). `price` é campo `filter` em `products_autoembed_v1` — conferido na definição do índice, e há teste que impede o pipeline de filtrar por campo não declarado.

```python
{"$vectorSearch": {
    "index": "products_autoembed_v1", "path": "search_text",
    "query": {"text": message}, "model": "voyage-4",
    "filter": {"active": True, "price": {"$lte": 400}, "category": "Áudio"},
    "numCandidates": 50, "limit": 8,
}}
```

Alternativa descartada: ler N candidatos e cortar com `$match` depois — mais lento e perde item barato quando os primeiros do ranking são todos caros.

### 1.4c `$vectorSearch` — classificador de turno pessoal

`turn_classifier.py:classify` — roda **só num HIT de cache e antes de gravar**, nunca num MISS. Limiar medido (0,7162) vive em `<brain>.turn_classifier_config`.

```python
[
  {"$vectorSearch": {"index": "turn_probes_vs", "path": "phrase",
                     "query": {"text": message}, "model": "voyage-4",
                     "numCandidates": 50, "limit": 1}},
  {"$project": {"phrase": 1, "_id": 0, "score": {"$meta": "vectorSearchScore"}}},
]
```

Erro, índice ausente ou limiar não medido ⇒ trata como pessoal (falha fechada). Em `DEMO_MODE` cai numa sobreposição de palavras contra os probes do código.

### 1.5 `$vectorSearch` — denylist semântico (guardrail)
**Onde**: `backend/app/guardrails.py` (`semantic_denylist`; chamada por `semantic_denylist_by_clause`, que roda o mesmo pipeline sobre a mensagem inteira e sobre cada cláusula, em paralelo — anti-diluição, ver `agent-behavior.md`).

```python
[
  {"$vectorSearch": {"index": "denylist_autoembed_v1", "path": "phrase",
                      "query": {"text": message}, "model": "voyage-4",
                      "filter": {"area": {"$in": ["global", area]}, "active": True, "layer": "semantic"},
                      "numCandidates": 50, "limit": 1}},
  {"$project": {"phrase": 1, "category": 1, "area": 1, "score": {"$meta": "vectorSearchScore"}}},
]
```

**Por que existe**: Jaccard sobre palavras não separa paráfrase de ataque ("quero ver dados de outro comprador") de pergunta legítima ("pode me enviar a nota fiscal da minha compra?") — as duas pontuam parecido. Só busca vetorial pega isso sem custo de LLM, e roda mesmo quando `skip_semantic` desativa o classificador LLM (mensagem já roteada com confiança). `area` é filtro nativo do índice — a busca ANN nunca varre entradas fora da área do cliente. Camada aprende sozinha: um bloqueio do classificador LLM escreve de volta em `guardrail_denylist` (`guardrails.py::_reinforce_denylist` → `guardrail_learning.py::learn`, em quarentena por cliente), então o próximo ataque igual é pego aqui, de graça.

**Calibração**: `vector_threshold` em `guardrail_policies` é medido contra probes de paráfrase real (`backend/calibrate_thresholds.py`), nunca "chutado" — calibrar com quase-cópias da frase original reverte o guardrail para match exato.

---

### 1.6 Busca híbrida de KB — `$rankFusion` (server-side) + fallback RRF em app
**Onde**: `backend/app/retrieval.py` (pipelines; `search_kb_explained` escolhe a estratégia), chamado por `backend/app/agents.py::search_kb` (tool `search_knowledge_base` e runner do suporte).

**Caminho preferido — `$rankFusion` (MongoDB 8.0+)**, `retrieval.py::build_kb_rank_fusion_pipeline` (forma básica, sem sinônimos/fuzzy; a forma completa está no fim deste arquivo):
```python
[
  {"$rankFusion": {"input": {"pipelines": {
      "vector": [
        {"$vectorSearch": {"index": "kb_autoembed_v1", "path": "content",
                            "query": {"text": query}, "model": "voyage-4",
                            "numCandidates": 80, "limit": 16}},
      ],
      "lexical": [
        {"$search": {"index": "kb_lexical_v1", "compound": {"should": [
            {"text": {"query": query, "path": "title", "score": {"boost": {"value": 2}}}},
            {"text": {"query": query, "path": "content"}},
        ], "minimumShouldMatch": 1}}},
        {"$limit": 16},
      ],
  }}}},
  {"$limit": 4},
  {"$project": {"article_id": 1, "title": 1, "content": 1, "category": 1, "_id": 0,
                "rrf_score": {"$meta": "score"}}},
]
```

**Fallback — duas pernas separadas + RRF na aplicação** (`retrieval.py::_app_rrf` e `reciprocal_rank_fusion`, quando o servidor não tem `$rankFusion` ou o índice está indisponível): `build_kb_vector_pipeline` ($vectorSearch em `content`) e `build_kb_lexical_pipeline` ($search BM25 com boost 2x no título) rodam em paralelo (`asyncio.gather`), e `reciprocal_rank_fusion` funde os dois rankings em Python (`k=60`, fórmula RRF clássica `1/(k+posição)`).

**Por que existe**: busca puramente semântica erra sinônimo raro de produto/código de erro; busca puramente lexical erra paráfrase. RRF combina os dois sem comparar escalas de score diferentes (cosine similarity vs. BM25). `$rankFusion` server-side evita 2 round-trips e o tráfego do ranking intermediário (até 80 docs/perna) pela rede — mesma matemática, uma agregação só.

**Fallback total (DEMO_MODE)**: `_local_rank` duas vezes com campos diferentes (léxico: title/content; "semântico" local: category/title) + RRF — sem Atlas Search real.

---

### 1.7 `$scoreFusion` — fusão por score, medida e fora do padrão (2026-11-12)
**Onde**: `backend/app/retrieval.py::build_score_fusion`, escolhida em `build_kb_rank_fusion_pipeline(method="score")` quando `agent_registry.support_agent.retrieval.fusion.method` é `"score"`.

```python
{"$scoreFusion": {
    "input": {"pipelines": {"vector": [<$vectorSearch>], "lexical": [<$search>, {"$limit": n}]},
              "normalization": "minMaxScaler"},
    "combination": {"weights": {"vector": <peso>, "lexical": <peso>}},
    "scoreDetails": True,
}}
# depois: o mesmo $limit -> $addFields fusion_score/score_details -> ($rerank) -> $limit 4 -> $project
```

**Por que não é o padrão**: o `$rankFusion` (RRF) só olha a posição; o `$scoreFusion` soma scores normalizados, então um BM25 muito alto puxa o artigo. Medido nas mesmas consultas (`eval/reports/kb-retrieval-{rankfusion,scorefusion}-2026-11-12.json`): MRR 0,872 e recall@1 0,80 contra 0,983 e 0,967 do `$rankFusion` (ADR-006). Fica como dado para medir de novo com uma base maior. Requer MongoDB 8.3+.

---

### 1.8 Coordenação agêntica: quadro do turno, políticas de aprovação, revisão pausada, cards (2026-11-12)

**Quadro do turno** (`backend/app/blackboard.py`, collection `turn_blackboard`):

```python
# n_chain_prepare -> open_board: um documento por turno
db.turn_blackboard.insert_one({"turn_id": "turn-<12hex>", "conversation_id": ..., "customer_key": ...,
    "message": "<mensagem mascarada>", "detected_specialties": {"order_agent": ["troca"], ...},
    "entries": [], "status": "open", "created_at": now, "updated_at": now})

# n_agent_hop -> append_entry: uma entrada por hop, delta atômico
db.turn_blackboard.update_one({"turn_id": turn_id, "customer_key": customer_key},
    {"$push": {"entries": {"hop": 0, "agent": "support_agent", "task": "...", "mode": "tool_calling",
                           "actions": [{"tool": "search_knowledge_base", "effect": "read"}],
                           "answer": "...", "handoff_to": "product_agent", "at": now}},
     "$set": {"updated_at": now}})

# set_status: plano, pausa e consolidação
db.turn_blackboard.update_one({"turn_id": turn_id, "customer_key": customer_key},
    {"$set": {"status": "open" | "awaiting_approval" | "consolidated", "updated_at": now,
              "plan": [...] | "review_id": "REV-..." | "final_response": "..."}})

# leitura (GET /api/blackboard/latest e recurso MCP coordination://blackboard/latest)
db.turn_blackboard.find({"conversation_id": ..., "customer_key": ...}).sort({"created_at": -1}).limit(1)
```

Por que existe: cada agente respondia à mensagem inteira e as respostas eram concatenadas — o suporte dizia "não encontrei registro de troca" antes do agente de pedidos registrar a troca. O quadro torna a coordenação uma query (`find({conversation_id})` mostra quem fez o quê) e é a fonte única da consolidação. `customer_key` em todo filtro: um quadro nunca é lido ou escrito por outro cliente. Nas `actions`, `args` guarda só ids/enums validados: texto livre do modelo (`summary`, `query`, `task`, `reason`) não entra no quadro (`blackboard.py::_safe_args`). Falha de escrita no quadro não derruba o turno (o espelho no estado do grafo continua valendo).

**Políticas de aprovação humana** (`backend/app/hitl.py`, `<brain>.hitl_policies`):

```python
db.hitl_policies.find({}).limit(50)        # load_policies (n_chain_prepare, n_fanout_check): aplica só as ativas
db.hitl_policies.update_one({"policy_id": "HITL-REEMBOLSO"}, {"$set": {"active": False}})   # PATCH /api/admin/hitl-policies/{id}
db.hitl_policies.replace_one({"policy_id": ...}, policy, upsert=True)                        # seed.py
```

Documento: `{policy_id, tool, when: {"status": "reembolsado"} | {"points_gte": 500}, reason, active}` (no seed, `HITL-RESGATE-ALTO` nasce com `active: false`). Lê todas e filtra as ativas na aplicação, para que desligar todas pelo admin desligue de verdade; só collection vazia ou erro de leitura cai nas políticas padrão do código (falha fechada).

**Revisão pausada por `interrupt()`** (`orchestration_graph.py::n_agent_hop` → `reviews.py::open_review`):

```python
# idempotência POR TURNO: o mesmo turno não empilha duas revisões pendentes (dois turnos = duas pausas)
db.pending_reviews.find_one({"subject_id": "PED-1002", "action": "hitl_request_order_status_change",
                             "customer_key": ..., "status": "pending", "turn_id": "turn-..."})
# na MESMA transação que a decisão de escalonamento (agent_decisions)
db.pending_reviews.insert_one({"review_id": "REV-...", "agent": "order_agent",
    "action": "hitl_request_order_status_change", "subject_id": "PED-1002", "status": "pending",
    "customer_key": ..., "conversation_id": ..., "recommended_action": "approve",
    "reasoning": "<motivo da política>", "risk_factors": ["HITL-REEMBOLSO"], "turn_id": "turn-...",
    "evidence": {"kind": "hitl_interrupt", "tool": "request_order_status_change",
                 "args": {"order_id": "PED-1002", "status": "reembolsado"}, "facts": {...},
                 "thread_id": "<customer_key>:<conversation_id>:<turno>", "turn_id": "turn-..."}})
```

`POST /api/admin/reviews/{id}/resolve` faz o claim atômico de sempre (`reviews.py::resolve_review`) e, quando `evidence.kind == "hitl_interrupt"`, retoma a thread `evidence.thread_id` com `Command(resume=...)` (só se a pausa pendente é desta revisão). A escrita aprovada roda pelo executor da tool, com o mesmo filtro de dono, no nó `apply_approved`, que lê e marca a revisão para não escrever duas vezes:

```python
db.pending_reviews.find_one({"review_id": "REV-...", "customer_key": ...})      # já aplicada? (applied_at)
db.pending_reviews.update_one({"review_id": "REV-..."}, {"$set": {"applied_at": now}})   # depois da escrita
db.agent_handoffs.insert_one({"conversation_id": ..., "customer_key": ..., "from_agent": "order_agent",
    "to_agent": "orchestrator", "reason": "turno retomado após decisão humana", "at": now})  # avisa a tela pelo Change Stream
```

`POST /api/admin/reviews/{id}/continue` relê a revisão (`find_one({review_id})`, precisa estar `resolved` e ter `evidence.thread_id`) e continua a thread se a escrita aprovada tinha falhado.

**Leituras do registry para A2A, MCP e telas** (todas em `multiagent_brain.agent_registry`, sem `_id`):

| Quem | Query | Para quê |
|---|---|---|
| `GET /.well-known/agent-card.json` (`agent_cards.py::system_card`) | `find({}).limit(50)` | card do serviço; agentes inativos ficam de fora; descrição pública (`public_description` ou papel fixo), nunca a `persona` |
| `GET /api/agents/{agent_key}/card` (`agent_cards.py::agent_card`) | `find_one({"agent_key": key})` | 404 se ausente ou inativo; skills = tools liberadas |
| `backend/mcp_server.py` (`CoordinationMCPServer.exposed`) | `find({"active": True}).limit(50)` a cada `tools/list` e `tools/call` | só tools de leitura liberadas por `allowed_tools`; desligar no registry esconde na próxima chamada |
| `backend/mcp_server.py` (recurso `coordination://handoffs/recent`) | `agent_handoffs.find({"customer_key": <do JWT>}).sort({"at": -1}).limit(20)` | handoffs recentes do cliente autenticado |
| `GET /api/tools`, `GET /api/graph/topology` | `find({}).limit(20)` | catálogo de tools por agente, nós do grafo ao vivo |
| `GET /api/memory-typed` | `customer_memory`, `long_term_memory`, `agent_registry`, `routing_rules`, `hitl_policies` | memória semântica, episódica e procedural |
| `n_ingest` | `<brain>.model_config.find_one({"key": "default"})` | `global_turn_tokens` (orçamento global do turno como dado) |

---

## 2. Queries de negócio (find/find_one/update_one) — principais padrões

Todas passam por `DataStore` (`database.py`), que injeta `session=` de transação quando aplicável. Não são exaustivamente listadas (há dezenas de `find_one`/`find_many` simples por chave primária), só os padrões que valem citar numa call:

| Padrão | Onde | Por quê |
|---|---|---|
| `orders.find_one({"order_id": ..., "owner_customer_key": ...})` | `agents.py::load_order` (tools `get_order`/`request_order_status_change` e `run_order_agent`), repetido em garantia e logística (`load_shipment`) | Ownership sempre no filtro — nunca aceito do payload, sempre reconstruído do JWT (`policies.py:safe_order_read_filter`) |
| Pedido "mais recente" via `find_many(..., sort=[("order_id", -1)], limit=1)` | `agents.py::load_order` | Só cai nesse fallback quando NÃO havia PED- explícito nem contexto ativo — evita resolver silenciosamente o pedido errado |
| `order_status change` via `update_one` restrito a um allowlist de status | `policies.py::safe_order_update`, usado em `agents.py::change_order_status` | Nunca um `$set` arbitrário vindo do LLM — só valores aprovados (`troca_solicitada`, `reembolsado`, etc.) |
| Escrita de negócio + registro de decisão na MESMA transação | `agents.py::change_order_status` (`_write` dentro de `run_in_transaction_with_retry`) | Mudar status sem gravar o "porquê" é o furo que a trilha de auditoria existe para fechar |
| `redemptions.find_many(..., sort=[("at", -1)])` + janela de 20s | `agents.py::redeem_reward` (dentro da transação) | Idempotência: retry de rede não pode debitar pontos duas vezes — checa resgate idêntico recente antes de escrever |
| `customer_memory` com supersessão (`active: False` no antigo, novo documento) | `memory.py` (`extract_and_store`) | Nunca sobrescreve um fato — encerra o velho e insere o novo, mantendo histórico completo |
| `agent_conversations` update via `$push`/`$each`/`$slice: -20` | `orchestration.py::_update_conversation` | Delta atômico no servidor, nunca um `replace_one` do documento inteiro — evita lost update em turnos concorrentes na mesma conversa |
| `pending_reviews.find_one({"subject_id", "action", "status": "pending"})` (idempotência de abertura) | `reviews.py:55` | Mesmo caso de qualidade não abre duplicado se o cliente insistir na mesma frase |
| Cota mensal da busca web: `find_one_and_update({"_id": "AAAA-MM", "used": {"$lt": limit}}, {"$inc": {"used": 1}}, upsert=True)` | `database.py:reserve_quota` (usado por `web_search.py`) | Atômico, sem ler-e-depois-incrementar: documento do mês no teto ⇒ o upsert colide com o `_id` (`DuplicateKeyError`) e o provedor nem é chamado |
| `web_search_cache.find_one({"_id": sha256(consulta normalizada), "expires_at": {"$gt": agora}})` | `web_search.py:_run` | Match exato com TTL de 6 h: repetir a busca não gasta crédito. Cache semântico do resultado web foi medido e descartado (ADR-005) |
| `tool_registry.find_one({"_id": "web_search"})` uma vez por turno | `orchestration_graph.py:n_ingest` | Liga/desliga por área ao vivo, sem deploy; ausente ou ilegível = ferramenta desligada |
| Change Stream em `agent_handoffs` filtrado por `customer_key` no `$match` | `database.py::watch_handoffs` / `handoff_pipeline` | Live feed server-side já isolado por dono, sem `find_one` extra por evento — `customer_key` é denormalizado no handoff exatamente para isso |

---

## 3. Índices MongoDB

Definidos centralizadamente em `backend/app/database.py::create_standard_indexes`, aplicados no boot (`main.py`, `create_indexes=True` no seed). Não roda em `DEMO_MODE`.

| Collection | Índice | Único? | TTL | Motivação |
|---|---|---|---|---|
| `customers` | `(customer_key)` | sim | — | Lookup de identidade por chave |
| `orders` | `(order_id)` | sim | — | Lookup direto por pedido |
| `orders` | `(owner_customer_key, status)` | não | — | "meus pedidos com status X" sem scan |
| `orders` | `(owner_customer_key, replacement_order_id)` | não | — | **Suporte direto ao `$graphLookup`** — sem índice em `connectToField`/campo de conexão, a travessia da cadeia de reposição vira collection scan a cada salto |
| `invoices` | `(invoice_id)` | sim | — | Lookup direto |
| `invoices` | `(owner_customer_key, due_date)` | não | — | Fatura mais recente do cliente, ordenada sem scan |
| `loyalty_accounts` | `(customer_key)` | sim | — | Uma conta por cliente |
| `shipments` | `(order_id)` | sim | — | Lookup por pedido |
| `shipments` | `(owner_customer_key)` | não | — | Envio mais recente do cliente |
| `warranty_policies` | `(category)` | sim | — | Política de garantia por categoria de produto |
| `agent_conversations` | `(conversation_id)` | sim | — | Retomar conversa |
| `agent_conversations` | `(updated_at)` | não | **86400s (24h)** | Conversa expira em 24h — sessão não é permanente |
| `customer_memory` | `(customer_key, active)` | não | — | Só fatos ativos do cliente, direto |
| `agent_handoffs` | `(conversation_id, at)` | não | **30 dias** | Observabilidade, não registro de conformidade — expira |
| `agent_handoffs` | `(at)` | não | — | Suporte a `watch_handoffs` / consultas globais por tempo |
| `agent_traces` | `(conversation_id, at)`, `(at)` | não | **30 dias** | Mesma lógica — trace é observabilidade |
| `turn_blackboard` | `(turn_id)` | sim | — | Uma entrada por turno; chave do `$push` de cada hop |
| `turn_blackboard` | `(created_at)` | não | **86400s (24h)** | O quadro é coordenação do turno, não registro: some com a conversa |
| `turn_blackboard` | `(conversation_id, created_at)` | não | — | "Último quadro desta conversa" (`GET /api/blackboard/latest`, recurso MCP) |
| `semantic_cache` | `(agent, area)`, `(expires_at)` | não | **TTL 0 = expira no valor do campo** | Cache expira sozinho (24h fixadas na escrita, `cascade.py`) |
| `short_term_memory` | `(session_id)`, `(expires_at)` | não | **TTL 0** | Memória de sessão — expira sozinha |
| `long_term_memory` | `(customer_key)` | não | — | Episódios do cliente, sem expiração (é memória de longo prazo por definição) |
| `guardrail_denylist` | `(phrase_norm)` | sim | — | Match exato de substring rápido + chave de upsert do reforço aprendido |
| `guardrail_denylist` | `(expires_at)` | não | **TTL 0** | Frase aprendida pelo classificador expira (7 dias, renovada a cada reaprendizado); seed não tem o campo e nunca expira |
| `guardrail_events` | `(at)` | não | **30 dias** | Log de auditoria de guardrail, não permanente |
| `guardrail_candidates` | `(status, created_at)` | não | — | Fila de revisão humana ordenada |
| `admin_audit` | `(at)` | não | **30 dias** | Auditoria administrativa |
| `eval_runs` | `(at)` | não | **90 dias** | Histórico de avaliação (golden dataset), retenção mais longa |
| `web_search_cache` | `(expires_at)` | não | **TTL 0 = expira no valor do campo** | Resultado web envelhece em horas: cada entrada grava `expires_at` = agora + `cache_ttl_seconds` do registro (6 h) |
| `external_calls` | `(at)`, `(area, at)` | não | **30 dias** | Auditoria da chamada externa (consulta mascarada, URLs, latência, cache hit, cota) |
| `support_tickets` | `(customer_key, created_at)` | não | — | Chamados do cliente ordenados |
| `redemptions` | `(customer_key, at)` | não | — | Suporte à checagem de idempotência de resgate |
| `pending_reviews` | `(review_id)` | sim | — | Lookup direto |
| `pending_reviews` | `(subject_id, action, status)` | não | — | **Idempotência**: `open_review` consulta isto antes de abrir, para o analista não receber o mesmo caso duas vezes |
| `pending_reviews` | `(status, created_at)` | não | — | Fila de revisão ordenada |
| `agent_decisions` | `(decision_id)` | sim | — | Lookup direto |
| `agent_decisions` | `(customer_key, at)`, `(subject_id, at)` | não | — | Trilha de auditoria por cliente e por objeto (pedido/resgate/etc.), sem retenção — é registro de conformidade, fica para sempre |
| `agent_audit_events` | `(decision_id)`, `(customer_key, at)`, `(subject_id, at)` | não | — | Mesma lógica de `agent_decisions` |

**Nota deliberada do código** (comentário em `create_standard_indexes`): `agent_traces`/`agent_handoffs` são observabilidade e expiram em 30 dias; decisão (`agent_decisions`) e trilha de auditoria (`agent_audit_events`) são registro de conformidade e **não** têm TTL — ficam para sempre, de propósito.

### Índices Atlas Search / Vector Search (definidos fora do código Python, no Atlas)

Referenciados por nome nas pipelines acima — não são criados por `create_standard_indexes` (que só cria índices B-tree comuns), presume-se provisionados manualmente/via Atlas CLI:

| Nome do índice | Tipo | Collection | Campo | Usado em |
|---|---|---|---|---|
| `short_term_autoembed_v1` | Vector Search (Automated Embedding, voyage-4) | `short_term_memory` | `question_text` | `cascade.py` |
| `cache_autoembed_v1` | Vector Search (Automated Embedding) | `semantic_cache` | `question_text` | `cascade.py` |
| `long_term_autoembed_v1` | Vector Search (Automated Embedding) | `long_term_memory` | `text` | `cascade.py` |
| `products_autoembed_v1` | Vector Search (Automated Embedding) | `products_catalog` | `search_text` | `agents.py:search_products` |
| `denylist_autoembed_v1` | Vector Search (Automated Embedding) | `guardrail_denylist` | `phrase` | `guardrails.py` |
| `scope_probes_vs` | Vector Search (Automated Embedding) | `<brain>.scope_probes` | `phrase` | `scope_classifier.py` — rótulos `in`/`out`/`chat`; criado por `seed_scope_probes.py`, fora do `seed.py` |
| `turn_probes_vs` | Vector Search (Automated Embedding) | `<brain>.turn_probes` | `phrase` | `turn_classifier.py` — criado por `seed_turn_probes.py`, fora do `seed.py` |
| `kb_autoembed_v1` | Vector Search (Automated Embedding) | `kb_articles` | `content` | `retrieval.py` |
| `kb_lexical_v1` | Atlas Search (BM25, analyzer português) | `kb_articles` | `title`, `content` | `retrieval.py` |

## KB com sinônimos e config viva (2026-10, `retrieval.build_kb_rank_fusion_pipeline`)

Perna lexical (dentro do `$rankFusion`), três cláusulas `should` (fuzzy e synonyms não podem estar
na mesma cláusula, doc do operador `text`):

```js
{ $search: { index: "kb_lexical_v1", compound: {
    should: [
      { text: { query: q, path: "title", score: { boost: { value: 2 } } } },
      { text: { query: q, path: "content" } },
      { text: { query: q, path: ["title", "content"], synonyms: "kb_sinonimos", matchCriteria: "any" } },
      { text: { query: q, path: ["title", "content"], fuzzy: { maxEdits: 1 }, score: { boost: { value: 0.5 } } } }
    ],
    minimumShouldMatch: 1,
    filter: [ { equals: { path: "category", value: "<categoria>" } } ]   // opcional
} } }, { $limit: <per_branch_limit> }
```

Depois da fusão: `$limit pool` → `$addFields {fusion_score: $meta score, score_details: $meta scoreDetails}`
→ (rerank ligado) `$set` de guarda → `$rerank {model, path: [title, content], numDocsToRerank}` →
`$limit 4` → `$project` com `rrf_score: "$fusion_score"`. Pesos/profundidade vêm de
`agent_registry.support_agent.retrieval.fusion`.

Catálogo com `$rerank` (`agents.build_product_rerank_pipeline`): `$vectorSearch` (filtro de preço/
categoria) → `$rerank` sobre `name` + `search_text` dos 8 candidatos → `relevance = rerank_score` →
mesma ponderação 0,55/0,30/0,15.

Economia (`usage_ts.usage_pipeline`): `llm_usage_ts` → `$match at >= agora - N h` → `$facet` com
`$group` por `{$dateTrunc: {date: "$at", unit: "hour"}}` + `meta.agent` e o resumo do cache.
