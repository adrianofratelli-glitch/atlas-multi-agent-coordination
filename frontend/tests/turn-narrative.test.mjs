import test from 'node:test';
import assert from 'node:assert/strict';
import { narrateTurn, describeNext, answerAuthor, authorLabel } from '../src/turnNarrative.js';

const labels = { orchestrator: 'Orquestrador', support_agent: 'Suporte', product_agent: 'Produtos', order_agent: 'Pedidos', logistics_agent: 'Logística', billing_agent: 'Cobrança' };

const flagship = [
  { category: 'guardrail', title: 'Guardrail de entrada', collection: 'guardrail_denylist', op: 'read', result: { blocked: false } },
  { category: 'agent', agent: 'support_agent', title: 'Roteamento inicial', collection: 'multiagent_brain.routing_rules', op: 'read', filter: { intent: 'suporte_tecnico' }, result: { target_agent: 'support_agent', source: 'rules', confidence: 0.9 } },
  { category: 'cache', agent: 'support_agent', title: 'Cascata semântica: MISS (curto prazo + cache global)', collection: 'short_term_memory', op: 'vectorSearch', result: { hit: false } },
  { category: 'coordination', agent: 'orchestrator', title: 'Plano do turno: 3 tarefa(s) decompostas pelo orquestrador', collection: 'turn_blackboard', op: 'write', result: { plan: [
    { agent: 'support_agent', task: 'diagnosticar' }, { agent: 'product_agent', task: 'recomendar' }, { agent: 'order_agent', task: 'trocar' }] } },
  { category: 'agent', agent: 'support_agent', title: 'support_agent · tool calling (1 rodada(s): search_knowledge_base, transfer_to_agent)', collection: 'multiagent_brain.agent_registry', op: 'read', result: { mode: 'tool_calling', task: 'diagnosticar', tools_called: ['search_knowledge_base', 'transfer_to_agent'], writes: [] } },
  { category: 'agent', agent: 'support_agent', title: 'Base de conhecimento via $rankFusion', collection: 'kb_articles', op: 'hybridSearch' },
  { category: 'coordination', agent: 'support_agent', title: 'Quadro do turno: entrada de support_agent', collection: 'turn_blackboard', op: 'write', result: { hop: 0, writes: [] } },
  { category: 'handoff', agent: 'support_agent', title: 'Handoff explícito', collection: 'agent_handoffs', op: 'write', result: { to_agent: 'product_agent', revisit: false, task: 'recomendar um fone' } },
  { category: 'agent', agent: 'product_agent', title: 'product_agent · tool calling (1 rodada(s): search_catalog)', result: { mode: 'tool_calling', tools_called: ['search_catalog'], writes: [] } },
  { category: 'handoff', agent: 'product_agent', title: 'Handoff explícito', collection: 'agent_handoffs', op: 'write', result: { to_agent: 'order_agent', revisit: false } },
  { category: 'agent', agent: 'order_agent', title: 'order_agent · tool calling (2 rodada(s): get_order, request_order_status_change)', result: { tools_called: ['get_order', 'request_order_status_change'], writes: ['request_order_status_change'] } },
  { category: 'agent', agent: 'order_agent', title: 'Tool get_order — consulta segura de pedido', collection: 'orders', op: 'read' },
  { category: 'agent', agent: 'order_agent', title: 'Atualização segura de status do pedido', collection: 'orders', op: 'write' },
  { category: 'handoff', agent: 'order_agent', title: 'Handoff explícito', result: { to_agent: 'logistics_agent' } },
  { category: 'handoff', agent: 'logistics_agent', title: 'Retorno controlado', result: { to_agent: 'order_agent', revisit: true, task: 'confirmar a troca' } },
  { category: 'coordination', agent: 'orchestrator', title: 'Orquestrador consolidou o quadro do turno numa resposta única', collection: 'turn_blackboard', op: 'read', result: { mode: 'llm', agents: ['support_agent', 'product_agent', 'order_agent', 'logistics_agent'] } },
  { category: 'memory', agent: 'order_agent', title: 'Episódio gravado em memória de longo prazo', collection: 'long_term_memory', op: 'write' },
  { category: 'guardrail', title: 'Guardrail de saída', result: { blocked: false } },
];

