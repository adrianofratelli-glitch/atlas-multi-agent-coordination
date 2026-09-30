# Queries, aggregation pipelines e índices

Levantamento feito direto no código (`grep .aggregate(`, `.find(`, `create_index`, `find_one`, `find_many` em `backend/app/`), não de memória. Referências `arquivo:linha` correspondem ao estado do repo nesta revisão — confira a linha se o arquivo mudar.

A maior parte das leituras/escritas simples passa pela abstração `DataStore` (`database.py`), que despacha para `pymongo` real ou para um backend em memória (`DEMO_MODE`) com o mesmo contrato. Esta seção documenta as **queries de negócio construídas em cima dessa abstração** e as **6 aggregation pipelines reais** do sistema (as únicas que usam `store.aggregate()` / `$vectorSearch` / `$search` / `$graphLookup` / `$rankFusion`).

---

## 1. Aggregation pipelines

### 1.1 `$graphLookup` — cadeia de reposição de pedido
**Onde**: `backend/app/graph.py:17-44` (`build_order_chain_pipeline`), chamada em `backend/app/agents.py:264-273` (`order_replacement_chain`).

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

**Fallback**: `traverse_order_chain_in_memory` (`graph.py:71-92`) — mesmo algoritmo em Python puro, usado só em `DEMO_MODE`/CI, sem Atlas.

**Índice de suporte**: `orders` tem índice composto `(owner_customer_key, replacement_order_id)` — sem ele a travessia vira collection scan a cada salto (ver seção 3).

---

### 1.2 `$vectorSearch` + `$unionWith` — cascata de cache semântico
**Onde**: `backend/app/cascade.py:37-57` (`cascade_lookup`).

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

**Fallback exato**: se MISS vetorial, `_cascade_lookup_fallback` (`cascade.py:99-112`) ainda tenta `question_norm` idêntico por `find_one` — texto literalmente igual sempre dá HIT, porque medições mostraram perguntas curtas repetidas pontuando entre 0.81–0.92 no índice vetorial, abaixo do corte fixo em alguns casos.

---

### 1.3 `$vectorSearch` — memória de longo prazo
**Onde**: `backend/app/cascade.py:121-126` (`cascade_long_term_context`).

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
**Onde**: `backend/app/agents.py:594-616` (`search_products`), usado por `run_product_agent`.

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

**Fallback**: `_local_rank` (`agents.py:556-566`) — ranking local por interseção de palavras (stem simples pt-BR) usado em `DEMO_MODE`.

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
**Onde**: `backend/app/guardrails.py:31-61` (`semantic_denylist`).

```python
[
  {"$vectorSearch": {"index": "denylist_autoembed_v1", "path": "phrase",
                      "query": {"text": message}, "model": "voyage-4",
                      "filter": {"area": {"$in": ["global", area]}, "active": True, "layer": "semantic"},
                      "numCandidates": 50, "limit": 1}},
  {"$project": {"phrase": 1, "category": 1, "area": 1, "score": {"$meta": "vectorSearchScore"}}},
]
```

**Por que existe**: Jaccard sobre palavras não separa paráfrase de ataque ("quero ver dados de outro comprador") de pergunta legítima ("pode me enviar a nota fiscal da minha compra?") — as duas pontuam parecido. Só busca vetorial pega isso sem custo de LLM, e roda mesmo quando `skip_semantic` desativa o classificador LLM (mensagem já roteada com confiança). `area` é filtro nativo do índice — a busca ANN nunca varre entradas fora da área do cliente. Camada aprende sozinha: um bloqueio do classificador LLM escreve de volta em `guardrail_denylist` (`_reinforce_denylist`, `guardrails.py:192-214`), então o próximo ataque igual é pego aqui, de graça.

**Calibração**: `vector_threshold` em `guardrail_policies` é medido contra probes de paráfrase real (`backend/calibrate_thresholds.py`), nunca "chutado" — calibrar com quase-cópias da frase original reverte o guardrail para match exato.

---

### 1.6 Busca híbrida de KB — `$rankFusion` (server-side) + fallback RRF em app
**Onde**: `backend/app/retrieval.py` (pipelines), `backend/app/agents.py:690-719` (`search_kb`, orquestra qual estratégia usar).

**Caminho preferido — `$rankFusion` (MongoDB 8.1+)**, `retrieval.py:55-71`:
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

**Fallback — duas pernas separadas + RRF na aplicação** (`retrieval.py:9-25,28-52`, quando o servidor não tem `$rankFusion` ou o índice está indisponível): `build_kb_vector_pipeline` ($vectorSearch em `content`) e `build_kb_lexical_pipeline` ($search BM25 com boost 2x no título) rodam em paralelo (`asyncio.gather`), e `reciprocal_rank_fusion` funde os dois rankings em Python (`k=60`, fórmula RRF clássica `1/(k+posição)`).

**Por que existe**: busca puramente semântica erra sinônimo raro de produto/código de erro; busca puramente lexical erra paráfrase. RRF combina os dois sem comparar escalas de score diferentes (cosine similarity vs. BM25). `$rankFusion` server-side evita 2 round-trips e o tráfego do ranking intermediário (até 80 docs/perna) pela rede — mesma matemática, uma agregação só.

**Fallback total (DEMO_MODE)**: `_local_rank` duas vezes com campos diferentes (léxico: title/content; "semântico" local: category/title) + RRF — sem Atlas Search real.

