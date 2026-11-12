# ADR-011 — Agentes com tool calling real, plano do orquestrador, quadro do turno e consolidação

- Status: aceito
- Data: 2026-11-12
- Relacionado: [ADR-001](ADR-001-arquitetura-multi-agente.md), [ADR-005](ADR-005-busca-web-deterministica-governada-pelo-atlas.md), [ADR-012](ADR-012-aprovacao-humana-com-interrupt.md)

## Contexto

Até 2026-11-11 os "agentes" eram funções Python com `if`/palavra-chave; o LLM só redigia o texto em
cima do documento já buscado (`llm_synthesize`), o handoff era decidido por substring
(`"fatura" in message`) e o campo `allowed_tools` do `agent_registry` não era lido por ninguém.
Rodado ao vivo no cenário principal (Ana, defeito → produto → troca → entrega → confirmação), cada
agente respondeu à mensagem inteira e as respostas foram concatenadas: o suporte disse "não
encontrei registro de troca" dois parágrafos antes do agente de pedidos registrar a troca, e o eval
golden (29/29) não viu, porque media rota e contagem de handoffs, não a resposta.

## Decisão

1. **Tool calling real** (`app/tools.py`, `app/agent_loop.py`). Cada especialista recebe tools
   tipadas (JSON Schema) filtradas pelo `allowed_tools` do registry; o modelo escolhe a tool e os
   argumentos, o servidor executa. O LLM continua sem montar query: o executor reconstrói o filtro
   com o `customer_key` do JWT e os argumentos são só ids validados e enums fechados.
2. **O LLM propõe, o servidor dispõe.** Toda tool de escrita passa por três portões: taint de busca
   web no turno (ADR-005), **intenção explícita na mensagem do cliente** (os mesmos detectores que o
   caminho determinístico usava; o modelo não consegue inventar uma escrita que o cliente não pediu)
   e política de aprovação humana (ADR-012). Os mesmos sinais são ditos ao modelo antes, para ele
   não esquecer de efetivar (medido: às vezes só consultava e transferia).
3. **Handoff é tool** (`transfer_to_agent`), com destino validado pelo servidor (registry ativo, sem
   ciclo, `MAX_HOPS`, `ALLOWED_REVISITS`).
4. **Plano do turno (orchestrator-workers)** (`app/planner.py`). Em mensagem composta o orquestrador
   decompõe o pedido em tarefas por especialista (temperature 0, JSON validado contra o registry).
   O primeiro agente continua sendo o da regra determinística (`cheap_route`). Quando um agente
   termina sem transferir e o plano tem passo pendente, o servidor passa a vez (cobertura garantida).
5. **Quadro do turno** (`app/blackboard.py`, collection `turn_blackboard`, TTL 24 h). Cada hop grava
   tarefa, tools, escritas e resposta. O próximo agente recebe **só fatos gerados pelo servidor**
   (tools e escritas efetivadas) — nunca o texto livre de outro modelo, que seria prompt injection
   indireta (`tests/adversarial/test_inter_agent_injection_adversarial.py`). A tarefa vinda de
   `transfer_to_agent` passa por `safe_task` (curta, sem cara de instrução, PII mascarada).
6. **Consolidação** (`app/consolidate.py`). O orquestrador escreve uma resposta única a partir do
   quadro: ordem do pedido do cliente, só fatos do quadro, escrita efetivada prevalece sobre frase
   que a contradiga. Sem modelo, junta as partes (comportamento anterior).
7. **Um hop por super-step do StateGraph** (`chain_prepare → agent_hop → advance/approval →
   consolidate → finalize`): cada hop tem checkpoint no Atlas, o que habilita a pausa do ADR-012 e a
   retomada após queda (`POST /api/demo/crash-resume`).
8. **Sem modelo, nada muda**: sem cliente Anthropic (DEMO_MODE/CI), com o breaker aberto ou com
   `AGENT_TOOL_CALLING=0`, o runner determinístico do agente responde, com o quadro no prompt.

## Medição (2026-11-12, banco `*_test`, Grove)

| Cenário principal (Ana) | Antes | Depois |
|---|---|---|
| Resposta | 5 parágrafos concatenados, com contradição | uma resposta, todas as partes, sem contradição |
| Custo estimado / turno | ~US$ 0,04 | ~US$ 0,22–0,27 |
| Chamadas ao modelo | 4 | 10–14 |

A/B na mesma pergunta (`POST /api/compare`, escritas simuladas, mesmas tools e modelo): o agente
único levou ~9 s e ~US$ 0,06; o time levou ~40 s e ~US$ 0,27. É o argumento do Cognition ("Don't
build multi-agents") medido na PoV, e a tela A/B mostra isso sem esconder. O que o time compra: escrita
restrita por agente (`allowed_tools`), orçamento por agente, trilha por hop no quadro, pausa e
retomada por checkpoint e plano auditável. Paralelizar passos independentes do plano é o próximo
passo de latência.

## Alternativas descartadas

- **Passar a resposta de um agente ao próximo** (o "share full context" do Cognition): abre prompt
  injection entre agentes; os fatos do servidor bastam para não contradizer.
- **Deixar o modelo decidir o primeiro agente**: o roteamento determinístico confiante é invariante
  (reprodutibilidade, custo zero).
- **Supervisor que chama todo especialista via tool**: um hop a mais por agente e o histórico do
  supervisor cresce com todas as observações.

## Passos independentes em paralelo (2026-11-12, mesma data)

O plano passou a trazer dependências (`depends_on` → `after`, validadas: só passo anterior; sem a informação,
depende de todos os anteriores). Passos de LEITURA sem dependência pendente rodam juntos num mesmo super-step
(`asyncio.gather`, cada um com seu ToolContext), já no 1º hop se nenhum depender do agente de abertura. Agente que
pode escrever só entra no lote se a mensagem não pede escrita dele (os mesmos sinais dos portões) e roda com
escritas retidas; escrita nunca é paralela ("writes stay single-threaded"). Cada agente do lote grava sua entrada
no quadro e seu handoff (o change stream mostra os despachos simultâneos). Medido ao vivo: "status do PED-1003 +
fatura + pontos" com os três especialistas juntos em 27,9 s; o cenário principal continua sequencial porque o plano
declara a dependência (diagnóstico → recomendação → troca → entrega da reposição) — paralelizá-lo seria errado.

A busca web voltou a ser decisão do servidor também no tool calling (ADR-005): ler uma entrega vencida com a
ferramenta ligada dispara a busca uma vez por turno (o modelo, sozinho, nunca chamava `search_carrier_news`);
pedido explícito de reagendamento tem prioridade e não busca; as fontes entram na resposta final pelo servidor.