test('flagship turn reads as guardrail → route → plan → agents → consolidation → memory → output', () => {
  const steps = narrateTurn(flagship, { labels });
  assert.deepEqual(steps.map((s) => s.kind), ['guardrail', 'route', 'plan', 'agent', 'agent', 'agent', 'agent', 'consolidate', 'memory_out', 'guardrail_out']);
  const route = steps[1];
  assert.match(route.text, /regra determinística/);
  assert.match(route.text, /Suporte/);
  assert.ok(route.infos.some((note) => /cache semântico/.test(note)), 'cache MISS folds into routing');
  assert.deepEqual(route.focus.edges, [{ from: 'orchestrator', to: 'support_agent' }]);
  assert.equal(steps[2].items.length, 3);
  assert.equal(steps[2].items[1].label, 'Produtos');
});

test('agent steps say what was done, in plain words, and where it went next', () => {
  const steps = narrateTurn(flagship, { labels });
  const support = steps[3];
  assert.equal(support.agent, 'support_agent');
  assert.match(support.text, /consultou a base de conhecimento/);
  assert.equal(support.task, 'diagnosticar');
  assert.equal(support.next.agent, 'product_agent');
  assert.equal(describeNext(support.next, labels), 'passou a vez para Produtos: recomendar um fone');
  assert.match(support.why, /\$rankFusion/);
  assert.ok(support.mongo.some((ref) => ref.collection === 'kb_articles'));
  assert.ok(support.mongo.some((ref) => ref.collection === 'turn_blackboard'), 'blackboard entry attaches to its agent');
  assert.ok(!support.mongo.some((ref) => ref.collection === 'multiagent_brain.agent_registry'));
  const order = steps[5];
  assert.equal(order.status, 'write');
  // get_order tool + orders read collapse into one verb; the write appears once
  assert.equal(order.text, 'Pedidos consultou o pedido e registrou a mudança de status do pedido.');
  assert.match(order.why, /allowlist/);
  assert.deepEqual(order.focus.edges[0], { from: 'product_agent', to: 'order_agent' });
  const logistics = steps[6];
  assert.equal(logistics.next.revisit, true);
  assert.match(describeNext(logistics.next, labels), /^voltou para Pedidos para confirmar/);
});

test('mongo refs keep the timeline index so the chip can jump to the capô row', () => {
  const steps = narrateTurn(flagship, { labels });
  const kb = steps[3].mongo.find((ref) => ref.collection === 'kb_articles');
  assert.equal(kb.index, 5);
  assert.equal(kb.tag, '$rankFusion');
});

test('consolidation counts the specialists from the event', () => {
  const steps = narrateTurn(flagship, { labels });
  const consolidate = steps.find((s) => s.kind === 'consolidate');
  assert.match(consolidate.text, /os 4 especialistas/);
  assert.deepEqual(consolidate.focus.edges, [{ from: 'logistics_agent', to: 'orchestrator' }]);
});

test('blocked guardrail is a single blocked step', () => {
  const steps = narrateTurn([{ category: 'guardrail', title: 'Guardrail de entrada', result: { blocked: true, reason: 'denylist' } }], { labels });
  assert.equal(steps.length, 1);
  assert.equal(steps[0].status, 'blocked');
  assert.match(steps[0].text, /bloqueada/);
});

test('HITL pause and resume become their own steps', () => {
  const events = [
    { category: 'agent', agent: 'order_agent', title: 'Roteamento inicial', result: { target_agent: 'order_agent', source: 'rules' } },
    { category: 'agent', agent: 'order_agent', title: 'order_agent · tool calling (1 rodada(s): request_order_status_change)', result: { tools_called: ['request_order_status_change'], writes: [], pending_approval: true } },
    { category: 'agent', agent: 'order_agent', collection: 'pending_reviews', op: 'write', title: 'Escrita pausada: aguarda aprovação humana (interrupt no checkpoint)',
      result: { tool: 'request_order_status_change', args: { order_id: 'PED-4001', status: 'reembolsado' }, policy: 'HITL-REEMBOLSO' } },
    { category: 'agent', agent: 'order_agent', collection: 'pending_reviews', op: 'read', title: 'Turno retomado do checkpoint após decisão humana', result: { approved: true, tool: 'request_order_status_change' } },
  ];
  const steps = narrateTurn(events, { labels });
  const pause = steps.find((s) => s.kind === 'pause');
  assert.equal(pause.status, 'paused');
  assert.match(pause.text, /HITL-REEMBOLSO/);
  assert.match(pause.text, /nada foi alterado/);
  assert.equal(pause.mongo[0].collection, 'pending_reviews');
  const resume = steps.find((s) => s.kind === 'resume');
  assert.match(resume.text, /aprovou/);
  // the agent step before the pause did not write (the write was held)
  assert.equal(steps.find((s) => s.kind === 'agent').writes, 0);
});

