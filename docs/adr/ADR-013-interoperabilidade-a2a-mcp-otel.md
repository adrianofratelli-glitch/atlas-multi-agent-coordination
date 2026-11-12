# ADR-013 — Interoperabilidade: agent cards A2A, servidor MCP somente leitura e spans GenAI do OTel

- Status: aceito
- Data: 2026-11-12
- Relacionado: [ADR-011](ADR-011-tool-calling-plano-quadro-e-consolidacao.md)

## Decisão

1. **A2A 1.0** (`app/agent_cards.py`): `GET /.well-known/agent-card.json` (card do serviço
   coordenado, uma skill por especialista ativo) e `GET /api/agents/{agent_key}/card` (skills = tools
   liberadas no registry). O card é gerado do `agent_registry` a cada requisição: desligar um agente o
   tira do card. Campos camelCase do 1.0 (`supportedInterfaces` com `protocolBinding: HTTP+JSON` e
   `protocolVersion: "1.0"`, `securitySchemes` com bearer JWT); sem modelo, orçamento ou config de
   retrieval no card.
2. **MCP** (`backend/mcp_server.py`, SDK `mcp` 2.3): expõe por stdio as tools de **leitura** dos
   especialistas e dois recursos (`coordination://handoffs/recent`, `coordination://blackboard/latest`),
   filtrados pelo registry a cada chamada. Identidade só do JWT (`MCP_CUSTOMER_TOKEN`), revalidado em
   toda chamada; sem token o servidor não sobe. Tools de escrita ficam de fora: o portão delas exige o
   pedido na mensagem do próprio cliente, e um cliente MCP é outro agente.
3. **OpenTelemetry GenAI** (status "Development" da convenção): `invoke_agent {agente}`,
   `execute_tool {tool}` (filho do agente) e `chat {modelo}` com `gen_ai.operation.name`,
   `gen_ai.agent.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.usage.*`; o
   `TRACE_SINK=atlas` grava esses spans como documentos.

## Fora do escopo

Atlas Agent Engine (Public Preview em 2026-09) vai ser explorado numa PoV própria; esta PoV mostra
por baixo do capô o que um runtime gerenciado empacota (memória, governança, tracing).
