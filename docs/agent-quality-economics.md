# Gateway, qualidade, custo e observabilidade

A PoV roteia chamadas por modelo e correlaciona consumo com resultado de negócio.
O Grove continua sendo o gateway externo; a aplicação escolhe a rota, controla o
orçamento e registra tentativas. Isso não implementa quotas distribuídas ou administração
central de múltiplas aplicações.

## Configuração local

No `.env`, preencher `GROVE_API_KEY`. A URL Anthropic conhecida está em
`GROVE_ANTHROPIC_BASE_URL`. A autenticação Grove é `Authorization: Bearer` mais a chave real em `x-api-key` (o gateway rejeita placeholder); acesso direto Anthropic usa
a credencial própria e o header do SDK. Redirecionamentos HTTP ficam desabilitados.

Para modelos expostos por Chat Completions, obter do Grove:

- `GROVE_CHAT_COMPLETIONS_URL`: URL completa, sem inferir caminhos.
- `GROVE_OPENAI_MODELS`: lista JSON dos aliases que usam esse protocolo.

O adapter Chat Completions utiliza `model`, `messages` e `max_completion_tokens`.
Compatibilidade real de cada deployment depende do contrato do Grove. A implementação
foi validada com transporte HTTP simulado, não com todas as famílias de modelos.
O nome escolhido em `agent_registry.model` seleciona a rota; `fallback_model` pode
apontar para outra rota configurada. Endpoints são configuração do servidor, nunca
argumentos do usuário ou do modelo. Não há alteração automática do registry ou seed.

`LLM_PRICES` é um JSON por alias com tarifas em USD por milhão de tokens.
Os campos são `input_tokens` (entrada não cacheada), `output_tokens`,
`cache_read_tokens` e `cache_write_tokens`. Inserir as tarifas aplicáveis ao acesso Grove;
nenhum preço público foi presumido. Tarifas ausentes produzem custo desconhecido.

As médias observadas por modelo não substituem as tarifas separadas exigidas por `LLM_PRICES`.

## Medição

Cada tentativa registra modelo selecionado, protocolo, agente, fallback, duração,
status, consumo reportado e snapshot das tarifas usadas. Prompts, respostas e corpos
de erro não são incluídos nesse ledger. O trace de negócio existente mantém seu
próprio fluxo de mascaramento.

Custos são estimativas de inferência, não fatura ou TCO: não incluem Atlas, buscas,
embeddings, gateway ou infraestrutura. Falhas sem consumo reportado tornam o total
desconhecido. A parte conhecida permanece em `known_cost_usd`. Respostas truncadas
com consumo reportado são contabilizadas, mas não são aceitas como resposta completa.

`usage.total` continua sendo o budget operacional legado, que inclui heurísticas de
contexto. O cálculo financeiro usa exclusivamente `llm_calls`, sem cobrar essas
heurísticas outra vez. Cache HIT pode ainda ter uma chamada de classificação/guardrail
antes da consulta ao cache; a UI mostra as tentativas efetivas do turno.

Eventos antigos recuperados do cache têm `replayed=true`. Continuam visíveis no
histórico, mas não incrementam contadores de operações nem geram chamadas fictícias
no Langfuse. As gerações Langfuse são derivadas do ledger de chamadas, com timestamps.
As métricas operacionais permanecem por processo; traces e evals são persistidos no Atlas.

## Avaliações

Execução isolada, sem Atlas/LLM ou escrita externa, a partir de `backend/`:

```bash
../.venv/bin/python eval.py --offline --output /tmp/multiagent-eval.json
```

Execução real contra uma instância de demonstração e o MESMO Atlas do `.env`:

```bash
../.venv/bin/python eval.py http://127.0.0.1:8031 --label grove-baseline --output /tmp/grove-baseline.json
```

O eval real executa as ações do roteiro (trocas, chamados, resgates e reagendamento)
e grava `eval_runs`: usar fixtures de demonstração dedicadas. Não executá-lo sobre
atendimentos reais. O avaliador usa a emissão de token da demo.

`--case ID` seleciona casos, `--repeats N` repete sequencialmente. Estado e cache são
preservados entre repetições: isso testa continuidade/idempotência, não constitui um
benchmark de trials independentes. Não há reset implícito de dados.