test('cache hit, fan-out and replayed events', () => {
  const hit = narrateTurn([
    { category: 'cache', title: 'Cascata semântica: HIT (cache_global)', collection: 'semantic_cache', op: 'vectorSearch', result: { hit: true } },
    { category: 'agent', agent: 'support_agent', title: 'old', replayed: true },
  ], { labels });
  assert.deepEqual(hit.map((s) => s.kind), ['cache']);
  const fan = narrateTurn([{ category: 'fanout', title: 'Despacho paralelo', result: { agents: ['order_agent', 'billing_agent'] } }], { labels });
  assert.match(fan[0].text, /Pedidos e Cobrança rodaram ao mesmo tempo/);
  assert.equal(fan[0].focus.edges.length, 2);
});

test('live marks the last step as current; empty timeline has no steps', () => {
  assert.deepEqual(narrateTurn([], {}), []);
  const steps = narrateTurn(flagship.slice(0, 2), { labels, live: true });
  assert.equal(steps[steps.length - 1].current, true);
});

test('answer author: consolidated turns are signed by the orchestrator', () => {
  const run = { active_agent: 'logistics_agent', agent_path: ['support_agent', 'product_agent', 'order_agent', 'logistics_agent', 'order_agent'], timeline: flagship };
  assert.deepEqual(answerAuthor(run), { consolidated: true, count: 4, agent: 'logistics_agent' });
  assert.equal(authorLabel(4), 'Orquestrador · resposta consolidada de 4 agentes');
  assert.equal(answerAuthor({ active_agent: 'order_agent', agent_path: ['order_agent'], timeline: [] }).consolidated, false);
  assert.equal(answerAuthor({ active_agent: 'order_agent+billing_agent', timeline: [] }).count, 2);
  assert.equal(answerAuthor({ active_agent: 'support_agent', cache_hit: true, agent_path: ['a', 'b'] }).consolidated, false);
});

test('every step lists the timeline events it consumed; stepByEvent maps capô rows back to steps', async () => {
  const { stepByEvent } = await import('../src/turnNarrative.js');
  const steps = narrateTurn(flagship, { labels });
  const owner = stepByEvent(steps);
  assert.equal(owner.get(0).kind, 'guardrail');
  assert.equal(owner.get(1).kind, 'route');
  assert.equal(owner.get(2).kind, 'route', 'cache MISS belongs to the routing step');
  assert.equal(owner.get(3).kind, 'plan');
  assert.equal(owner.get(4).agent, 'support_agent');
  assert.equal(owner.get(6).agent, 'support_agent', 'blackboard entry of support_agent belongs to its step');
  assert.equal(owner.get(7).agent, 'support_agent', 'the handoff belongs to the agent that passed the turn');
  assert.equal(owner.get(13).agent, 'order_agent', 'a handoff without collection is still linked');
  assert.equal(owner.get(15).kind, 'consolidate');
  assert.equal(owner.get(16).kind, 'memory_out');
  assert.equal(owner.get(17).kind, 'guardrail_out', 'event without collection (only in the old Execução tab) is linked');
  // no event is claimed by two steps
  const all = steps.flatMap((s) => s.events);
  assert.equal(all.length, new Set(all).size);
});

test('events emitted before their step (blackboard opened before the plan) wait for it', async () => {
  const { stepByEvent } = await import('../src/turnNarrative.js');
  const steps = narrateTurn([
    { category: 'coordination', agent: 'orchestrator', title: 'Quadro do turno aberto', collection: 'turn_blackboard', op: 'write' },
    { category: 'coordination', agent: 'orchestrator', title: 'Plano do turno', collection: 'turn_blackboard', op: 'write', result: { plan: [{ agent: 'order_agent', task: 'x' }] } },
  ], { labels });
  assert.deepEqual(steps[0].events, [0, 1]);
  assert.equal(stepByEvent(steps).get(0).kind, 'plan');
});
