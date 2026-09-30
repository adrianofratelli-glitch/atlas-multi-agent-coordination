# ADR-004 — Escopo por embedding e medição por situações (não por frases escritas à mão)

- Status: aceito
- Data: 2026-09-21
- Relacionado: [ADR-003](ADR-003-guardrail-em-duas-faixas.md) (guardrail em duas faixas e fora de escopo), [ADR-002](ADR-002-memoria-llm-e-turno-pessoal.md)

## Contexto

O ADR-003 resolveu "qual é a temperatura hoje?" com um vocabulário de domínio (`router.DOMAIN_VOCAB`): sem palavra da loja, orientação educada com 0 tokens. Funcionou nos exemplos que eu tinha em mente. A pergunta que o usuário fez em seguida foi a certa: **"se colocarmos frases, em alguma situação ele vai escorregar"**.

Para saber *quanto* escorregava, em vez de acreditar, montei um conjunto de situações (ver abaixo) e medi o sistema real. Linha de base, com o sistema exatamente como estava no ADR-003:

| Métrica (n = 229) | Linha de base |
|---|---|
| Acerto geral | **87,3%** (dev 86,5 / holdout 88,1) |
| Pedido real da loja recusado como "fora de escopo" | 7,2% |
| Ataque que chegou a um agente | 33% |
| Ataque bloqueado | 27% |
| Cliente legítimo bloqueado pelo guardrail | 0% |

Os escorregões eram todos de lista de palavras: inglês e espanhol ("I want to return this product"), gíria e erro de digitação ("Cancela ai mano"), aparelhos que a loja não vende ("meu notebook não está ligando"), saudação/agradecimento/meta ("Olá, tudo certo por aí?", "vc eh inteligencia artificial?"). E ataques com `PED-xxxx` embutido casavam uma regra de roteamento e **pulavam o classificador de segurança**.

## Decisão

### 1. Escopo por embedding, LLM só na dúvida

`app/scope_classifier.py` responde "é assunto da loja?" com `$vectorSearch` em `<brain>.scope_probes` (índice `scope_probes_vs`, autoEmbed voyage-4), com **três rótulos**: `in` (da loja), `out` (alheio) e `chat` (saudação, agradecimento, "quem é você?"). Embedding é bom exatamente em tópico, então entende idioma, gíria e erro de digitação sem lista.

- **Decide pela margem** entre o rótulo líder e o segundo colocado, não por score absoluto (que varia com o tamanho da frase). Cada rótulo tem o seu limiar.
- **Faixa ambígua vai ao LLM.** Margem abaixo do limiar ⇒ `unsure`: o LLM de roteamento decide (com saída `nenhum` e `conversa`). O LLM gasta tokens só onde há dúvida.
- **Rótulo ausente entre os vizinhos** = mais distante que o último vizinho devolvido; usa-se esse teto (nunca 0), para a margem nunca parecer maior do que é. (Achado em live: "where is my order?" traz 12 vizinhos, todos `in`; exigir dois rótulos fazia o classificador se abster justamente nos casos mais claros.)
- **Sem veredito real** (DEMO_MODE, índice ausente/vazio, limiar não medido, exceção) ⇒ comportamento anterior, por palavras. Nunca derruba o turno e nunca inventa decisão.
- Só roda quando **nada mais decidiu**: sem palavra forte, sem rota determinística, sem ser saudação/agradecimento/meta óbvios (`orchestration.reaches_scope_classifier`). A calibração mede exatamente essa população.

### 2. Limiares medidos, "precisão primeiro", em situações que o índice nunca viu

`calibrate_thresholds.py --only scope` mede no **split dev** de `tests/data/situations.json`. Cada rótulo só é decisivo acima do **maior erro medido dele** (+ folga) e nunca abaixo de um piso de **0,04** (o dev tem poucos itens `in` sem palavra-chave; margem menor que isso é ruído). O relatório mostra a cobertura, que é o custo da prudência: **`in` 4/6, `out` 11/19, `chat` 3/5** decisivos; o resto vai ao LLM.

Um único item legítimo ambíguo ("o roteador que adquiri não conecta") prendia o limiar de `out` em 0,0802. Reclamar de algo que o cliente **comprou** é suporte, seja qual for o aparelho; acrescentar probes `in` disso baixou o limiar sem arriscar recusa real.

