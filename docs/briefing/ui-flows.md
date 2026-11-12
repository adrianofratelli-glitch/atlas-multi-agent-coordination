# UI — telas, fluxos e componentes

> Números de linha podem ficar defasados após mudanças: a fonte atualizada é [MAPA_DO_CODIGO.md](../MAPA_DO_CODIGO.md).

Frontend é React + Vite: `frontend/src/App.jsx` guarda o estado da sessão e a navegação; cada tela e painel é um componente em `frontend/src/components/`, e a lógica pura (grafo, capô, custo) mora em `graphModel.js`, `mongoOps.js` e `costs.js`, com testes em `frontend/tests/`. 5 abas (`App.jsx::NAV`, contrato de UI: no máximo cinco destinos): **Chat**, **A/B**, **Agentes** (inclui Guardrails), **Decisões**, **Métricas**. Porta 5191, estrita.

Screenshots de referência em `docs/screenshots/`: `01-chat-home.png`, `03-chain-timeline.png`, `04-agents-registry.png`, `05-metrics.png`, `06-guardrails.png` (anteriores à tela de coordenação de 2026-11; serão recapturados com os mesmos nomes). O README também referencia `07-ab-compare.png` (aba A/B) e `08-hitl-approval.png` (escrita pausada), ainda a capturar.

## Barra superior (`App.jsx`, `nav.top-nav`, sempre visível)

- Marca "MongoDB Agent Control Plane" (clicável, volta para Chat).
- Seletor de **identidade** — troca entre 4 clientes fixos de demo (`ana`, `bruno`, `carla`, `diego`), cada um com pedidos/faturas/fidelidade reais no seed.
- Pílula **"ao vivo"** — status da conexão SSE (`GET /api/events/stream`, Change Stream em `agent_handoffs`): `conectando` / `ao vivo` / `offline`, com reconexão automática a cada 3s.
- Pílula de **health** (`GET /api/health`) — mostra qual storage está ativo (Atlas real vs. in-memory).

## Aba Chat (tela padrão, `components/ChatStage.jsx`)

Três colunas que cabem na viewport (a conversa nunca é empurrada para baixo da dobra): **conversa** fixa à esquerda (`ChatPanel.jsx`), **grafo ao vivo** no centro com abas de detalhe embaixo, e **Por baixo do capô** à direita. Abaixo de 1240 px o capô vira uma aba; abaixo de 900 px tudo empilha.

**Fluxo de uma interação**:
1. Cliente digita mensagem (ou carrega um "roteiro rápido" de `GET /api/demo-scenarios`) e envia (`Enter` ou "Enviar turno").
2. `POST /api/chat/stream` roda o turno; cada `TimelineEvent` chega em SSE e redesenha o grafo e o capô na hora.
3. **Grafo ao vivo** (`AgentGraph.jsx`, derivado por `graphModel.js::deriveTurn`): orquestrador no centro e especialistas de `GET /api/graph/topology` em volta (desligados ficam tracejados). O nó ativo pulsa; cada roteamento/handoff desenha a aresta na ordem em que aconteceu (verde = handoff, azul = retorno controlado, lima = despacho paralelo, com duas arestas simultâneas); números azuis são os passos do plano do orquestrador; o selo laranja marca agente que escreveu; a escrita pausada mostra o cadeado "aguardando analista"; a aresta tracejada de volta ao centro é a consolidação. Ao final, `agent_path` do resultado define a ordem exibida. Com `prefers-reduced-motion` não há pulso nem traçado animado.
4. **Por baixo do capô** (`UnderTheHood.jsx`, `mongoOps.js`): uma linha por operação MongoDB do turno (collection, operação e o filtro real, mascarado), com selos para `$vectorSearch`, `$rankFusion`, `$rerank`, `$graphLookup`, transação, escrita no `turn_blackboard`, interrupt em `pending_reviews` e escrita em `agent_handoffs` que dispara o Change Stream. No pé, os eventos que o Change Stream entregou ao navegador nesta sessão.
5. Abas sob o grafo:
   - **Quadro do turno** (`BlackboardView.jsx`): `GET /api/blackboard/latest` depois de cada turno — plano, especialidades detectadas e uma entrada por hop (modo, tools com efeito, escritas, para quem passou). A cadeia de trocas (`ReplacementChain.jsx`, `$graphLookup`) aparece aqui quando o turno a tocou.
   - **Execução** (`Timeline.jsx`): a timeline completa, com `QueryDetails.jsx` em cada evento. A busca web aparece como **fonte externa** (categoria `web`, op `external`), sem o selo MongoDB.
   - **Custo** (`CostPanel.jsx::TurnCost`): custo estimado, HIT/MISS da cascata semântica, prompt cache do provedor e `llm_calls` agrupadas por agente (custo desconhecido aparece como "não informado", nunca zero).
   - **Memória** (`TypedMemory.jsx`): `GET /api/memory-typed` — semântica (`customer_memory`), episódica (`long_term_memory`, só rótulos) e procedural (`agent_registry`, `routing_rules`, `hitl_policies`).
