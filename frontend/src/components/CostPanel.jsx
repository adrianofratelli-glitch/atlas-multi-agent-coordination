import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { costByAgent, economicsByAgent, formatUsd, formatTokens } from '../costs.js';
import { describeExecution } from '../execution.js';

function Bar({ value, max, label }) {
  const share = max > 0 ? Math.max(0.02, value / max) : 0;
  return <span className="bar" role="presentation" title={label}><i style={{ transform: `scaleX(${share})` }} /></span>;
}

/** Custo do último turno, por agente (llm_calls do ChatResponse). */
export function TurnCost({ run, timeline = [], labels = {} }) {
  if (!run) return <p className="panel-empty">O custo por agente aparece depois do primeiro turno.</p>;
  const rows = costByAgent(run.llm_calls);
  const execution = describeExecution(run, timeline);
  const byCost = rows.some((row) => row.knownCostUsd > 0);
  const max = Math.max(0, ...rows.map((row) => (byCost ? row.knownCostUsd : row.inputTokens + row.outputTokens)));
  const cacheRead = run.usage?.cache_read || 0;
  return (
    <div className="cost">
      <dl className="cost-summary">
        <div><dt>custo estimado do turno</dt><dd>{formatUsd(run.economics?.estimated_cost_usd, 5)}</dd></div>
        <div><dt>cascata semântica</dt><dd className={run.cache_hit ? 'is-hit' : ''}>{run.cache_hit ? `HIT · ${run.cache_source || 'cache'}` : 'MISS'}</dd></div>
        <div><dt>chamadas ao modelo</dt><dd>{execution.calls.length}</dd></div>
        <div><dt>prompt cache do provedor</dt><dd>{cacheRead ? `${formatTokens(cacheRead)} tokens` : '—'}</dd></div>
      </dl>
      {run.cache_hit && <p className="cost-note">Resposta reaproveitada do MongoDB: ~{formatTokens(run.tokens_economizados)} tokens evitados, nenhum agente chamou o modelo.</p>}
      {!run.cache_hit && !rows.length && <p className="cost-note">Nenhuma chamada ao modelo neste turno: os agentes responderam de forma determinística a partir dos documentos.</p>}
      {rows.length > 0 && (
        <table className="cost-table">
          <caption>Por agente, {byCost ? 'ordenado por custo' : 'ordenado por tokens (custo indisponível)'}</caption>
          <thead><tr><th>agente</th><th>chamadas</th><th>tokens (entrada / saída)</th><th>custo</th><th aria-hidden="true" /></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.agent}>
                <td>{labels[row.agent] || row.agent}<small>{row.models.join(', ')}</small></td>
                <td>{row.calls}</td>
                <td>{formatTokens(row.inputTokens)} / {formatTokens(row.outputTokens)}</td>
                <td>{row.costUsd === null ? 'não informado' : formatUsd(row.costUsd, 5)}</td>
                <td><Bar value={byCost ? row.knownCostUsd : row.inputTokens + row.outputTokens} max={max} label={`${labels[row.agent] || row.agent}`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {run.economics?.cost_bases?.includes('historical_blended_estimate') && <p className="cost-note">Estimativa pelas médias observadas por modelo no gateway; não é cobrança exata.</p>}
    </div>
  );
}

const WINDOWS = [[1, '1 h'], [6, '6 h'], [24, '24 h'], [168, '7 dias']];

/** Custo por agente ao longo do tempo (time series `llm_usage_ts`, GET /api/economics). */
export function EconomicsPanel({ labels = {}, refreshKey }) {
  const [hours, setHours] = useState(24);
  const [report, setReport] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    api.economics(hours).then((data) => { if (!cancelled) { setReport(data); setError(''); } })
      .catch((err) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [hours, refreshKey]);
  const data = economicsByAgent(report);
  const cacheTotal = data.cache.hits + data.cache.misses;
  return (
    <div className="economics">
      <div className="economics-controls" role="group" aria-label="Janela de tempo">
        {WINDOWS.map(([value, label]) => (
          <button type="button" key={value} aria-pressed={hours === value} className={hours === value ? 'is-on' : ''} onClick={() => setHours(value)}>{label}</button>
        ))}
        <code>{report?.source === 'timeseries' ? 'llm_usage_ts · $facet' : report?.source || '…'}</code>
      </div>
      {error && <p className="panel-empty">Não foi possível ler a economia: {error}</p>}
      <div className="economics-grid">
        <section>
          <h4>Cache semântico: HIT × MISS</h4>
          {cacheTotal ? (
            <>
              <div className="split-bar" role="img" aria-label={`${data.cache.hits} respostas do cache e ${data.cache.misses} chamadas ao modelo`}>
                <i className="is-hit" style={{ flexGrow: data.cache.hits }} />
                <i className="is-miss" style={{ flexGrow: data.cache.misses }} />
              </div>
              <p className="split-legend"><span><b>{data.cache.hits}</b> HIT (sem modelo)</span><span><b>{data.cache.misses}</b> chamadas ao modelo</span><span>{formatTokens(data.cache.tokensSaved)} tokens evitados</span></p>
            </>
          ) : <p className="panel-empty">Sem turnos nesta janela.</p>}
        </section>
        <section>
          <h4>Custo por agente · {formatUsd(data.totalCostUsd, 4)}</h4>
          {data.agents.length ? (
            <table className="cost-table">
              <thead><tr><th>agente</th><th>chamadas</th><th>tokens</th><th>custo</th><th aria-hidden="true" /></tr></thead>
              <tbody>
                {data.agents.map((row) => (
                  <tr key={row.agent}>
                    <td>{labels[row.agent] || row.agent}</td><td>{row.calls}</td><td>{formatTokens(row.tokens)}</td>
                    <td>{formatUsd(row.costUsd, 4)}{row.costComplete ? '' : '*'}</td>
                    <td><Bar value={row.costUsd} max={data.maxAgentCost} label={labels[row.agent] || row.agent} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="panel-empty">Nenhuma chamada ao modelo registrada nesta janela{report?.source === 'memory' ? ' (modo demonstração sem LLM)' : ''}.</p>}
          {data.agents.some((row) => !row.costComplete) && <p className="cost-note">* parte das chamadas sem tarifa conhecida: o valor é o mínimo conhecido.</p>}
        </section>
        {data.hours.length > 1 && (
          <section className="economics-hours">
            <h4>Custo por hora</h4>
            <div className="hour-columns">
              {data.hours.map((row) => (
                <span key={row.hour} className="hour-col" title={`${new Date(row.hour).toLocaleString('pt-BR')}: ${formatUsd(row.costUsd, 4)} · ${row.calls} chamadas`}>
                  <i style={{ transform: `scaleY(${data.maxHourCost ? Math.max(0.03, row.costUsd / data.maxHourCost) : 0})` }} />
                </span>
              ))}
            </div>
            <details><summary>Ver tabela</summary>
              <table className="cost-table"><thead><tr><th>hora</th><th>chamadas</th><th>custo</th></tr></thead>
                <tbody>{data.hours.map((row) => <tr key={row.hour}><td>{new Date(row.hour).toLocaleString('pt-BR')}</td><td>{row.calls}</td><td>{formatUsd(row.costUsd, 4)}</td></tr>)}</tbody>
              </table>
            </details>
          </section>
        )}
      </div>
    </div>
  );
}
