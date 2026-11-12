import test from 'node:test';
import assert from 'node:assert/strict';
import { mongoOps, summarizeOps, opTags, compactJson } from '../src/mongoOps.js';
import { costByAgent, economicsByAgent, formatUsd } from '../src/costs.js';

test('ops keep timeline order and tag native Atlas capabilities', () => {
  const rows = mongoOps([
    { category: 'guardrail', title: 'Guardrail de saída' },
    { category: 'agent', agent: 'support_agent', title: 'KB com $rankFusion + $rerank', collection: 'kb_articles', op: 'hybridSearch', filter: { q: 'x' } },
    { category: 'agent', agent: 'order_agent', title: 'Cadeia', collection: 'orders', op: 'graphLookup' },
    { category: 'coordination', title: 'Quadro', collection: 'turn_blackboard', op: 'write' },
    { category: 'handoff', title: 'Handoff', collection: 'agent_handoffs', op: 'write' },
    { category: 'agent', title: 'Status', collection: 'orders', op: 'write' },
    { category: 'web', title: 'Busca', op: 'external', filter: { provider: 'tavily', query: 'q' } },
  ]);
  assert.equal(rows.length, 6);
  assert.deepEqual(rows[0].tags, ['$rankFusion', '$rerank']);
  assert.deepEqual(rows[1].tags, ['$graphLookup']);
  assert.ok(rows[2].tags.includes('quadro do turno'));
  assert.ok(rows[3].tags.includes('dispara change stream'));
  assert.ok(rows[4].tags.includes('escrita de negócio'));
  assert.equal(rows[5].collection, 'tavily');
  assert.deepEqual(summarizeOps(rows), { total: 5, writes: 3, native: 2, collections: 4 });
});

test('hybrid without rankFusion is labelled honestly; filters are masked and truncated', () => {
  assert.deepEqual(opTags({ op: 'hybridSearch', title: 'RAG híbrido com RRF na aplicação' }), ['BM25 + vetor']);
  assert.equal(compactJson({ email: 'a@b.c' }), '{"email":"<mascarado>"}');
  assert.equal(compactJson({ embedding: Array(32).fill(0.1) }), '{"embedding":"<32 floats omitidos>"}');
  assert.ok(compactJson({ long: 'x'.repeat(400) }, 50).endsWith('…'));
});

test('cost by agent: unknown stays unknown, circuit_open skipped', () => {
  const rows = costByAgent([
    { agent: 'order_agent', model: 'm', status: 'ok', input_tokens: 100, cache_read_tokens: 50, output_tokens: 20, estimated_cost_usd: 0.002 },
    { agent: 'order_agent', model: 'm', status: 'ok', input_tokens: 10, output_tokens: 5, estimated_cost_usd: 0.001 },
    { agent: 'billing_agent', model: 'm', status: 'error', estimated_cost_usd: null },
    { agent: 'billing_agent', status: 'circuit_open' },
  ]);
  assert.equal(rows[0].agent, 'order_agent');
  assert.equal(rows[0].calls, 2);
  assert.equal(rows[0].inputTokens, 160);
  assert.ok(Math.abs(rows[0].costUsd - 0.003) < 1e-9);
  assert.equal(rows[1].costUsd, null);
  assert.equal(rows[1].calls, 1);
});

test('economics report grouped per agent and per hour', () => {
  const out = economicsByAgent({ by_agent_hour: [
    { hour: '2026-11-12T10:00:00', agent: 'order_agent', calls: 2, cost_usd: 0.01, input_tokens: 100, output_tokens: 10 },
    { hour: '2026-11-12T11:00:00', agent: 'order_agent', calls: 1, cost_usd: 0.02, input_tokens: 50, output_tokens: 5 },
    { hour: '2026-11-12T11:00:00', agent: 'billing_agent', calls: 1, cost_usd: 0.005, input_tokens: 10, output_tokens: 1, cost_complete: false },
  ], cache: { hits: 3, llm_calls: 4, hit_rate: 0.4286, tokens_saved: 900 } });
  assert.equal(out.agents[0].agent, 'order_agent');
  assert.equal(out.agents[0].calls, 3);
  assert.equal(out.agents[1].costComplete, false);
  assert.equal(out.hours.length, 2);
  assert.ok(Math.abs(out.hours[1].costUsd - 0.025) < 1e-9);
  assert.deepEqual(out.cache, { hits: 3, misses: 4, hitRate: 0.4286, tokensSaved: 900 });
  assert.equal(economicsByAgent(null).agents.length, 0);
  assert.equal(formatUsd(null), '—');
});

test('hoodRows keeps every event: MongoDB ops, external search (not MongoDB) and lighter turn events', async () => {
  const { hoodRows, rowsForStep } = await import('../src/mongoOps.js');
  const events = [
    { category: 'guardrail', title: 'Guardrail de entrada', collection: 'guardrail_denylist', op: 'read' },
    { category: 'handoff', agent: 'order_agent', title: 'Handoff explícito', result: { to_agent: 'logistics_agent' } },
    { category: 'web', agent: 'logistics_agent', title: 'Busca web', op: 'external', filter: { provider: 'tavily', query: 'q' } },
    { category: 'agent', agent: 'support_agent', title: 'old', collection: 'kb_articles', op: 'read', replayed: true },
    { category: 'guardrail', title: 'Guardrail de saída', result: { blocked: false } },
  ];
  const rows = hoodRows(events);
  assert.deepEqual(rows.map((r) => r.index), [0, 1, 2, 3, 4]);
  assert.deepEqual(rows.map((r) => r.kind), ['op', 'event', 'external', 'op', 'event']);
  assert.equal(rows[1].categoryLabel, 'coordenação');
  assert.equal(rows[2].collection, 'tavily');
  assert.ok(rows[2].tags.includes('fora do MongoDB'));
  assert.equal(rows[3].replayed, true);
  assert.equal(rows[4].title, 'Guardrail de saída');
  assert.equal(rows[4].event, events[4], 'the row keeps the raw event for "ver detalhe"');
  assert.deepEqual(rowsForStep({ events: [4, 1] }, rows), [1, 4], 'rows of a step come back in capô order');
  assert.deepEqual(rowsForStep(null, rows), []);
});

test('result JSON in the detail masks secrets but keeps token counts', async () => {
  const { redactResult } = await import('../src/redact.js');
  assert.deepEqual(redactResult({ tokens_economizados: 1200, input_tokens: 9, email: 'a@b.c', token: 'abc', nested: { cpf: '1' } }),
    { tokens_economizados: 1200, input_tokens: 9, email: '<mascarado>', token: '<mascarado>', nested: { cpf: '<mascarado>' } });
});