6. **Escrita sensível pausada (HITL)**: quando o resultado traz `awaiting_approval`, a resposta ganha o cartão "Aguardando analista" (review id, tool, argumentos, motivo) e o botão para abrir a fila. Quando o analista decide, o handoff `human_reviewer → agente` chega pelo Change Stream e o chat relê `GET /api/conversations/latest` até a resposta retomada aparecer (marcada "após decisão humana"); se a decisão foi tomada nesta mesma aba, o `resumed_turn` do resolve entra direto.
7. **"Simular queda e retomar do checkpoint"** (`App.jsx::simulateCrash`): abre uma conversa nova e chama `POST /api/demo/crash-resume` (usa o texto da caixa ou uma cadeia pedido → logística → pedido da identidade). O diálogo (`CrashResume.jsx`) mostra a queda, `checkpoint_id`, próximo nó e hops concluídos antes da retomada, o caminho retomado e a conferência em `GET /api/handoffs` de que nenhum handoff foi gravado duas vezes.
8. Chips de **"posso seguir com"** — sugestões de próximo turno, numa linha rolável, cada uma com a mensagem exata que dispara aquele fluxo.

**Roteiros de demo por identidade** (`GET /api/demo-scenarios`, ordenados por `position`): além dos cenários multiagente, cada uma das 4 identidades tem 6 chips que exercitam as três camadas de memória e o cache (`seed_data.py:MEMORY_DEMOS`, campo `demo_kind`):

| Chip | O que prova |
|---|---|
| ⚡ Cache semântico | Pergunta genérica já aquecida — 1º clique é HIT, com tokens economizados |
| 🕐 Curto prazo 1/2 e 2/2 | Pergunta e depois a reformulação na MESMA conversa → HIT em `short_term_memory` |
| 🧠 Longo prazo: gravar | "me chame de X e nunca me ofereça acima de R$ N" → fatos em `customer_memory` |
| 🧠 Longo prazo: usar | Recomendação respeitando o teto lido da memória (pré-filtro no `$vectorSearch`) |
| 🧠 Longo prazo: atualizar | Novo teto → supersessão do fato antigo, recomendação muda |

A ordem importa (o que um chip escreve o seguinte usa), por isso eles ficam **fora** do golden eval. A reformulação do curto prazo depende do índice vetorial, que o Atlas atualiza de forma assíncrona: se o 2º clique vier em segundos e errar, clicar de novo acerta.

Um clique em **"Nova conversa"** zera `conversation_id`, mensagens, timeline e métricas locais. **"Reiniciar memória da demo"** (`POST /api/demo/reset`) vai além: desfaz o que a demo gravou NESTE cliente (fatos extraídos, episódios, curto prazo, cache do cliente) e reativa o que ela substituiu — o roteiro inteiro pode ser repetido sem a segunda rodada parecer que "não aconteceu nada" por deduplicação. Não toca no cache global aquecido nem em nenhum outro cliente. Fica liberado no banco da demo de propósito (é o botão usado ao vivo, de escopo estreito) e tem limite de requisições; o reset completo do ambiente é `backend/scripts/reset_demo.py`.

**Retomada de sessão**: ao trocar de identidade (ou no boot), `GET /api/conversations/latest` traz a última conversa daquele cliente (se houver, dentro da janela de 24h) e repopula mensagens + timeline do último turno — para não parecer que nada aconteceu numa sessão retomada.

## Aba A/B (`components/ComparePage.jsx`)

