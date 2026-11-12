import test from 'node:test';
import assert from 'node:assert/strict';
import { buildCompareScenarios, compareWaitMs, summarizeCompare, isRateLimited } from '../src/compareScenarios.js';

const anaDemos = [
  { scenario_id: 'ana-controlled-return', message: 'meu Fone Pulse X do pedido PED-1001 chegou com defeito…' },
  { scenario_id: 'ana-parallel-fanout', message: 'onde está o meu pedido PED-1001 e qual é o valor e o vencimento da fatura FAT-1001?' },
  { scenario_id: 'ana-warranty-catalog', message: 'o Teclado Air do pedido PED-1002 ainda está na garantia?' },
];

test('five scenarios, messages from the identity demo scenarios when present', () => {
  const list = buildCompareScenarios('ana', anaDemos);
  assert.deepEqual(list.map((s) => s.id), ['simples', 'pedido-fatura', 'composto', 'garantia-catalogo', 'reembolso']);
  assert.equal(list[2].message, anaDemos[0].message);
  assert.equal(list[2].source, 'roteiro');
  assert.equal(list[0].message, 'onde está meu pedido PED-1001?');
  assert.equal(list[4].source, 'fixo');
  assert.ok(list.every((s) => s.tests.length > 20));
});

test('fixed messages only use ids that belong to the identity', () => {
  for (const [key, n] of [['ana', 1], ['bruno', 2], ['carla', 3], ['diego', 4]]) {
    const list = buildCompareScenarios(key, []);
    for (const scenario of list) {
      for (const id of scenario.message.match(/(PED|FAT)-\d+/g) || []) assert.equal(id.charAt(4), String(n), `${key}: ${id}`);
    }
  }
  const diego = buildCompareScenarios('diego', [{ scenario_id: 'diego-refund-return', message: 'solicite o reembolso do pedido PED-4001…' }]);
  assert.equal(diego[4].scenarioId, 'diego-refund-return');
  // another identity's scenario never leaks in
  const bruno = buildCompareScenarios('bruno', anaDemos);
  assert.ok(bruno.every((s) => !/PED-1/.test(s.message)));
});

test('rate-limit pacing: 3 calls per 60 s window', () => {
  const now = 100000;
  assert.equal(compareWaitMs([], now), 0);
  assert.equal(compareWaitMs([now - 10000, now - 5000], now), 0);
  assert.equal(compareWaitMs([now - 50000, now - 20000, now - 1000], now, { marginMs: 0 }), 10000);
  assert.equal(compareWaitMs([now - 70000, now - 20000, now - 1000], now), 0);
  assert.equal(compareWaitMs([now - 59000, now - 58000, now - 57000, now - 1000], now, { marginMs: 0 }), 2000);
});

test('summary marks the better arm per metric', () => {
  const sum = summarizeCompare({
    single: { latency_ms: 8900, cost_usd: 0.09, judge: { available: true, media: 3 } },
    multi: { latency_ms: 26500, cost_usd: 0.21, judge: { available: true, media: 4.33 } },
  });
  assert.equal(sum.faster, 'single');
  assert.equal(sum.cheaper, 'single');
  assert.equal(sum.betterJudge, 'multi');
  const offline = summarizeCompare({ single: { available: false }, multi: { available: false } });
  assert.equal(offline.single, null);
  assert.equal(offline.faster, null);
  assert.ok(isRateLimited('limite do A/B excedido (3 por minuto)'));
});
