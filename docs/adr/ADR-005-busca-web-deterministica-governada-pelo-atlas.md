# ADR-005 — Busca web como recuperação determinística, governada pelo Atlas

- Status: aceito
- Data: 2026-10-13
- Relacionado: [ADR-004](ADR-004-escopo-por-embedding-e-medicao-por-situacoes.md) (fora de escopo por embedding, 0 tokens — **não muda**), [ADR-002](ADR-002-memoria-llm-e-turno-pessoal.md) (turno pessoal fora do cache), [ADR-001](ADR-001-arquitetura-multi-agente.md)

## Contexto

A PoV irmã de agente único ganhou busca web (Tavily) com um loop de ferramentas: o modelo decide buscar,
escolhe a consulta e responde "qualquer pergunta". A primeira proposta foi portar o mesmo desenho para cá — um
`web_agent` com loop próprio que respondesse tudo o que hoje é recusado como fora de escopo.

Avaliado contra o objetivo desta PoV, o desenho foi recusado:

1. **Diluiria a tese medida.** Fora de escopo decidido por `$vectorSearch` com 0 tokens (ADR-004) é o argumento
   de custo mais claro da PoV. Responder clima, dólar e futebol trocaria esse 0 por classificador de segurança +
   loop de LLM + 3–5 s + crédito do provedor em todo fora de escopo.
2. **Repetiria a PoV irmã** em vez de contrastar com ela: lá, um agente autônomo com loop; aqui, orquestração
   determinística de especialistas.
3. **Atendimento de varejo não quer opinar sobre o jogo de ontem.** Recusar bem fora do domínio é uma qualidade.
4. **Quebraria o invariante** "dado é recuperado de forma determinística; o LLM só redige" — sem ganho que pague.

Dois fatos medidos no código mostraram o que de fato faltava, e não era web aberta:

- **Nenhum prompt sabia a data.** Os envios da demo têm previsão em julho de 2026; em outubro, o PED-1001 aparecia
  "em trânsito" com a previsão vencida havia 86 dias e nada no sistema percebia.
- **Garantia por `months * 30`.** "12 meses" virava 360 dias e errava a borda do prazo.

O catálogo da demo é fictício ("Fone Pulse X"): buscar recall ou ficha técnica não acharia nada. As
transportadoras (Correios, Loggi, Jadlog) são reais.

## Decisão

### 1. Relógio e contas de calendário em Python (`app/clock.py`)

- Data, hora e fuso (America/Sao_Paulo) no contexto **dinâmico** dos agentes cujo documento tem prazo
  (`agents.CLOCK_AGENTS`: pedidos, cobrança, garantia, logística — produto, suporte e fidelidade não recebem), nunca
  no `GROUNDING_RULES` estático — que perderia o prompt cache. Uma linha curta (o orçamento por agente é 4.000
  tokens e já causou 429).
- Toda distância entre datas chega pronta no documento: `warranty_agent` → `expires_at` por mês de calendário e
  `days_left`; `logistics_agent` → `days_until_delivery` e `delivery_overdue`. O modelo não conta dias (medido na
  PoV irmã: "72 dias até o Natal" em 13/10; são 73).
- Resposta que cita a data de hoje não entra em nenhuma camada de cache (`clock.uses_current_date`, regra pela
  resposta, não por lista de palavras da pergunta).

### 2. Busca web só onde ela tem dado real, decidida pelo servidor

- **Gatilho determinístico:** `logistics_agent`, entrega com `delivery_overdue` **e** ferramenta ligada para a área.
  Sem classificador novo, sem lista de palavras, sem LLM decidindo.
- **Consulta montada pelo servidor** a partir do documento: `"<transportadora> atraso entregas Brasil"`. O texto do cliente
  nunca sai do processo; ainda assim a consulta passa por `mask_pii` e é truncada antes de sair.
- O LLM redige sobre o resultado como redige sobre qualquer documento do MongoDB. O resultado entra entre
  `<resultados_web_nao_confiaveis>`, com a hora atual e a ordem de ignorar comandos, citar a URL e avisar quando a
  fonte contradiz o momento atual. Prazo e status vêm sempre do MongoDB, nunca da web.
- **A fonte é citada pelo servidor:** se a resposta com busca não traz link, o servidor anexa as fontes. Medido ao
  vivo: o modelo às vezes resumia as fontes ("são antigas, não confirmam nada") sem citar nenhuma.
- **Fora de escopo não muda:** clima, dólar e futebol seguem recusados com 0 tokens (ADR-004). O roteador não
  ganhou chave nova.

### 3. O Atlas governa a ferramenta (`app/web_search.py`)

