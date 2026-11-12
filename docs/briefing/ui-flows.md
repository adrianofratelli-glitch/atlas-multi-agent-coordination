# UI — telas, fluxos e componentes

> Números de linha podem ficar defasados após mudanças: a fonte atualizada é [MAPA_DO_CODIGO.md](../MAPA_DO_CODIGO.md).

Frontend é React + Vite: `frontend/src/App.jsx` guarda o estado da sessão e a navegação; cada tela e painel é um componente em `frontend/src/components/`, e a lógica pura (grafo, narrativa, capô, custo) mora em `graphModel.js`, `turnNarrative.js`, `mongoOps.js` e `costs.js`, com testes em `frontend/tests/`. 5 abas (`App.jsx::NAV`, contrato de UI: no máximo cinco destinos): **Chat**, **A/B**, **Agentes** (inclui Guardrails), **Decisões**, **Métricas**. Porta 5191, estrita.

Screenshots de referência em `docs/screenshots/`: `01-chat-home.png`, `03-chain-timeline.png`, `04-agents-registry.png`, `05-metrics.png`, `06-guardrails.png` (anteriores à tela de coordenação de 2026-11; serão recapturados com os mesmos nomes). O README também referencia `07-ab-compare.png` (aba A/B) e `08-hitl-approval.png` (escrita pausada), ainda a capturar.

## Barra superior (`App.jsx`, `nav.top-nav`, sempre visível)

- Marca "MongoDB Agent Control Plane" (clicável, volta para Chat).
- Seletor de **identidade** — troca entre 4 clientes fixos de demo (`ana`, `bruno`, `carla`, `diego`), cada um com pedidos/faturas/fidelidade reais no seed.
- Pílula **"ao vivo"** — status da conexão SSE (`GET /api/events/stream`, Change Stream em `agent_handoffs`): `conectando` / `ao vivo` / `offline`, com reconexão automática a cada 3s.
- Pílula de **health** (`GET /api/health`) — mostra qual storage está ativo (Atlas real vs. in-memory).

## Aba Chat (tela padrão, `components/ChatStage.jsx`)

Três colunas que cabem na viewport (a conversa nunca é empurrada para baixo da dobra): **conversa** fixa à esquerda (`ChatPanel.jsx`); no centro, o **grafo compacto** (cerca de 35% da altura da coluna) e, abaixo dele, a narrativa **"Como o time resolveu"** como leitura principal, com rolagem própria; e **Por baixo do capô** à direita. Abaixo de 1240 px o capô vira uma aba ao lado das outras; abaixo de 900 px tudo empilha. Sem rolagem horizontal em 1280, 1440, 1920 e 390 px.

