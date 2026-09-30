// Derive concurrency from recorded request intervals, never ledger insertion order.
export function describeExecution(run, timeline = []) {
  const calls = (run.llm_calls || []).filter(c => c.status !== 'circuit_open');
  const events = timeline.filter(e => !e.replayed);
  const parallel = events.some(e => e.category === 'fanout') || (!run.cache_hit && run.route_source === 'fanout');
  const sequential = events.some(e => e.category === 'handoff');
  const overlaps = calls.some((a, i) => calls.slice(i + 1).some(b => {
    const startA = Date.parse(a.started_at), startB = Date.parse(b.started_at);
    return a.agent !== b.agent && a.model !== b.model &&
      Number.isFinite(startA) && Number.isFinite(startB) &&
      a.latency_ms > 0 && b.latency_ms > 0 &&
      Math.max(startA, startB) < Math.min(startA + a.latency_ms, startB + b.latency_ms);
  }));
  const models = new Set(calls.map(c => c.model));
  const label = run.cache_hit ? 'Resposta reaproveitada' : parallel && overlaps
    ? 'Múltiplas LLMs em paralelo' : parallel ? 'Agentes em paralelo'
    : sequential ? 'Agentes em sequência' : models.size > 1 ? 'Múltiplas LLMs no turno' : 'Execução do turno';
  return { calls, label, overlaps, parallel, sequential };
}
