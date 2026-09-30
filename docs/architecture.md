# Arquitetura

## Topologia

```mermaid
flowchart LR
    UI[React / Vite] --> API[FastAPI]
    API --> G[Guardrail de entrada]
    G --> R{Routing rules}
    R -->|óbvio| OA[order_agent]
    R -->|ambíguo| ORC[orchestrator]
    ORC --> OA
    ORC --> PA[product_agent]
    ORC --> SA[support_agent]
    ORC --> BA[billing_agent]
    ORC --> WA[warranty_agent]
    ORC --> LA[loyalty_agent]
    ORC --> LG[logistics_agent]
    SA -->|handoff explícito| PA
    PA -->|ação de troca| OA
    OA --> BA & LG
    OA & PA & SA & BA & WA & LA & LG --> DATA[(multi_agent_poc)]
    ORC --> BRAIN[(multiagent_brain)]
    DATA --> TRACE[agent_handoffs + agent_traces]
```

## Fluxo de um turno com handoff

```mermaid
sequenceDiagram
    participant C as Cliente
    participant A as API
    participant M as MongoDB Atlas
    participant O as Orchestrator
    participant S as support_agent
    participant P as product_agent

    C->>A: mensagem + JWT
    A->>A: claim sub, rate limit e máscara de PII
    A->>M: guardrail + routing_rules
    A->>O: intenção ambígua
    O-->>A: support_agent
    A->>M: cache(agent=support, area=tenant)
    A->>S: mensagem + contexto bounded
    S->>M: RAG híbrido em kb_articles
    S-->>A: orientação + transferir para product_agent
    A->>M: insert agent_handoffs
    A->>P: contexto do handoff
    P->>M: $vectorSearch em products_catalog
    P-->>A: recomendação
    A->>M: conversa, cache e trace
    A-->>C: resposta consolidada + timeline
```

Esse mesmo turno vira uma trace Langfuse opcional (`backend/app/langfuse_client.py`), com um span/generation por passo acima — ver seção de Observability no README.

## Limites de segurança

- O token define `customer_key`; campos de identidade do payload são ignorados.
- Filtros de pedido e fatura são reconstruídos do zero com ownership.
- Somente `order_agent` recebe a ferramenta de escrita, limitada a `$set.status` e estados aprovados.
- O plano `multiagent_brain` é alterado somente pelos endpoints administrativos.
- Conversas são bounded a 20 mensagens e expiram em 24 horas; eventos e auditoria expiram em 30 dias.
- `conversation_id` e memória de curto prazo são validados junto com o `customer_key`; conhecer um ID não permite retomar ou sobrescrever a conversa de outro cliente.

## Retrieval

- Catálogo: `autoEmbed` com `voyage-4`, `indexingMethod: flat` e filtros `active`, `category`, `price`.
- Base de suporte: ranking vetorial e BM25 combinados com RRF.
- Memória longa: um documento por fato, com `customer_key + active` no índice vetorial.
- Cache curto: índice vetorial particionado por `session_id + customer_key + agent`.

### Operadores em uso (auditado em 2026-08-18)

`$vectorSearch` (15 ocorrências), `$unionWith` (3 — a cascata de cache), `$search`/BM25 (1 — perna lexical da KB), mais o ranking ponderado do catálogo com `$addFields`/`$multiply` dentro da própria agregação. **Zero `$regex` em query.**

`find` aparece só em point lookup por índice (`order_id`, `invoice_id`, `customer_key`) e leitura de configuração no `multiagent_brain` — trocar isso por busca textual seria erro de modelagem. O diferencial não é evitar `find`: é o mesmo cluster ser data plane e coordination plane, sem fila, workflow engine ou vector DB ao lado.

Detalhe completo, com os pipelines colados, em [`docs/briefing/02-mongodb.md`](prompts/02-mongodb.md).
- Cache entre sessões: documentos `scope=customer` sempre filtram `customer_key`.
- Cache global: limitado a catálogo/KB sem memória personalizada, handoff ou escrita; garantia, pedido, cobrança, fidelidade e logística nunca são compartilhados entre clientes.