**Fluxo de uma interação**:
1. Cliente digita mensagem (ou carrega um "roteiro rápido" de `GET /api/demo-scenarios`) e envia (`Enter` ou "Enviar turno").
2. `POST /api/chat/stream` roda o turno; cada `TimelineEvent` chega em SSE e redesenha o grafo e o capô na hora.
3. **Grafo ao vivo, compacto** (`AgentGraph.jsx`, derivado por `graphModel.js::deriveTurn`; medidas em `graphModel.js::GRAPH_SIZES`): orquestrador no centro e especialistas de `GET /api/graph/topology` numa elipse achatada em volta (desligados ficam tracejados). Cada nó mostra só o nome e um resumo curto ("2 tools", "aguarda analista", "consolidou"); tools usadas, escritas e passo do plano ficam no tooltip, e o texto nunca passa da borda do nó (o ajuste é feito em unidades do viewBox e testado com os 8 agentes e o sufixo de revisita). Embaixo, uma linha só: o status do turno (texto completo no tooltip) e o caminho em ordem. A legenda fica atrás do botão **"?"** no canto do grafo (Esc ou clique fora fecha), para não roubar altura. O nó ativo pulsa; cada roteamento/handoff desenha a aresta na ordem em que aconteceu (verde = handoff, azul = retorno controlado, lima = despacho paralelo, com duas arestas simultâneas); números azuis são os passos do plano do orquestrador; o selo laranja marca agente que escreveu; a escrita pausada mostra o cadeado "aguardando analista"; a aresta tracejada de volta ao centro é a consolidação. Ao final, `agent_path` do resultado define a ordem exibida. Com `prefers-reduced-motion` não há pulso nem traçado animado.
4. **Como o time resolveu** (visão principal do centro; `TurnNarrative.jsx`, derivado por `turnNarrative.js::narrateTurn` só a partir dos eventos da timeline): etapa por etapa em linguagem simples, em corpo de leitura confortável — guardrail de entrada, roteamento (regra/classificador, com a confiança), plano do orquestrador, cada agente (o que consultou ou gravou e para quem passou a vez), pausa humana, decisão do analista, consolidação, memória e guardrail de saída. Cada etapa traz "Por que importa" e as coleções que tocou. Passar o mouse numa etapa destaca seus nós e arestas no grafo e suas linhas no capô; **clicar em qualquer ponto da etapa** fixa o destaque e rola o capô até as operações dela; clicar no chip de uma coleção leva à linha exata do capô (abaixo de 1240 px, abre a aba Capô).
5. **Por baixo do capô** (`UnderTheHood.jsx`, `mongoOps.js::hoodRows`): uma linha por evento do turno, na ordem em que aconteceu. Operações MongoDB mostram collection, operação e o filtro real (mascarado), com selos para `$vectorSearch`, `$rankFusion`, `$rerank`, `$graphLookup`, transação, escrita no `turn_blackboard`, interrupt em `pending_reviews` e escrita em `agent_handoffs` que dispara o Change Stream. A busca web aparece como **fonte externa** (categoria `web`, op `external`, selo "fora do MongoDB"), nunca como operação MongoDB. Eventos sem coleção (guardrail de saída, coordenação, handoff sem escrita) entram como linhas mais leves, com a categoria e o título, para nada da antiga aba Execução se perder. Cada linha tem **"ver detalhe"** (`EventDetail.jsx`): título do evento, agente, duração, resultado em JSON (mascarado por `redact.js::redactResult`, que preserva contagens de tokens), "Por que estes artigos" no `$rankFusion` e a query/chamada executada (`QueryDetails.jsx`). Passar o mouse numa linha destaca a etapa correspondente na narrativa e os nós no grafo; o vínculo etapa ↔ linhas vem de `step.events` (`turnNarrative.js::stepByEvent` e `mongoOps.js::rowsForStep`). No pé, os eventos que o Change Stream entregou ao navegador nesta sessão.
   Abas secundárias sob a narrativa (barra menor, cada uma com uma linha explicando o que mostra):
   - **Quadro do turno** (`BlackboardView.jsx`): `GET /api/blackboard/latest` depois de cada turno — plano, especialidades detectadas e uma entrada por hop (modo, tools com efeito, escritas, para quem passou). A cadeia de trocas (`ReplacementChain.jsx`, `$graphLookup`) aparece aqui quando o turno a tocou.
   - **Custo** (`CostPanel.jsx::TurnCost`): custo estimado, HIT/MISS da cascata semântica, prompt cache do provedor e `llm_calls` agrupadas por agente (custo desconhecido aparece como "não informado", nunca zero).
   - **Memória** (`TypedMemory.jsx`): `GET /api/memory-typed` — semântica (`customer_memory`), episódica (`long_term_memory`, só rótulos) e procedural (`agent_registry`, `routing_rules`, `hitl_policies`).
   - **Capô** (só abaixo de 1240 px): o mesmo painel da coluna da direita.
   Não há mais aba Execução: o que ela mostrava por evento está no "ver detalhe" do capô. Com `prefers-reduced-motion`, as rolagens de vínculo são instantâneas e as transições de destaque ficam desligadas.