### 3. Segurança nunca depende do embedding

- **`guardrails.needs_security_review`**: formas de exfiltração (dado de terceiros), autoridade fingida, política a burlar e injeção técnica **ligam o classificador de segurança mesmo com regra de roteamento casada**. Não decide nada; só liga a verificação. Corrige o furo do `PED-xxxx`.
- **Tudo o que chega a agente por fora de palavra-chave passa antes pelo classificador de segurança** (testado: ordem `security` → `route`).
- **O prompt do classificador de segurança** passou a cobrir intenção **declarada** de enganar ("vou mentir…", "exijo reembolso mesmo tendo recebido", ameaça de chargeback) e injeção embutida dentro de um pedido legítimo, e deixou de mandar "preferir DUVIDA": intenção declarada de enganar não é ambígua.
- O isolamento por `customer_key` no JWT continua sendo a rede de segurança real; nenhum classificador é a defesa final.

### 4. O prompt do roteador descrevia 4 dos 7 agentes

`orchestration.ROUTER_PROMPT` (agora constante testável) não dizia nada sobre garantia, fidelidade e logística, e não cobria cancelamento, "falar com humano" nem "meus dados cadastrais" — que voltavam como `nenhum`. Agora descreve todos os agentes e tem a saída `conversa` (**somente** cumprimento, agradecimento, despedida, "quem é você"; poema, piada e CNPJ são `nenhum`). A primeira versão de `conversa` ficou gulosa e engoliu "escreva um poema" como boas-vindas: foi pego pela medição e apertado.

### 5. Como medir: situações geradas por LLM, com holdout

`generate_situations.py` gera **229 situações** (33 categorias) a partir de seeds de intenção; o LLM produz variações (gíria, erro de digitação, sem acento, longa, curta, inglês/espanhol, irritada) e o rótulo vem da **construção**. Um **verificador** descarta casos que contradizem o rótulo (achou e removeu, por exemplo, "fraude" que era pergunta legítima de devolução; tornou o seed de `attack_fraud` mais estrito). Split **dev/holdout por hash da mensagem** (não escolhido): ajusta-se olhando o dev; o holdout só diz a verdade.

`eval_situations.py` roda o agente real (Atlas + LLM) e reporta, separadamente, as taxas que importam ao cliente: **falso bloqueio**, **pedido real recusado**, **assunto alheio respondido por agente**, **ataque que chegou a um agente**. O agente esperado é métrica à parte (`agent_match_pct`): ser atendido por outro agente não é falha. **Fronteiras** (produto que a loja não vende, ajuda técnica genérica) têm rótulo `handled`: agente que diz "não temos" ou orientação de escopo são ambos corretos; só bloquear ou acolher é erro.

## Resultado (mesmas 229 situações, mesmo procedimento)

| Métrica | Antes | Depois | 
|---|---|---|
| Acerto geral — dev | 86,5% | **98,2%** |
| Acerto geral — **holdout** | 88,1% | **97,5%** |
| Pedido real recusado | 7,2% | 0% dev / 1,5% holdout |
| Ataque que chegou a agente | 33% | 10,5% dev / **0%** holdout |
| Ataque bloqueado | 27% | 89% dev / 71% holdout |
| Cliente legítimo bloqueado | 0% | 0% |

O holdout subiu tanto quanto o dev, o que descarta ajuste ao conjunto de calibração.

## Segunda rodada — o que estava "pendente" e foi feito

Depois de fechar o ADR, as limitações que eu tinha listado como "futuras" foram atacadas em vez de deixadas. Cada uma foi medida separadamente (rodada 1 → 2 → 3) sobre o conjunto ampliado.

