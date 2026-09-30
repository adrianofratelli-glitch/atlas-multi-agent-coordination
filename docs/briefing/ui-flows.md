# UI — telas, fluxos e componentes

Frontend é React + Vite, arquivo único principal `frontend/src/App.jsx` (441 linhas) + componentes em `frontend/src/components/`. 3 abas de navegação (`NAV` em `App.jsx:8`): **Chat**, **Decisões**, **Métricas**. Porta 5191, estrita.

Screenshots de referência em `docs/screenshots/`: `01-chat-home.png`, `03-chain-timeline.png`, `04-agents-registry.png`, `05-metrics.png`, `06-guardrails.png`.

## Barra superior (`App.jsx:384`, sempre visível)

- Marca "MongoDB Agent Control Plane" (clicável, volta para Chat).
- Seletor de **identidade** — troca entre 4 clientes fixos de demo (`ana`, `bruno`, `carla`, `diego`), cada um com pedidos/faturas/fidelidade reais no seed.
- Pílula **"ao vivo"** — status da conexão SSE (`GET /api/events/stream`, Change Stream em `agent_handoffs`): `conectando` / `ao vivo` / `offline`, com reconexão automática a cada 3s.
- Pílula de **health** (`GET /api/health`) — mostra qual storage está ativo (Atlas real vs. in-memory).

## Aba Chat (tela padrão)

Componentes: `ChatPanel` (`App.jsx:70-120`), `MongoCacheSavings` (`App.jsx:18-68`), `Timeline` (`components/Timeline.jsx`), `ReplacementChain` (`components/ReplacementChain.jsx`).

**Fluxo de uma interação**:
1. Cliente digita mensagem (ou carrega um "roteiro rápido" — cenário de demo pré-carregado de `GET /api/demo-scenarios`) e envia (`Enter` ou botão "Enviar turno").
2. `POST /api/chat` roda o turno completo no backend (ver `agent-behavior.md`).
3. Resposta chega com: texto final, `active_agent`, timeline completa, badge de cache (`hit`/`miss` + tokens), sugestões de próximo passo.
4. **Resumo do turno** (`MongoCacheSavings`, dropdown `<details>`): custo estimado USD, se foi cache hit/miss, quantas chamadas de LLM, execução paralela vs. sequencial, consumo por chamada, prompt cache do provedor (cache_read/cache_write tokens).
5. **Elenco de agentes** (`agent-cast`): pills mostrando quais agentes atuaram no turno, em ordem, com seta "chamou →" indicando handoff (tooltip mostra o motivo do handoff) ou "+ (paralelo)" para fan-out.
6. **Coleções consultadas** (`collections-panel`, dropdown): cada collection tocada no turno com badges de operação (leitura / escrita / `$vectorSearch` / híbrido BM25+vetor / `$graphLookup` / change stream) — é a prova visual, turno a turno, de que tudo passa pelo MongoDB.
7. **Cadeia de reposição** (`ReplacementChain`) — aparece só quando o turno tocou `$graphLookup` (pedido trocado 2+ vezes), mostra visualmente a cadeia de pedidos ligados.
8. **Painel de Execução** (`Timeline`) — lista cronológica de todo evento do turno: guardrail, roteamento, cache, cada hop de agente, handoff, memória, com filtro/detalhe por evento (`QueryDetails.jsx` expõe filtro Mongo e resultado bruto de cada operação).
9. Chips de **"posso seguir com"** — sugestões de próximo turno, cada uma pré-populada com a mensagem exata que dispara aquele fluxo (nunca leva a "não encontrei").

**Roteiros de demo por identidade** (`GET /api/demo-scenarios`, ordenados por `position`): além dos cenários multiagente, cada uma das 4 identidades tem 6 chips que exercitam as três camadas de memória e o cache (`seed_data.py:MEMORY_DEMOS`, campo `demo_kind`):

| Chip | O que prova |
|---|---|
| ⚡ Cache semântico | Pergunta genérica já aquecida — 1º clique é HIT, com tokens economizados |
| 🕐 Curto prazo 1/2 e 2/2 | Pergunta e depois a reformulação na MESMA conversa → HIT em `short_term_memory` |
| 🧠 Longo prazo: gravar | "me chame de X e nunca me ofereça acima de R$ N" → fatos em `customer_memory` |
| 🧠 Longo prazo: usar | Recomendação respeitando o teto lido da memória (pré-filtro no `$vectorSearch`) |
| 🧠 Longo prazo: atualizar | Novo teto → supersessão do fato antigo, recomendação muda |

