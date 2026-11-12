// Cost grouping for the "Custo" views. Unknown cost stays unknown (null), never zero:
// the backend only prices calls whose tariff and usage are known (economics.py).

export function costByAgent(llmCalls = []) {
  const groups = new Map();
  for (const call of llmCalls || []) {
    if (!call || call.status === 'circuit_open') continue;
    const key = call.agent || 'desconhecido';
    const row = groups.get(key) || { agent: key, calls: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
      costUsd: 0, costComplete: true, latencyMs: 0, models: [] };
    row.calls += 1;
    row.inputTokens += (call.input_tokens || 0) + (call.cache_read_tokens || 0) + (call.cache_write_tokens || 0);
    row.outputTokens += call.output_tokens || 0;
    row.cacheReadTokens += call.cache_read_tokens || 0;
    row.latencyMs += call.latency_ms || 0;
    if (call.model && !row.models.includes(call.model)) row.models.push(call.model);
    if (typeof call.estimated_cost_usd === 'number') row.costUsd += call.estimated_cost_usd;
    else row.costComplete = false;
    groups.set(key, row);
  }
  return [...groups.values()]
    .map((row) => ({ ...row, costUsd: row.costComplete ? row.costUsd : null, knownCostUsd: row.costUsd }))
    .sort((a, b) => (b.knownCostUsd - a.knownCostUsd) || (b.inputTokens + b.outputTokens) - (a.inputTokens + a.outputTokens));
}

/** Totals per agent and per hour from GET /api/economics (by_agent_hour rows). */
export function economicsByAgent(report) {
  const rows = report?.by_agent_hour || [];
  const agents = new Map();
  const hours = new Map();
  for (const row of rows) {
    const key = row.agent || 'desconhecido';
    const agent = agents.get(key) || { agent: key, calls: 0, costUsd: 0, tokens: 0, costComplete: true };
    agent.calls += row.calls || 0;
    agent.costUsd += row.cost_usd || 0;
    agent.tokens += (row.input_tokens || 0) + (row.output_tokens || 0);
    agent.costComplete = agent.costComplete && row.cost_complete !== false;
    agents.set(key, agent);
    const hour = hours.get(row.hour) || { hour: row.hour, costUsd: 0, calls: 0 };
    hour.costUsd += row.cost_usd || 0;
    hour.calls += row.calls || 0;
    hours.set(row.hour, hour);
  }
  const agentRows = [...agents.values()].sort((a, b) => b.costUsd - a.costUsd || b.tokens - a.tokens);
  const hourRows = [...hours.values()].sort((a, b) => String(a.hour).localeCompare(String(b.hour)));
  const cache = report?.cache || {};
  return {
    agents: agentRows,
    hours: hourRows,
    maxAgentCost: Math.max(0, ...agentRows.map((row) => row.costUsd)),
    maxHourCost: Math.max(0, ...hourRows.map((row) => row.costUsd)),
    totalCostUsd: agentRows.reduce((sum, row) => sum + row.costUsd, 0),
    cache: { hits: cache.hits || 0, misses: cache.llm_calls || 0, hitRate: cache.hit_rate ?? null, tokensSaved: cache.tokens_saved || 0 },
  };
}

export function formatUsd(value, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `$${value.toFixed(digits)}`;
}

export function formatTokens(value) {
  return (value || 0).toLocaleString('pt-BR');
}