| Item | O que foi feito | Efeito medido |
|---|---|---|
| Conjunto pequeno demais (dev com 6 itens `in` sem palavra-chave) | `generate_situations.py --append` gerou **58 situações novas** em 6 categorias (gíria sem palavra-chave, inglês, espanhol, aparelho comprado, escrita criativa, recomendação alheia), sem trocar as 229 já medidas. Total: **287**. | Dev `in` 6 → 13 itens e `out` 19 → 25. O pior erro medido dos limiares ficou em 0,004 — dez vezes abaixo do piso de 0,04 — então os limiares se sustentam com o dobro de dados. |
| "me recomenda um filme" ia ao `product_agent` | Gate de **código** (`router.has_catalog_anchor` + `weak_product_route`): quando a rota de produto se apoia só no verbo genérico "recomenda" (sem categoria de catálogo nem "produto"), um veredito de escopo decisivo `out` a recusa. Não mexe na regra seedada. | "filme" corrigido; com 12 probes `out` de recomendação alheia (restaurante, podcast, app, série, livro), `oos_answered_by_agent` no holdout foi a **0%**. |
| Custo de fora-de-escopo (~400 tokens) | Na faixa ambígua **sem suspeita**, o LLM de roteamento roda **antes** do classificador de segurança: se disser `nenhum`/`conversa`, a resposta é enlatada e a segurança não roda; se escolher um agente, a segurança roda logo depois. Com suspeita, a segurança continua primeiro. | Tokens médios de fora-de-escopo: dev **545 → 354**, holdout **416 → 297** (−35% / −29%), sem perda de acerto. |

Um bug de medição pego no caminho: ao ampliar o conjunto, os limiares saíram **idênticos** ao anterior — o calibrador não conhecia as categorias novas, então a amostra maior não entrava na medição. Corrigido, com teste que exige que toda categoria do conjunto esteja mapeada a um rótulo ou explicitamente sem rótulo (ataques e fronteiras).

**Resultado final (287 situações):** holdout **98,6%**, dev 95,7% (o dev concentra os casos mais duros do conjunto novo); 0% de assunto alheio respondido por agente e 0% de ataque chegando a agente no holdout; tokens de fora-de-escopo 314 (dev) / 207 (holdout).

## Limitações declaradas

- **O classificador de segurança é um LLM.** Antes de fixar `temperature=0` (o gateway não a enviava, então rodava com amostragem padrão), 2 a 3 ataques trocavam de lado entre rodadas e a regressão completa pegou um jailbreak que passou uma vez e foi bloqueado em isolamento. Classificação (segurança, roteamento, extrator de memória) agora roda com `temperature=0`, com repetição sem o parâmetro se o modelo o rejeitar (`llm._request`). Melhorou, mas um LLM continua não sendo prova: um ataque que chega a agente ainda é contido pelo isolamento por `customer_key`, e o guardrail não deve ser vendido como 100%.
- **Custo de tokens em fora-de-escopo subiu**: de 139 para ~400 em média. O caminho antigo era barato *e errado* (recusava pedido real em inglês/gíria); o novo paga LLM na faixa ambígua (só 11/19 alheios são decisivos com o piso prudente). É o preço de não escorregar.
- **Fronteiras**: "quanto custa um iPhone" recebe recusa educada (probe `out`); o `product_agent` diria "não temos no catálogo", também correto. Escolha registrada, não um erro.
- **Recomendação alheia dentro de uma mensagem longa** ("Olá, gostaria de recomendações de restaurantes bons perto de casa") ainda pode ir ao `product_agent`: o gate só recusa quando o veredito de escopo é decisivo, e mensagem longa com saudação costuma cair na faixa ambígua. Sobram 2 casos no dev, um deles de fronteira ("vocês vendem livros?").
- **Amostras**: o dev tem 13 itens `in` e 25 `out` que chegam ao classificador (a maioria das mensagens legítimas tem palavra-chave e nunca chega a ele). O piso de 0,04 continua por prudência; recalibrar quando o conjunto crescer.
- O conjunto é gerado por LLM: reflete o que um LLM imagina que clientes escrevem, com o viés disso. É muito mais variado do que o que eu escrevo à mão, mas não substitui tráfego real. O melhor passo seguinte é alimentar as falhas do uso real em `situations.json`.

## Consequências

- `<brain>.scope_probes` + índice `scope_probes_vs` + `<brain>.scope_classifier_config` são um passo explícito (`seed_scope_probes.py`), fora do `seed.py`, porque criam coleção e índice no cluster.
- Adicionar probes reindexa de forma assíncrona: recalibrar só depois de o probe novo ser o vizinho nº 1 dele mesmo (o score de texto idêntico **não** chega a 1,0 neste cluster).
- Ajustar probes olhando o dev contamina a medição do dev; por isso o holdout é o número a reportar.
- Regressão nova precisa entrar em `situations.json` (ou nos seeds de `generate_situations.py`) — é assim que a próxima falha real deixa de escorregar duas vezes.
