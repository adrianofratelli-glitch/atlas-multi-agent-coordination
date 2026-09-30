# ADR-002 — Memória extraída por LLM e turno pessoal fora do cache compartilhado

- Status: aceito
- Data: 2026-09-20
- Relacionado: [ADR-001](ADR-001-arquitetura-multi-agente.md) (MongoDB como plano de coordenação), [ADR-003](ADR-003-guardrail-em-duas-faixas.md)

## Contexto

A memória do cliente (`customer_memory`) era uma lista de palavras: se a mensagem continha "mais barato" ou "defeito", gravava um de dois textos fixos (`price_sensitive`, `product_complaint`). Isso tinha quatro problemas, todos verificados em execução contra o cluster real:

1. **Não extraía fato nenhum de verdade.** "Me chame de Bruno" ou "nunca me ofereça acima de R$ 800" não viravam memória — a lista não conhecia essas frases.
2. **O orçamento era prosa.** `price_sensitive` era um texto no prompt e um teto fixo de R$ 350 no código. O modelo podia ignorar; o servidor não aplicava filtro nenhum.
3. **`cascade_store_episode` gravava "Pergunta: …/Resposta: …" cru** em `long_term_memory`, e essa memória volta ao prompt em turnos seguintes (`long_term_hint`). Qualquer frase digitada pelo cliente — injeção incluída — virava contexto persistente.
4. **Uma pergunta pessoal podia ser servida do cache compartilhado.** Como a cascata roda antes da memória, um HIT nunca consulta `customer_memory`: a resposta de um cliente vazaria para outro.

A PoV irmã (`singleagent`) já tinha resolvido isso; o padrão foi portado e medido aqui.

## Decisão

**Memória (`backend/app/memory.py`)** — um LLM barato extrai fatos duráveis, o servidor decide o que entra:

| Peça | O que faz |
|---|---|
| Portão de frases (`should_extract`) | Caminho grátis e **sem regex**: só paga o extrator se o texto (sem acento/pontuação, borda de palavra) tiver sinal de identidade/preferência em 1ª pessoa |
| Extrator com saída estruturada | Fatos em 3ª pessoa ("me chame de Bruno" → "Cliente prefere ser chamado de Bruno"), com `category` e `max_price_brl` |
| Dedup por `fact_norm` | Contra a memória ativa inteira, não só contra os candidatos recuperados |
| Supersessão transacional | Fato que contradiz outro o desativa (`active: false`, `superseded_by`) na MESMA transação — memória nunca fica contraditória e o histórico continua auditável |
| `looks_like_instruction` | Regra fixa, determinística: fato em formato de comando ("assistente deve…", "ignore…", "disregard…") é descartado **mesmo que o LLM o devolva** |
| Falha fechada | Sem LLM, saída inválida, erro de rede ou falha de escrita ⇒ nada é gravado e **o turno não cai** |

**Orçamento** — `max_price_brl` é campo estruturado, não frase. O servidor injeta `filter: {price: {$lte: N}}` no `$vectorSearch` do catálogo (`agents.py:build_product_pipeline`). O modelo não consegue ignorar nem substituir o filtro. É limite **duro**: um teto vindo da memória (ou dito na mensagem e igual/mais estrito que ele) nunca é relaxado em silêncio — o agente diz que nada cabe em vez de mostrar item acima.

**Episódio de longo prazo** — `cascade_store_episode` grava só rótulos do próprio sistema ("Cliente já foi atendido sobre 'recomendacao' pelo agente product_agent"), um documento por `(cliente, intent, agente)`. `cascade_long_term_context` só injeta no prompt documentos com `kind: "episode"`; o legado cru fica de fora.

**Cache (`backend/app/cascade.py`)** — turno pessoal não lê nem grava o cache compartilhado, em três portões, do mais barato ao mais caro:

1. **Portão de frases** (grátis) — nem consulta o cache.
2. **Pedido de ação / mensagem composta** (grátis) — "abra um chamado", "cancelar", ou mensagem muito mais longa que a pergunta guardada.
3. **Classificador vetorial** (`turn_classifier.py`, `$vectorSearch` em `<brain>.turn_probes`) — só num HIT e antes de gravar, nunca num MISS.

**Falha fechada com granularidade**: sem veredito (índice ausente, limiar não medido, erro) descarta só HIT de escopo **global**; sessão/cliente seguem, porque não saem do dono. Um veredito "pessoal" decisivo descarta qualquer escopo.

### Números medidos (não escolhidos)

- Limiar do classificador de turno: **0,7162**, medido com `calibrate_thresholds.py --only turn --allow-errors --apply` contra 26 probes de teste **distintos dos 44 semeados** — 0 falsos alarmes, 1 positivo perdido (declarado: "me lembra o que combinamos sobre o valor máximo", 0,6702, contra um negativo em 0,7021).
- Reformulação no curto prazo: "quero saber onde está o meu pedido PED-1001" mede 0,8646 contra a pergunta original; "cadê o pedido PED-1001?" não alcança o corte de 0,78 — por isso o roteiro usa a primeira.

## Alternativas descartadas

- **Manter a lista de palavras** — não extrai preferência nem orçamento, e o teto fixo de R$ 350 era um limite que o cliente nunca declarou.
- **LLM classificando "é pessoal?" em todo turno** — soma latência e custo em todo turno e anula o ganho do cache. O classificador só roda no HIT e antes de gravar.
- **`$regex` para os portões** — regra do projeto: nada de `$regex` em query MongoDB (busca textual é `$search`). Os portões são listas de frases em Python sobre texto normalizado.
- **Chutar o limiar do classificador** — calibrar com quase-cópias da frase semeada fixa o corte na faixa de "texto idêntico"; o corte é medido contra paráfrases.

## Consequências

- Turno pessoal custa uma chamada ao LLM (sem cache). É o preço de não vazar resposta.
- O classificador perde paráfrases raras. Erro preferido: pular o cache a mais, nunca servir a mais.
- `<brain>.turn_probes` + índice `turn_probes_vs` são um passo explícito (`seed_turn_probes.py`), fora do `seed.py`, porque criam coleção e índice no cluster.
- Adicionar probes genéricos **piora** o classificador. Meça antes e depois de mexer neles.
- Fatos legados (`fact_type`/`value`) continuam sendo lidos: `migrate_legacy_memory.py` é aditivo e idempotente, nunca remove os campos antigos.
