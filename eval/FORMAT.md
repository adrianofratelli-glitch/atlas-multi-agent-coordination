# Formato do eval de roteamento (compartilhável com a PoV singleagent)

Objetivo: medir a MESMA coisa nas duas PoVs para poder comparar multiagente x singleagent com
números, não com impressão. O dataset e o relatório abaixo não dependem de nada específico do
multiagente — no singleagent, `expected_agent` é sempre o agente único e a métrica interessante
passa a ser resolução e custo por turno.

## Dataset — `eval/routing_dataset.json`

```json
{
  "name": "...", "version": 1,
  "synthetic": true,
  "generated_by": "modelo/autor e data",
  "limitation": "frase honesta sobre o que este dataset NÃO mede",
  "cases": [
    {
      "id": "route-001",
      "customer": "ana",
      "message": "onde está meu pedido?",
      "expected_agent": "order_agent",
      "min_handoffs": 0,
      "requires_llm": false,
      "expected_route_source": "rules",
      "expect_out_of_scope": false,
      "expect_blocked": false,
      "notes": "por que este caso existe"
    }
  ]
}
```

| Campo | Obrigatório | Significado |
|---|---|---|
| `id` | sim | estável; é a chave de comparação entre rodadas |
| `customer` | sim | identidade real do seed (o `customer_key` sai do JWT, nunca do payload) |
| `message` | sim | a frase do cliente, como ele escreveria |
| `expected_agent` | sim | agente de **entrada** do turno; `a+b` (ordem irrelevante) para fan-out paralelo; `orchestrator` para resposta enlatada; `guardrail` para bloqueio |
| `min_handoffs` | não (0) | piso de handoffs — cobre cadeia que deveria existir e sumiu |
| `requires_llm` | não (false) | caso sem veredito honesto offline: entra na contagem, fica fora da acurácia em DEMO_MODE |
| `expected_route_source` | não | `rules` \| `orchestrator` \| `fallback` \| `fanout` (documental) |
| `expect_out_of_scope` | não | resolvido = orientação de escopo, sem agente |
| `expect_blocked` | não | resolvido = bloqueio do guardrail |
| `notes` | não | por que a expectativa é essa (de preferência: medida, não suposta) |

`synthetic: true` é obrigatório quando as frases foram escritas por um modelo. Se dataset e
agente saem da mesma família de modelo, isso vai em `limitation` e é repetido no relatório.

## Métricas — `backend/eval_routing.py`

* **Acurácia de roteamento** — fração de casos cujo **primeiro** agente é o esperado. O primeiro,
  não o último: uma cadeia correta (diagnóstico → recomendação → efetivação) termina em outro
  agente sem que o roteamento tenha errado.
* **Taxa de resolução** — o turno entregou o desfecho esperado: resposta não vazia, não degradada,
  sem cair na orientação genérica, e (conforme o caso) bloqueado/orientado como esperado.
* **Handoffs por turno (média)** — custo de coordenação. Comparável direto com o singleagent (0).
* **Tokens por turno (média)** e **turnos degradados** — custo e resiliência na mesma tabela.

## Relatório

`--json out.json` grava `{"summary": {...}, "rows": [...]}`; `--compare antes.json` imprime o
delta campo a campo. `summary` carrega `mode` (`demo`/`live`), `scored_cases`,
`skipped_requires_llm`, as quatro métricas acima, `synthetic` e `limitation`.

```bash
cd backend && ../.venv/bin/python eval_routing.py                 # offline (DEMO_MODE)
cd backend && ../.venv/bin/python eval_routing.py --live          # Atlas + LLM reais, banco ISOLADO
cd backend && ../.venv/bin/python eval_routing.py --json hoje.json --compare ontem.json
```

## Medições desta PoV (22/09/2026, 24 casos)

| Modo | Casos avaliados | Acurácia de roteamento | Resolução | Handoffs/turno | Tokens/turno |
|---|---|---|---|---|---|
| `demo` (offline) | 21 (3 exigem LLM) | 100,0% | 100,0% | 0,125 | 43,5 |
| `live` (Atlas + LLM, banco `multi_agent_poc_test`) | 24 | 100,0% | 100,0% | 0,125 | 762,6 |

Três casos (`route-011`, `route-018`, `route-024`) só têm veredito com LLM: dependem do
orquestrador ou do classificador do guardrail, que em DEMO_MODE não existem.

O modo `--live` grava dado real (conversa, memória, resgate de pontos) e por isso roda nos bancos
de TESTE (`<banco>_test`, `backend/scripts/isolation.py`), nunca no da demo — ele recusa o banco
da demo sem `ALLOW_DEMO_DB_WRITE=1`. O `summary` do relatório carrega o campo `database` para o
número nunca ficar órfão de onde foi medido.

O `summary` carrega também `embedding_classifiers`: duas sondas (`scope_classifier` e
`turn_classifier`) com o veredito cru e o booleano `embedding_path_live`. O banco de teste recebe
`turn_probes`/`scope_probes` e seus índices vetoriais copiados da demo, então o caminho de
embedding é o mesmo — mas o relatório PROVA isso em vez de afirmar: em `demo` (offline) o campo
sai `method: fallback` e o texto avisa, em letras, que os números não cobrem esse caminho. Um
"100%" sem essa linha não diz qual classificador foi exercitado.