O avaliador confere roteamento e sequência, mas o resultado de negócio vem de leituras
diretas posteriores ao turno: status do pedido, reagendamento, chamado novo, revisão
pendente, resgate e correspondência do débito de pontos. Casos sem escrita esperada
verificam que os documentos de negócio do titular não mudaram. Isso não substitui
uma avaliação completa de vazamento de dados ou qualidade semântica da resposta.

O relatório agrega taxa de sucesso, p95 do pedido de chat, cache hits, cobertura de
custo e custo por sucesso. O numerador inclui custos das tarefas que falharam.
Se algum custo for desconhecido, o custo total/por sucesso fica indisponível. O modo
offline não publica custo por sucesso como benchmark; seus modelos não são executados.

A aba **Métricas** mostra o histórico em modo admin (habilitado em **Decisões**).
Compare execuções com o mesmo `dataset_sha256`, fixtures equivalentes e a mesma
política de cache. `successful_llm_calls` mostra se houve inferência real.

## Validação e limites encontrados

Validação local: 85 testes de backend e dois de frontend aprovados; build do frontend
e lint dos módulos alterados aprovados. Em 18/09/2026, uma chamada mínima real ao Grove
via rota Anthropic com `claude-haiku-4-5` funcionou e reportou 29 tokens de entrada e
5 de saída. Isso valida conectividade/autenticação dessa rota, não a qualidade dos
29 cenários com LLM nem os demais provedores.

Também foi validado `gpt-5.6-luna` no endpoint chat/completions do gateway Grove anterior (o gateway atual só atende o luna pela Responses API, ainda não implementada aqui).
O teste mínimo reportou 32 tokens de entrada e 4 de saída. Em um turno fan-out com
fixtures sintéticas em memória e LLMs reais, `billing_agent` usou Claude Haiku
(566 tokens de entrada, 50 de saída, 1766 ms) e `order_agent` usou GPT Luna
(501 de entrada, 107 de saída, 3491 ms), ambos com status `ok`, sem fallback.
São amostras de integração, não benchmark de qualidade ou latência.

A configuração mista foi aplicada somente ao store em memória desse teste. Duas
tentativas de leitura do Atlas expiraram (5 e 20 segundos); nenhum modelo foi
alterado no registry persistente. O backend local não estava ouvindo na porta 8031
no momento dessa primeira validação. O bloqueio foi resolvido na etapa integrada abaixo.

A avaliação offline inicial cobriu 29 cenários: 27 passaram. As paráfrases
`ana-paraphrase-jailbreak` e `bruno-paraphrase-exfiltration` não são bloqueadas pelo
fallback lexical offline. Elas continuam falhando no relatório e precisam de validação
ao vivo com Vector Search/classificador; não foram convertidas em sucessos artificiais.

O gateway entre famílias, preços e comportamento ao vivo dependem de credencial,
endpoints, aliases e tarifas reais. Não há LLM-as-judge, comparação automática de modelos,
SLO distribuído ou medição de recuperação de falhas de infraestrutura nesta entrega.


## Perfil misto ativo e avaliação integrada — 18/09/2026

Após o cluster ser ligado, a API administrativa atualizou `order_agent` no registry
persistente para `gpt-5.6-luna`, com fallback `claude-haiku-4-5`. A operação gerou
`admin_audit`. Os sete outros agentes permanecem em Claude Haiku. Esse perfil é
configuração do Atlas; executar o seed novamente pode restaurar seus valores originais.

A execução `grove-mixed-live`, persistida em `eval_runs`, passou nos cinco casos
selecionados: fan-out de Ana, jailbreak por paráfrase, exfiltração por paráfrase,
pedido legítimo de documento da fatura e rastreamento legítimo. Foram quatro chamadas
LLM bem-sucedidas, zero cache hits e p95 de 10,60 s nessa amostra pequena. Os cinco
casos não representam uma execução completa dos 29 cenários.

O backend foi iniciado na porta 8031 e a interface na 5191. As tarifas continuam
pendentes: custo total e custo por sucesso ficam indisponíveis, sem presumir preços.

## Estimativa financeira habilitada

A estimativa usa `LLM_BLENDED_PRICES`: **2,40 USD/MTok para Haiku** e
**0,38 USD/MTok para Luna**, médias observadas de cada modelo na captura de
19/09/2026. Substituem as referências de 18/09/2026. A UI identifica a base como
média histórica; `LLM_PRICES` continua reservado a tarifas detalhadas.
