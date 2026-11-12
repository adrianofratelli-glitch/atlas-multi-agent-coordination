import test from 'node:test';
import assert from 'node:assert/strict';
import { layoutGraph, edgePath, deriveTurn, describeTurn, duplicateHandoffs, isHumanResume, HUB } from '../src/graphModel.js';

const agents = [
  { agent_key: 'orchestrator', label: 'Orquestrador', active: true },
  { agent_key: 'product_agent', label: 'Produtos', active: true },
  { agent_key: 'order_agent', label: 'Pedidos', active: true },
  { agent_key: 'logistics_agent', label: 'Logística', active: false },
  { agent_key: 'support_agent', label: 'Suporte', active: true },
];

// Shape of a real DEMO_MODE turn (flagship message), trimmed to what the graph reads.
const flagship = [
  { category: 'guardrail', title: 'Guardrail de entrada', result: { blocked: false } },
  { category: 'agent', agent: 'support_agent', title: 'Roteamento inicial', collection: 'multiagent_brain.routing_rules', op: 'read' },
  { category: 'cache', agent: 'support_agent', title: 'Cascata semântica: MISS', result: { hit: false } },
  { category: 'coordination', agent: 'orchestrator', title: 'Plano do turno: 3 tarefa(s) decompostas pelo orquestrador', result: { plan: [
    { agent: 'support_agent', task: 'diagnosticar' }, { agent: 'product_agent', task: 'recomendar' }, { agent: 'order_agent', task: 'trocar' }] } },
  { category: 'coordination', agent: 'support_agent', title: 'Quadro do turno: entrada de support_agent', collection: 'turn_blackboard', op: 'write', result: { hop: 0, writes: [] } },
  { category: 'handoff', agent: 'support_agent', title: 'Handoff explícito', collection: 'agent_handoffs', op: 'write', result: { to_agent: 'product_agent', revisit: false, task: 'recomendar' } },
  { category: 'agent', agent: 'product_agent', title: 'product_agent · tool calling (1 rodada(s): search_catalog)', result: { mode: 'tool_calling', tools_called: ['search_catalog'], writes: [] } },
  { category: 'handoff', agent: 'product_agent', title: 'Handoff explícito', collection: 'agent_handoffs', op: 'write', result: { to_agent: 'order_agent', revisit: false } },
  { category: 'agent', agent: 'order_agent', title: 'Atualização segura de status do pedido', collection: 'orders', op: 'write' },
  { category: 'handoff', agent: 'order_agent', title: 'Handoff explícito', result: { to_agent: 'logistics_agent' } },
  { category: 'handoff', agent: 'logistics_agent', title: 'Retorno controlado', result: { to_agent: 'order_agent', revisit: true } },
  { category: 'coordination', agent: 'orchestrator', title: 'Orquestrador consolidou o quadro do turno numa resposta única' },
];

test('layout puts the hub in the centre and every specialist on the ring', () => {
  const graph = layoutGraph(agents, { width: 600, height: 400 });
  assert.equal(graph.byKey[HUB].x, 300);
  assert.equal(graph.byKey[HUB].y, 200);
  assert.equal(graph.nodes.length, 5);
  assert.equal(graph.byKey.logistics_agent.active, false);
  // first specialist in the fixed order sits at the top
  assert.equal(graph.byKey.order_agent.x, 300);
  assert.ok(graph.byKey.order_agent.y < 200);
});

test('A→B and B→A bend to opposite sides', () => {
  const a = { x: 0, y: 0 }; const b = { x: 100, y: 0 };
  assert.ok(edgePath(a, b).mid.y > 0);
  assert.ok(edgePath(b, a).mid.y < 0);
});

test('flagship turn: route, handoffs, revisit, plan, writes, consolidation', () => {
  const turn = deriveTurn(flagship, null, { live: true });
  assert.deepEqual(turn.edges.map((e) => [e.from, e.to, e.kind]), [
    ['orchestrator', 'support_agent', 'route'],
    ['support_agent', 'product_agent', 'handoff'],
    ['product_agent', 'order_agent', 'handoff'],
    ['order_agent', 'logistics_agent', 'handoff'],
    ['logistics_agent', 'order_agent', 'revisit'],
    ['order_agent', 'orchestrator', 'return'],
  ]);
  assert.deepEqual(turn.path, ['support_agent', 'product_agent', 'order_agent', 'logistics_agent', 'order_agent']);
  assert.deepEqual(turn.planIndex, { support_agent: 1, product_agent: 2, order_agent: 3 });
  assert.deepEqual(turn.writes, { order_agent: ['orders'] });
  assert.deepEqual(turn.tools.product_agent, ['search_catalog']);
  assert.equal(turn.consolidated, true);
  assert.equal(turn.active, 'orchestrator');
  assert.match(describeTurn(turn), /plano com 3 tarefa/i);
});