6. **Escrita sensível pausada (HITL)**: quando o resultado traz `awaiting_approval`, a resposta ganha o cartão "Aguardando analista" (review id, tool, argumentos, motivo) e o botão para abrir a fila. Quando o analista decide, o handoff `human_reviewer → agente` chega pelo Change Stream e o chat relê `GET /api/conversations/latest` até a resposta retomada aparecer (marcada "após decisão humana"); se a decisão foi tomada nesta mesma aba, o `resumed_turn` do resolve entra direto.
7. **"Simular queda e retomar do checkpoint"** (`App.jsx::simulateCrash`): abre uma conversa nova e chama `POST /api/demo/crash-resume` (usa o texto da caixa ou uma cadeia pedido → logística → pedido da identidade). O diálogo (`CrashResume.jsx`) mostra a queda, `checkpoint_id`, próximo nó e hops concluídos antes da retomada, o caminho retomado e a conferência em `GET /api/handoffs` de que nenhum handoff foi gravado duas vezes.
8. Chips de **"posso seguir com"** — sugestões de próximo turno, quebrando linha (todas legíveis), cada uma com a mensagem exata que dispara aquele fluxo.
9. A resposta de um turno com mais de um agente vem assinada "Orquestrador · resposta consolidada de N agentes" (o último agente fica no tooltip).

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

**Retomada de sessão**: ao recarregar a página (a identidade fica lembrada no navegador) ou ao trocar de identidade, `GET /api/conversations/latest` traz a última conversa daquele cliente (se houver, dentro da janela de 24h) e repopula mensagens + timeline do último turno; se a conversa tem uma pausa HITL ainda pendente (`awaiting_approval`, lida de `pending_reviews`), o cartão "Aguardando analista" volta na última resposta — para não parecer que nada aconteceu numa sessão retomada.

## Aba A/B (`components/ComparePage.jsx`)

Cartões de cenário por identidade (`compareScenarios.js`), cada um com "o que este cenário testa": pergunta simples (tende a favorecer o agente único), pedido + fatura, plano paralelo (pedido + fatura + pontos, três especialistas ao mesmo tempo), pedido composto (o principal), garantia → catálogo, reembolso com aprovação (no A/B a política HITL vira pausa simulada) e pergunta livre. As mensagens vêm do `GET /api/demo-scenarios` da identidade ou de textos fixos com os ids dela. "Rodar todos em sequência" respeita o limite de 3 por minuto do endpoint (contagem regressiva, pode parar) e monta um resumo cenário × latência/custo/juiz com o braço melhor em verde. O quadro "Como ler o resultado" explica que o agente único ganha em latência e custo em pedidos simples e o time ganha em completude e coerência em pedidos compostos. A mesma pergunta vai para um generalista com todas as tools (o dobro de rodadas, porque cobre o pedido sozinho) e para o time (`POST /api/compare`). Mostra lado a lado resposta, notas do juiz LLM (completude, coerência, grounding) em barras, latência, tokens, custo, agentes e tools. Escritas são simuladas nos dois braços; com modelo real leva de 30 a 90 s, com progresso por etapa. Sem modelo (DEMO_MODE) os braços aparecem como indisponíveis.

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
| `EventDetail.jsx` | "ver detalhe" de uma linha do capô — título, agente, duração, resultado mascarado, fonte externa, `$rankFusion` e query |
| `QueryDetails.jsx` | Query/chamada executada de um evento — filtro Mongo usado (mascarado por `redact.js`) |
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
  servidor (`event: timeline`) e aparece no grafo, na narrativa e no capô na hora; o `event: result` final é o
  mesmo payload do `/api/chat` e substitui a timeline. Sem stream (404/rede), cai para `/api/chat`.
- A tela de Chat cabe na viewport (≥ 900 px de largura): a página não rola; conversa, narrativa/abas e
  capô rolam por dentro, e o capô segue o último item enquanto o turno roda.
- Evento `hybridSearch` do `support_agent` mostra, no "ver detalhe" do capô, "Por que estes artigos" (rank e peso de cada perna
  do `$rankFusion`); `$rerank` do catálogo aparece como evento próprio, com modelo e tempo.
- O feed ao vivo de handoffs aceita `event: resync` (o servidor perdeu o histórico do change stream).
