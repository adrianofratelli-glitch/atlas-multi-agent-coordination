# ADR-012 — Aprovação humana antes de escrita sensível: política é dado, pausa é checkpoint

- Status: aceito
- Data: 2026-11-12
- Relacionado: [ADR-011](ADR-011-tool-calling-plano-quadro-e-consolidacao.md)

## Contexto

A fila `pending_reviews` já existia para a revisão de qualidade (defeito recorrente), mas o turno
não parava: o agente respondia e o analista resolvia depois, sem o grafo voltar a rodar. A demo não
mostrava a propriedade mais útil do checkpointer no Atlas: um turno pausado sem conexão aberta, que
sobrevive a restart e retoma de onde parou.

## Decisão

- `<brain>.hitl_policies` diz quais chamadas de tool exigem um humano
  (`HITL-REEMBOLSO`: `request_order_status_change` com `status: reembolsado`;
  `HITL-RESGATE-ALTO`: `redeem_reward` com 500+ pontos, desligada no seed para ser ligada ao vivo). Ligar/desligar é `update_one`
  (`PATCH /api/admin/hitl-policies/{id}`).
- Quando uma política bate, a tool **não escreve**: devolve `aguardando_aprovacao_humana`; o hop
  abre a revisão (`evidence.kind = "hitl_interrupt"`, com `thread_id`) e o nó `approval` chama
  `langgraph.types.interrupt()`. O cliente recebe `awaiting_approval` com o id da revisão.
- A cadeia NÃO para na hora: os passos restantes do plano rodam com as escritas retidas (só leitura), e a pausa
  acontece antes da consolidação — a resposta da pausa já cobre entrega, fatura etc.
- Uma thread de checkpoint por TURNO (`<cliente>:<conversa>:<turno>`), gravada em `evidence.thread_id`.
- `POST /api/admin/reviews/{id}/resolve` grava a decisão (como antes) e retoma a thread GRAVADA na revisão, conferindo
  que a pausa pendente é a desta revisão, com
  `Command(resume={approved, resolved_by, note})`. O nó `approval` só chama `interrupt()` (roda de novo na
  retomada, sem efeito colateral); a escrita aprovada fica no nó `apply_approved`, pelo MESMO executor da tool
  (mesmos portões), idempotente por `applied_at` na revisão. Uma segunda retomada é no-op;
  `POST /api/admin/reviews/{id}/continue` repete uma retomada cuja escrita falhou.
- O caminho determinístico respeita a mesma política (o `order_agent` pausa o reembolso), e pedido
  de reembolso não entra no fan-out, que não pausa.

## Achado durante a implementação

O `JsonPlusSerializer` do checkpoint recebia `allowed_msgpack_modules` como nomes de módulo; a versão
atual do LangGraph só aceita pares `(módulo, classe)`. Tudo era bloqueado e o estado voltava como
`dict` na retomada (`'dict' object has no attribute 'intent'`). O `MemorySaver` do DEMO_MODE não
serializa e escondia o bug — mais um caso de "DEMO_MODE esconde bug do driver real".

## Medição (ao vivo, `*_test`)

Reembolso do Bruno: pausa com `REV-…`, pedido continua `processando`; aprovação retoma o thread e
grava `reembolsado` uma vez; segunda retomada devolve nada. Crash antes do 2º hop: o checkpoint mostra
`next=('agent_hop',)` com 1 hop concluído; a retomada termina o turno com **1** decisão gravada (sem
duplicar a escrita do 1º hop).

## Revisão independente (2026-11-12)

A primeira versão usava uma thread por CONVERSA: um turno novo recomeçava do START sobre o estado do anterior
e descartava o interrupt pendente; aprovar a revisão R1 podia retomar a pausa R2 de outro pedido; a revisão
era deduplicada por assunto e fundia pausas de turnos diferentes. Corrigido com thread por turno, conferência do
`review_id`, dedupe por `turn_id` e a divisão `approval`/`apply_approved` (testes em `tests/test_agentic_core.py`).
