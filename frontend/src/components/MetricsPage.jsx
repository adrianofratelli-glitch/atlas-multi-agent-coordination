import PageHead from './PageHead.jsx';
import { EconomicsPanel } from './CostPanel.jsx';

export default function MetricsPage({ metrics, evalRuns, adminMode, labels = {}, refreshKey }) {
  const counters = metrics?.counters || {};
  const route = metrics?.routes?.chat || {};
  const cacheHits = Object.entries(counters).filter(([key]) => key.startsWith('cache.hits.')).reduce((sum, [, value]) => sum + value, 0);
  const cacheMisses = counters['cache.misses'] || 0;
  const cacheTotal = cacheHits + cacheMisses;
  const specialistAgents = Object.entries(counters)
    .filter(([key, value]) => /^agent\.(?!orchestrator)[^.]+\.turns$/.test(key) && value > 0)
    .length;
  const handoffCount = Object.entries(counters)
    .filter(([key]) => key.endsWith('.handoffs'))
    .reduce((sum, [, value]) => sum + value, 0);
  const businessCollections = new Set(['orders', 'support_tickets', 'redemptions', 'shipments']);
  const businessWrites = Object.entries(counters)
    .filter(([key]) => key.startsWith('collection.') && key.endsWith('.write') && businessCollections.has(key.slice('collection.'.length, -'.write'.length)))
    .reduce((sum, [, value]) => sum + value, 0);
  const nativeSearches = Object.entries(counters)
    .filter(([key]) => key.startsWith('collection.') && (key.endsWith('.vectorSearch') || key.endsWith('.hybridSearch') || key.endsWith('.graphLookup')))
    .reduce((sum, [, value]) => sum + value, 0);
  const collectionRows = Object.entries(counters).reduce((rows, [key, value]) => {
    if (!key.startsWith('collection.')) return rows;
    const parts = key.slice('collection.'.length).split('.');
    const op = parts.pop();
    const collection = parts.join('.');
    const row = rows.get(collection) || { collection, read: 0, write: 0, vectorSearch: 0, hybridSearch: 0, changeStream: 0, graphLookup: 0 };
    row[op] = value;
    rows.set(collection, row);
    return rows;
  }, new Map());
  const cards = [
    ['turnos concluídos', counters['route.chat.ok'] || 0, 'API /api/chat'],
    ['agentes exercitados', `${specialistAgents}/${Math.max(1, Object.keys(labels).filter((key) => key !== 'orchestrator').length || 7)}`, 'especialistas com atuação real'],
    ['handoffs', handoffCount, `${counters['coordination.revisits'] || 0} retornos controlados`],
    ['escritas de negócio', businessWrites, 'orders · tickets · resgates · entregas'],
    ['operações nativas', nativeSearches, 'Vector Search · $rankFusion · $graphLookup'],
    ['latência p95', route.p95_ms ? `${Math.round(route.p95_ms)} ms` : '—', `${route.count || 0} amostras`],
    ['cache hit rate', cacheTotal ? `${Math.round((cacheHits / cacheTotal) * 100)}%` : '—', `${cacheHits} hits · ${cacheMisses} misses`],
    ['tokens economizados', (counters['tokens.economizados'] || 0).toLocaleString('pt-BR'), 'estimados, evitados por HIT na cascata'],
    ['guardrails', counters['guardrails.blocked'] || 0, 'turnos bloqueados'],
  ];

  return (
    <section className="full-page-section">
      <PageHead source="llm_usage_ts · agent_traces" title="O valor aparece no fluxo, não no log">
        Cobertura, coordenação e operações MongoDB em uma leitura executiva, e o custo por agente lido da time series.
      </PageHead>
      <div className="metric-grid">
        {cards.map(([label, value, note]) => <article className="metric-card" key={label}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>)}
      </div>
      <section className="page-block">
        <h3>Custo por agente e economia do cache <code>/api/economics</code></h3>
        <EconomicsPanel labels={labels} refreshKey={refreshKey} />
      </section>
      <div className="metric-ledger">
        <div className="panel-label"><span>coleções em operação</span><code>collection.*</code></div>
        {collectionRows.size === 0
          ? <p className="metric-empty">Execute um cenário no Chat para materializar leituras, escritas e buscas.</p>
          : <div className="metric-table-wrap"><table><thead><tr><th>collection</th><th>read</th><th>write</th><th>vector</th><th>hybrid</th><th>graph</th></tr></thead><tbody>
            {[...collectionRows.values()].sort((a, b) => a.collection.localeCompare(b.collection)).map((row) => <tr key={row.collection}><td><code>{row.collection}</code></td><td>{row.read}</td><td className={row.write ? 'hot' : ''}>{row.write}</td><td>{row.vectorSearch}</td><td>{row.hybridSearch}</td><td className={row.graphLookup ? 'hot' : ''}>{row.graphLookup}</td></tr>)}
          </tbody></table></div>}
      </div>
      <div className="eval-panel">
        <div className="panel-label"><span>qualidade · GoalSuccessRate</span><code>eval_runs</code></div>
        {!adminMode && <p className="metric-empty">Ligue o modo admin na aba Agentes ou Decisões para consultar o histórico de avaliação.</p>}
        {adminMode && !evalRuns.length && <p className="metric-empty">Nenhuma execução registrada. Rode <code>python eval.py</code> no backend.</p>}
        {adminMode && evalRuns.map((run) => (
          <div className="eval-run" key={run.at}>
            <b className={run.pass_rate === 1 ? 'ok' : ''}>{Math.round(run.pass_rate * 100)}%</b>
            <span>{run.passed}/{run.total} casos · {run.label || 'baseline'}</span>
            <span>p95: {run.p95_ms != null ? `${Math.round(run.p95_ms)} ms` : '—'}</span>
            <span>USD por sucesso: {run.cost_per_success_usd != null ? run.cost_per_success_usd.toFixed(6) : 'não disponível'}</span>
            <code>{new Date(run.at).toLocaleString('pt-BR')}</code>
          </div>
        ))}
      </div>
      <details className="raw-metrics"><summary>Ver payload técnico</summary><pre>{JSON.stringify(metrics, null, 2)}</pre></details>
    </section>
  );
}