test('backend agent_path wins once the turn ends; active node only while live', () => {
  const turn = deriveTurn(flagship.slice(0, 6), { agent_path: ['support_agent', 'product_agent', 'order_agent'] });
  assert.deepEqual(turn.path, ['support_agent', 'product_agent', 'order_agent']);
  assert.equal(turn.active, null);
});

test('fan-out draws two simultaneous edges from the hub', () => {
  const turn = deriveTurn([{ category: 'fanout', title: 'Despacho paralelo', result: { agents: ['billing_agent', 'order_agent'] } }],
    { route_source: 'fanout', agent_path: [] , active_agent: 'billing_agent+order_agent' });
  assert.equal(turn.fanout, true);
  const fan = turn.edges.filter((e) => e.kind === 'fanout');
  assert.equal(fan.length, 2);
  assert.equal(fan[0].step, fan[1].step);
  assert.deepEqual(turn.path, ['billing_agent', 'order_agent']);
});

test('HITL pause comes from the event or awaiting_approval and clears on resume', () => {
  const pause = { category: 'agent', agent: 'order_agent', title: 'Escrita pausada: aguarda aprovação humana (interrupt no checkpoint)',
    collection: 'pending_reviews', op: 'write', result: { tool: 'request_order_status_change', args: { status: 'reembolsado' }, policy: 'HITL-REEMBOLSO', review_id: 'REV-1' }, reason: 'Reembolso' };
  const turn = deriveTurn([pause]);
  assert.equal(turn.paused.review_id, 'REV-1');
  assert.equal(turn.paused.agent, 'order_agent');
  assert.match(describeTurn(turn), /aguardando aprovação/);
  const fromRun = deriveTurn([], { awaiting_approval: { review_id: 'REV-2', tool: 'redeem_reward', agent: 'loyalty_agent', reason: 'alto' }, agent_path: ['loyalty_agent'] });
  assert.equal(fromRun.paused.review_id, 'REV-2');
  const resumed = deriveTurn([pause, { category: 'agent', agent: 'order_agent', title: 'Turno retomado do checkpoint após decisão humana' }]);
  assert.equal(resumed.paused, null);
  assert.equal(resumed.resumed, true);
});

test('cache hit and guardrail block have their own captions; replayed events are ignored', () => {
  assert.match(describeTurn(deriveTurn([{ category: 'cache', title: 'HIT', result: { hit: true } }])), /cache/);
  assert.match(describeTurn(deriveTurn([{ category: 'guardrail', title: 'Guardrail de entrada', result: { blocked: true } }])), /bloqueou/);
  assert.equal(deriveTurn([{ category: 'fanout', replayed: true, result: { agents: ['a', 'b'] } }]).fanout, false);
});

test('path-only payload still draws a chain', () => {
  const turn = deriveTurn([], { agent_path: ['order_agent', 'logistics_agent'] });
  assert.deepEqual(turn.edges.map((e) => e.kind), ['route', 'handoff']);
});

test('crash/resume helpers', () => {
  const hs = [{ from_agent: 'order_agent', to_agent: 'logistics_agent', task: 't' }, { from_agent: 'logistics_agent', to_agent: 'order_agent', task: 'u' }];
  assert.deepEqual(duplicateHandoffs(hs), []);
  assert.equal(duplicateHandoffs([...hs, hs[0]])[0].count, 2);
  assert.equal(isHumanResume({ from_agent: 'human_reviewer', to_agent: 'order_agent' }), true);
  assert.equal(isHumanResume({ from_agent: 'order_agent', to_agent: 'billing_agent' }), false);
});

test('node text always fits inside the node (viewBox units)', async () => {
  const { fitText, textWidth, nodeSubtitle } = await import('../src/graphModel.js');
  const nodeWidth = 128;
  const subMax = nodeWidth - 28;
  for (const sub of [
    nodeSubtitle({ tools: ['get_order', 'transfer_to_agent', 'request_order_status_change'] }),
    nodeSubtitle({ paused: true }), nodeSubtitle({ visited: true }), nodeSubtitle({ active: false }),
    fitText('get_order, transfer_to_agent, request_order_status_change', subMax, 10),
  ]) {
    assert.ok(textWidth(fitText(sub, subMax, 10), 10) <= subMax, sub);
  }
  assert.equal(nodeSubtitle({ tools: ['a', 'b'] }), '2 tools');
  assert.equal(nodeSubtitle({ hub: true, planLength: 4 }), 'plano: 4 tarefas');
  assert.equal(fitText('Orquestrador', 154 - 24, 15), 'Orquestrador');
  assert.ok(fitText('Um rótulo de agente muito comprido', 104, 13.5).endsWith('…'));
  assert.ok(textWidth(fitText('Um rótulo de agente muito comprido', 104, 13.5), 13.5) <= 104);
});