A mesma pergunta (pré-preenchida com o cenário multiagente mais longo da identidade) para um generalista com todas as tools e para o time (`POST /api/compare`). Mostra lado a lado resposta, notas do juiz LLM (completude, coerência, grounding) em barras, latência, tokens, custo, agentes e tools. Escritas são simuladas nos dois braços; com modelo real leva de 30 a 90 s, com progresso por etapa. Sem modelo (DEMO_MODE) os braços aparecem como indisponíveis.

## Aba Agentes (`components/AgentsPage.jsx`)

- **Time e tools**: cada documento de `multiagent_brain.agent_registry` vira um cartão com ativo/inativo, modelo editável, budget e as tools do catálogo (`GET /api/tools`) com efeito (leitura, escrita, externa) e liga/desliga — tudo por `PATCH /api/admin/agents/{agent_key}` em modo admin. Abaixo, as políticas de aprovação humana (`GET`/`PATCH /api/admin/hitl-policies`).
- **Guardrails** (`GuardrailsPanel.jsx`, admin): eventos, candidatos e denylist (`GET /api/guardrails/{view}`). A aprovação de candidato segue só pela API: a listagem não devolve o `_id` que o endpoint de aprovação exige.

## Aba Decisões / Governança (`CompliancePage.jsx`)

- Trilha de decisões (`agent_decisions`) e eventos de auditoria (`agent_audit_events`) — via `GET /api/decisions`.
- Fila de revisão humana (`pending_reviews`, admin). Casos de defeito recorrente mantêm os encaminhamentos de qualidade; uma **escrita pausada** (`evidence.kind = hitl_interrupt`) mostra a escrita pedida e só "Aprovar escrita" / "Recusar". Ao decidir, `POST /api/admin/reviews/{id}/resolve` retoma o mesmo turno do checkpoint e a resposta retomada aparece no topo da fila e no chat do cliente.
- Modo admin (o mesmo das abas Agentes e Métricas) — expõe ações administrativas.

## Aba Métricas (`components/MetricsPage.jsx`)

- Cartões executivos: turnos, agentes exercitados, handoffs, escritas de negócio, operações nativas, latência p95, cache hit rate, tokens economizados, guardrails.
- **Custo por agente e economia do cache** (`CostPanel.jsx::EconomicsPanel`, `GET /api/economics?hours=N`, janelas 1 h / 6 h / 24 h / 7 dias): HIT × MISS, custo e tokens por agente e custo por hora (com tabela).
- **Ledger por collection** e **qualidade (GoalSuccessRate)** do `eval_runs` (admin), mais o payload técnico de `GET /api/metrics`.

## Componentes de suporte

| Componente | Papel |
|---|---|
| `Timeline.jsx` | Renderiza a lista cronológica de `TimelineEvent`s do turno |
| `QueryDetails.jsx` | Detalhe expandido de um evento — filtro Mongo usado (mascarado por `redact.js`) |
| `ReplacementChain.jsx` | Visualização da cadeia de `$graphLookup` (pedido → reposição → reposição...) |
| `RichText.jsx`, `PageHead.jsx`, `Icons.jsx` | Texto do modelo sem HTML cru, cabeçalho único das abas, ícones SVG |

## Como validar visualmente

```bash
cd backend && python run.py            # porta 8031
cd frontend && npm install && npm run dev   # porta 5191
```

O cache **se aquece sozinho**: o backend dispara ao subir e o frontend dispara de novo ao abrir a página (`POST /api/warmup`, execução única por vez, no máximo uma a cada 45 min). `python backend/warmup.py <url>` só força e acompanha o estado; `WARMUP_ON_START=0` desliga.

## Turno ao vivo e tela única (2026-10)

- `Enviar turno` chama `POST /api/chat/stream`: cada `TimelineEvent` chega em SSE quando nasce no
  servidor (`event: timeline`) e aparece na coluna Execução na hora; o `event: result` final é o
  mesmo payload do `/api/chat` e substitui a timeline. Sem stream (404/rede), cai para `/api/chat`.
- A tela de Chat cabe na viewport (≥ 900 px de largura): a página não rola; conversa, abas e capô
  rolam por dentro e seguem o último item enquanto o turno roda.
- Evento `hybridSearch` do `support_agent` mostra "Por que estes artigos" (rank e peso de cada perna
  do `$rankFusion`); `$rerank` do catálogo aparece como evento próprio, com modelo e tempo.
- O feed ao vivo de handoffs aceita `event: resync` (o servidor perdeu o histórico do change stream).
