# ADR-001 — Coordenação multi-agente orientada a documentos

- Status: aceito
- Data: 2026-07-15
- Complementado por: [ADR-002](ADR-002-memoria-llm-e-turno-pessoal.md) (memória extraída por LLM, turno pessoal fora do cache) e [ADR-003](ADR-003-guardrail-em-duas-faixas.md) (guardrail em duas faixas, pergunta fora de escopo); [ADR-004](ADR-004-escopo-por-embedding-e-medicao-por-situacoes.md) (escopo por embedding, medição por situações)

## Contexto

O atendimento exige agentes especializados, troca explícita de responsabilidade, isolamento por cliente, auditoria e alteração de política sem novo deploy. A PoV precisa demonstrar esses requisitos sem introduzir uma fila ou um motor de workflow apenas para guardar estado de coordenação.

## Decisão

Usar MongoDB Atlas como plano de dados e de coordenação:

- `ai_brain.agent_registry` define o time de agentes, modelos, ferramentas e budgets;
- `ai_brain.routing_rules` mantém regras determinísticas de primeiro nível;
- `agent_conversations` guarda somente o contexto curto, bounded e com TTL;
- `agent_handoffs` e `agent_traces` são eventos independentes e consultáveis;
- dados operacionais, memória longa, cache e guardrails mantêm filtros de tenant nos próprios documentos e índices.

O orquestrador não responde ao cliente. Casos inequívocos não pagam uma chamada de modelo; ambiguidades passam pelo orquestrador. Um turno percorre no máximo cinco agentes (`MAX_HOPS = 5`), com detecção de ciclo. Perguntas compostas independentes de pedido + fatura usam fan-out paralelo; cadeias com dependência continuam sequenciais.

Antes de chamar um modelo, a aplicação consulta uma cascata de memória:

- memória de curto prazo isolada por `session_id + customer_key + agent`;
- cache semântico do próprio cliente, reutilizável entre sessões;
- cache global somente para respostas comprovadamente públicas de catálogo/KB, sem memória do cliente, handoff ou escrita;
- memória de longo prazo do cliente como contexto, nunca como resposta pronta.

## Por que multi-agente aqui

Os domínios têm permissões, fontes, modelos e budgets realmente diferentes. O `order_agent` pode alterar apenas estados aprovados; cobrança é somente leitura; produto e suporte usam estratégias de retrieval distintas. Essa separação reduz o blast radius e deixa responsabilidade e custo auditáveis.

## Por que o registry é um documento

A configuração muda com mais frequência que o código e precisa ser inspecionada na demonstração. Um documento permite ativação, troca de modelo, persona e budget de forma atômica, auditada e sem redeploy. A aplicação continua aplicando uma allowlist de ferramentas: documento configura política, mas não ganha capacidade arbitrária.

## Consequências

- A colaboração pode ser consultada por `conversation_id` e agregada por agente.
- Um `conversation_id` só é retomado pelo titular autenticado; IDs alheios ou desconhecidos geram uma nova conversa.
- TTL cuida do ciclo de vida operacional sem jobs externos.
- Em escala maior, revisitaremos particionamento de traces, arquivamento e Change Streams para consumidores assíncronos. Uma fila só será adicionada quando existir trabalho assíncrono real, não para representar handoff síncrono.