| Peça | Onde | Regra |
|---|---|---|
| Registro | `multiagent_brain.tool_registry` (`_id: web_search`) | `enabled`, `disabled_areas`, `monthly_limit`, `max_results`, `cache_ttl_seconds`, `search_depth`. Só restringe; ausente, ilegível, `DEMO_MODE` ou sem `TAVILY_API_KEY` = ferramenta não existe. Liga/desliga por `update_one`, efeito no turno seguinte. |
| Cota mensal | `multiagent_brain.web_search_quota` (`_id: AAAA-MM`) | `find_one_and_update` com `used < limit` + upsert (`DataStore.reserve_quota`); `DuplicateKeyError` = cota estourada, o provedor nem é chamado. |
| Cache de resultado | `web_search_cache` | consulta normalizada, TTL de 6 h em `expires_at`. Match **exato**: cache semântico do resultado web foi medido e descartado na PoV irmã (perguntas diferentes pontuam acima da reescrita mais fraca da mesma pergunta). |
| Auditoria | `external_calls` | consulta mascarada, URLs, latência, cache hit, cota, erro; TTL 30 dias. |

Seed: `financeiro` fica sem web de propósito, para mostrar o controle por área. A chave Tavily é **uma** para o
portfólio (1.000 créditos/mês): esta PoV fica com 400, a PoV irmã com 500.

### 4. Resiliência

Toda chamada passa por `resilience.call_tool("web_search")` (span, caos, circuit breaker) com timeout HTTP próprio
de 15 s. Timeout, circuito aberto, HTTP do provedor, payload fora do formato ou cota estourada viram resultado
degradado honesto: o agente responde com o documento do MongoDB e não supõe nada sobre a transportadora.

### 5. Taint do turno

Depois que um resultado web entra no turno, nenhum agente de `WRITE_EFFECT_AGENTS` roda — nem por handoff. O
cliente confirma numa nova mensagem. Aqui o risco é estruturalmente menor que na PoV irmã (a escrita é decidida em
Python a partir da mensagem do cliente, e o resultado web nunca chega a um agente que escreve), mas a regra existe
para que a segurança nunca dependa disso. Escrita no mesmo hop da busca não existe: reagendamento é um ramo antes
da busca.

### 6. Cache semântico e UI

- Turno com evento `op: "external"` não grava em nenhuma camada (nem curto prazo de 24 h, que serviria um
  resultado mais velho que o TTL do próprio resultado).
- `TimelineEvent` ganhou `category: "web"` e `op: "external"`; a timeline mostra provedor, consulta, fontes e
  "cache Atlas" no acerto, sem o selo MongoDB. Links da resposta são clicáveis só para `http(s)`, montados como nó
  React (nunca `innerHTML`).

### 7. Achado da bateria ao vivo: continuação não é "fora de escopo"

Numa mensagem mista, a lista de palavras (`router.out_of_scope_sentences`) marcava "quantos dias faltam?" como
alheia, e a resposta da garantia — que já trazia os 270 dias — terminava com "isso foge do que eu resolvo". Agora a
lista só **propõe**; o classificador por embedding do ADR-004 **confirma** cada frase, e só `out` decisivo vira
apêndice. Medido no cérebro de teste: continuações curtas ("quantos dias faltam", "e quanto custa", "ainda dá
tempo") saem `unsure`/`in` (margem ≤ 0,014); temas alheios ("vai chover amanhã", "cotação do dólar", "quem ganhou o
jogo") saem `out` com margem ≥ 0,077. Sem veredito real, vale a lista, como antes.

## Alternativas descartadas

- **`web_agent` com loop de ferramentas para qualquer pergunta:** ver Contexto.
- **4º rótulo `adjacent` no `scope_probes`:** mexeria no gate medido do ADR-004 (holdout 98,6%) para um caso de uso
  que não existe com catálogo fictício.
- **Recall/ficha técnica de produto:** catálogo fictício, busca vazia.
- **Busca nativa do provedor do LLM:** o gateway não aceita as ferramentas nativas de busca.
- **Carimbo `web_enabled` nas entradas do cache:** a recusa de fora de escopo não passa pelo cache e turno com web
  não grava; seria código sem caminho que o exercite.

## Consequências

- Invariante preservado: dado do MongoDB é recuperado de forma determinística e o LLM só redige. A web é mais uma
  fonte de recuperação determinística, com a consulta montada pelo servidor.
- A demo ganha um beat honesto: "sua entrega venceu há 86 dias; há notícia pública de atraso na transportadora
  [fonte]; quer reagendar?" — e três pontos de MongoDB: ferramenta por `update_one`, crédito poupado por TTL,
  auditoria por agregação.
- O backend passa a depender de saída para a internet só nesse caminho; rede fora vira resposta só com o MongoDB.
- Custo de busca: no máximo uma por transportadora a cada 6 h por consulta normalizada.
- **Custo de tokens do relógio (medido):** `eval_routing.py --live`, 24 casos, cache semântico quente nas três
  configurações: sem relógio ~1.366 tokens/turno (3 rodadas, ±6); relógio só nos agentes de data ~1.434 (+5%, 2
  rodadas); relógio em todos 1.461 (+7%). Rota e resolução em 100% em todas. Uma primeira leitura comparou uma
  rodada de cache frio (logo após o reset do banco de teste) com uma de cache quente e chegou a "+23%": o salto era
  do cache, não do relógio. Turno com busca web fica em ~2.400–2.800 dos 4.000 de orçamento do `logistics_agent`.