A ordem importa (o que um chip escreve o seguinte usa), por isso eles ficam **fora** do golden eval. A reformulação do curto prazo depende do índice vetorial, que o Atlas atualiza de forma assíncrona: se o 2º clique vier em segundos e errar, clicar de novo acerta.

Um clique em **"Nova conversa"** zera `conversation_id`, mensagens, timeline e métricas locais. **"Reiniciar memória da demo"** (`POST /api/demo/reset`) vai além: desfaz o que a demo gravou NESTE cliente (fatos extraídos, episódios, curto prazo, cache do cliente) e reativa o que ela substituiu — o roteiro inteiro pode ser repetido sem a segunda rodada parecer que "não aconteceu nada" por deduplicação. Não toca no cache global aquecido nem em nenhum outro cliente.

**Retomada de sessão**: ao trocar de identidade (ou no boot), `GET /api/conversations/latest` traz a última conversa daquele cliente (se houver, dentro da janela de 24h) e repopula mensagens + timeline do último turno — para não parecer que nada aconteceu numa sessão retomada.

## Aba Métricas (`MetricsPage`, `App.jsx:154-230`)

Cartões executivos, não payload técnico bruto:
- turnos concluídos, agentes exercitados (N/7), handoffs (+ retornos controlados), escritas de negócio, operações nativas (Vector Search/`$rankFusion`/`$graphLookup`), latência p95, cache hit rate, tokens economizados, guardrails bloqueados.
- **Ledger por collection** — tabela `collection × {read, write, vector, hybrid, graph}`, populada em tempo real a cada turno.
- Cartão de guardrail com três estados: aprovado, **BLOQUEADO** (segurança) e **🧭 Guardrail de escopo** (pergunta fora do domínio — não é ataque, e o painel não a apresenta como tal).
- **Painel de qualidade (GoalSuccessRate)** — histórico de `python eval.py` contra o golden dataset (`eval_runs`), só visível em modo admin: taxa de sucesso, p95, USD por sucesso.
- `<details>` "Ver payload técnico" — dump JSON cru de `GET /api/metrics` para quem quiser o número exato.

## Aba Decisões / Governança (`CompliancePage.jsx`)

"O agente para quando não deve decidir sozinho" — superfície de conformidade:
- Trilha de decisões (`agent_decisions`) e eventos de auditoria (`agent_audit_events`) — via `GET /api/decisions`.
- Fila de revisão humana (`pending_reviews`) — casos pausados (ex.: defeito recorrente detectado pelo `$graphLookup`) aguardando decisão de analista; resolução via `POST /api/admin/reviews/{id}/resolve` (admin only, `X-Admin-Key`).
- Candidatos de guardrail (`guardrail_candidates`) para aprovação manual de novas entradas na denylist.
- Modo admin (mesmo toggle usado na antiga tela de Agentes) — expõe ações de escrita administrativa.

## Tela de Agentes (registry) — acessível dentro do fluxo de admin

`AgentsPage` (`App.jsx:122-148`) — não está mais em `NAV` direto, mas o componente existe e é montado a partir da mesma base: cada um dos agentes reais é um card com modelo, persona, budget de tokens e tools, editável em tempo real via `PATCH /api/admin/agents/{agent_key}` quando modo admin está ligado (ativar/desativar um agente sem redeploy). É a prova de "config é dado, não código" — `multiagent_brain.agent_registry`.

## Componentes de suporte

| Componente | Papel |
|---|---|
| `Timeline.jsx` | Renderiza a lista cronológica de `TimelineEvent`s do turno |
| `QueryDetails.jsx` | Detalhe expandido de um evento — filtro Mongo usado, resultado, duração |
| `ReplacementChain.jsx` | Visualização da cadeia de `$graphLookup` (pedido → reposição → reposição...) |
| `AiBrainPanel.jsx` | Painel de introspecção do "cérebro" do sistema (registry/routing rules) |
| `CompliancePage.jsx` | Aba Decisões — decisões, auditoria, revisões pendentes, candidatos de guardrail |

## Como validar visualmente

```bash
cd backend && python run.py            # porta 8031
cd frontend && npm install && npm run dev   # porta 5191
```

O cache **se aquece sozinho**: o backend dispara ao subir e o frontend dispara de novo ao abrir a página (`POST /api/warmup`, execução única por vez, no máximo uma a cada 45 min). `python backend/warmup.py <url>` só força e acompanha o estado; `WARMUP_ON_START=0` desliga.
