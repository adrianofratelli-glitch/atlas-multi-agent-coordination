import test from 'node:test';
import assert from 'node:assert/strict';
import { describeExecution as describe } from '../src/execution.js';
const a = {agent:'order_agent', model:'luna', status:'ok', started_at:'2026-09-19T12:00:00Z', latency_ms:1000};
const b = {...a, agent:'billing_agent', model:'haiku', started_at:'2026-09-19T12:00:00.500Z'};
test('concurrent different models and agents, regardless of completion order', () => {
  assert.equal(describe({route_source:'fanout', llm_calls:[b,a]}).label, 'Múltiplas LLMs em paralelo');
  assert.equal(describe({route_source:'fanout', llm_calls:[a,{...b,started_at:'2026-09-19T12:00:02Z'}]}).label, 'Agentes em paralelo');
});
test('fallback, circuit skips and cache do not imply concurrent agents', () => {
  assert.equal(describe({llm_calls:[a,{...b,agent:a.agent,fallback:true}]}).overlaps, false);
  assert.equal(describe({llm_calls:[a,{...b,status:'circuit_open'}]}).calls.length, 1);
  assert.equal(describe({cache_hit:true,route_source:'fanout'}).label, 'Resposta reaproveitada');
});
test('handoffs prove sequence; replayed history is not fresh execution', () => {
  assert.equal(describe({},[{category:'handoff'}]).label, 'Agentes em sequência');
  assert.equal(describe({},[{category:'fanout',replayed:true}]).parallel, false);
});
