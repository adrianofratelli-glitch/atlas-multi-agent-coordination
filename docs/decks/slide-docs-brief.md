# Brief — slide "A documentação que sustenta o PoV multi-agente"

Conteúdo pronto de um slide único (16:9, 13.3" × 7.5") para apresentação de cliente.
Referência visual atual: cheat sheet estilo terminal — cabeçalho com prompt `>_`,
árvore de arquivos à esquerda em fonte monoespaçada, caixas numeradas com faixa
colorida no topo à direita. Paleta: verde `#0F6B4F`, âmbar `#A8641B`, violeta `#4B4A86`,
tinta `#10241C`, fundo `#E9EEE9`, cartões brancos.

Idioma: português. Termos técnicos (README, ADR, change stream, fan-out) ficam como estão.

---

## Cabeçalho

- **Título:** A documentação que sustenta o PoV multi-agente
- **Subtítulo:** Cada arquivo tem um leitor e um trabalho — nenhum repete o outro
- **Meta (canto direito, mono):** MongoDB Atlas · 8 agentes · :8031 / :5191

## Painel esquerdo — "Onde cada coisa mora"

Árvore de arquivos, cada arquivo com uma legenda curta abaixo:

```
multiagente-atendimento/
├── README.md                  a porta de entrada pública
├── implementation_plan.md     briefing original, 408 linhas
├── .env.example               segredos esperados, sem valor
├── docs/
│    ├── architecture.md       topologia e limites
│    ├── adr/
│    │    └── ADR-001-arquitetura-multi-agente.md
│    │                        por que Mongo e não fila
│    └── screenshots/          5 telas, 1600×1000
├── backend/  app/ tests/
└── frontend/ src/
```

## Caixas numeradas (6, em duas linhas de três)

**1 · README.md** — verde
Para quem: cliente e visitante do GitHub.
Traz o problema em um parágrafo, a demo em 5 passos numerados com um print cada, a tabela de agentes e o setup.

**2 · Briefing de arquitetura** — âmbar
Para quem: o assistente de código.
Traz comandos exatos, arquitetura em prosa, decisões já pagas e lacunas conhecidas. Evita que cada sessão redescubra o projeto.

**3 · implementation_plan.md** — violeta
Para quem: quem vai construir de novo.
Traz o pedido original em português: os oito agentes, o roteiro de demo, a ordem de trabalho e o que era para ficar de fora.

**4 · docs/architecture.md** — verde
Para quem: arquiteto do cliente.
Traz topologia, o passo a passo de um turno com repasse, os limites de segurança e como funciona a busca.

**5 · ADR-001** — âmbar
Para quem: quem pergunta "por quê".
Traz a decisão registrada: coordenar agentes no MongoDB em vez de fila ou motor de workflow — com contexto, alternativas e consequências.

**6 · docs/screenshots/** — violeta
Regra: todo print vem depois de uma execução real.
Métrica zerada ou busca sem resultado não prova nada. Nenhuma imagem carrega identidade de cliente.

## Faixa inferior — "7 · O que ainda falta escrever"

Quatro itens em duas colunas:

- **RUNBOOK.md** — como rodar a demo ao vivo: pré-aquecer o cache, ordem dos cenários, o que fazer se o Atlas cair.
- **docs/data-model.md** — as coleções e por que cada uma existe; hoje isso só aparece espalhado no código.
- **ADR-002 em diante** — o limiar do guardrail medido, o cache semântico e a escolha do fan-out ainda não estão registrados.
- **SECURITY.md** — o modelo de ameaça já implementado (chave do cliente vem do token, admin separado) sem lugar próprio.

## Notas do apresentador

A documentação do PoV é dividida por leitor: README para o cliente, o briefing de arquitetura
para o assistente de código, implementation_plan.md para quem reconstrói, architecture.md
e ADR-001 para o arquiteto. Faltam runbook de demo, modelo de dados, ADRs novos e SECURITY.md.

---

## Restrições para o ajuste de design

- Um slide só, 16:9, precisa abrir no Google Slides sem quebrar.
- Margem mínima de 0,5"; nada de texto estourando caixa.
- Densidade alta é intencional: é um slide de referência, não de impacto.
- Corpo em 9,5–10 pt, títulos de caixa em 10,5 pt, título do slide em 21 pt.
- A árvore precisa continuar em fonte monoespaçada e sem quebra de linha.
- Arquivos gerados hoje: `outputs/multiagente-docs.pptx` (este slide) e
  `outputs/multiagente-visao-geral.pptx` (visão geral da arquitetura, outro estilo).