---

## 2. Queries de negócio (find/find_one/update_one) — principais padrões

Todas passam por `DataStore` (`database.py`), que injeta `session=` de transação quando aplicável. Não são exaustivamente listadas (há dezenas de `find_one`/`find_many` simples por chave primária), só os padrões que valem citar numa call:

| Padrão | Onde | Por quê |
|---|---|---|
| `orders.find_one({"order_id": ..., "owner_customer_key": ...})` | `agents.py:104-106` (`run_order_agent`) e repetido em `warranty_agent`, `logistics_agent` | Ownership sempre no filtro — nunca aceito do payload, sempre reconstruído do JWT (`policies.py:safe_order_read_filter`) |
| Pedido "mais recente" via `find_many(..., sort=[("order_id", -1)], limit=1)` | `agents.py:112-114` | Só cai nesse fallback quando NÃO havia PED- explícito nem contexto ativo — evita resolver silenciosamente o pedido errado |
| `order_status change` via `update_one` restrito a um allowlist de status | `policies.py:safe_order_update`, usado em `agents.py:172-187` | Nunca um `$set` arbitrário vindo do LLM — só valores aprovados (`troca_solicitada`, `reembolsado`, etc.) |
| Escrita de negócio + registro de decisão na MESMA transação | `agents.py:178-187` (`_write` dentro de `run_in_transaction_with_retry`) | Mudar status sem gravar o "porquê" é o furo que a trilha de auditoria existe para fechar |
| `redemptions.find_many(..., sort=[("at", -1)])` + janela de 20s | `agents.py:419-425` | Idempotência: retry de rede não pode debitar pontos duas vezes — checa resgate idêntico recente antes de escrever |
| `customer_memory` com supersessão (`active: False` no antigo, novo documento) | `memory.py` (`extract_and_store`) | Nunca sobrescreve um fato — encerra o velho e insere o novo, mantendo histórico completo |
| `agent_conversations` update via `$push`/`$each`/`$slice: -20` | `orchestration.py:428-472` (`_update_conversation`) | Delta atômico no servidor, nunca um `replace_one` do documento inteiro — evita lost update em turnos concorrentes na mesma conversa |
| `pending_reviews.find_one({"subject_id", "action", "status": "pending"})` (idempotência de abertura) | `reviews.py:55` | Mesmo caso de qualidade não abre duplicado se o cliente insistir na mesma frase |
| Change Stream em `agent_handoffs` filtrado por `customer_key` no `$match` | `database.py:335-359` (`watch_handoffs`) | Live feed server-side já isolado por dono, sem `find_one` extra por evento — `customer_key` é denormalizado no handoff exatamente para isso |

---

## 3. Índices MongoDB

Definidos centralizadamente em `backend/app/database.py:475-527` (`create_standard_indexes`), aplicados no boot (`main.py`, `create_indexes=True` no seed). Não roda em `DEMO_MODE`.

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
| `semantic_cache` | `(agent, area)`, `(expires_at)` | não | **TTL 0 = expira no valor do campo** | Cache expira sozinho (24h fixadas na escrita, `cascade.py`) |
| `short_term_memory` | `(session_id)`, `(expires_at)` | não | **TTL 0** | Memória de sessão — expira sozinha |
| `long_term_memory` | `(customer_key)` | não | — | Episódios do cliente, sem expiração (é memória de longo prazo por definição) |
| `guardrail_denylist` | `(phrase_norm)` | sim | — | Match exato de substring rápido + chave de upsert do reforço aprendido |
| `guardrail_events` | `(at)` | não | **30 dias** | Log de auditoria de guardrail, não permanente |
| `guardrail_candidates` | `(status, created_at)` | não | — | Fila de revisão humana ordenada |
| `admin_audit` | `(at)` | não | **30 dias** | Auditoria administrativa |
| `eval_runs` | `(at)` | não | **90 dias** | Histórico de avaliação (golden dataset), retenção mais longa |
| `support_tickets` | `(customer_key, created_at)` | não | — | Chamados do cliente ordenados |
| `redemptions` | `(customer_key, at)` | não | — | Suporte à checagem de idempotência de resgate |
| `pending_reviews` | `(review_id)` | sim | — | Lookup direto |
| `pending_reviews` | `(subject_id, action, status)` | não | — | **Idempotência**: `open_review` consulta isto antes de abrir, para o analista não receber o mesmo caso duas vezes |
| `pending_reviews` | `(status, created_at)` | não | — | Fila de revisão ordenada |
| `agent_decisions` | `(decision_id)` | sim | — | Lookup direto |
| `agent_decisions` | `(customer_key, at)`, `(subject_id, at)` | não | — | Trilha de auditoria por cliente e por objeto (pedido/resgate/etc.), sem retenção — é registro de conformidade, fica para sempre |
| `agent_audit_events` | `(decision_id)`, `(customer_key, at)`, `(subject_id, at)` | não | — | Mesma lógica de `agent_decisions` |

**Nota deliberada do código** (`database.py:501-503`): `agent_traces`/`agent_handoffs` são observabilidade e expiram em 30 dias; decisão (`agent_decisions`) e trilha de auditoria (`agent_audit_events`) são registro de conformidade e **não** têm TTL — ficam para sempre, de propósito.

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
