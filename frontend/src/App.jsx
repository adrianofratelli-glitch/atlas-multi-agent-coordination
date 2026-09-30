import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';
import { describeExecution } from './execution.js';
import Timeline from './components/Timeline.jsx';
import ReplacementChain from './components/ReplacementChain.jsx';
import CompliancePage from './components/CompliancePage.jsx';

const NAV = ['Chat', 'Decisões', 'Métricas'];
// O rótulo do híbrido não cita mais o RRF na aplicação: a fusão passou a rodar server-side
// com $rankFusion, e o título do evento diz qual dos dois caminhos rodou de fato.
const OP_LABELS = { read: 'leitura', write: 'escrita', vectorSearch: '$vectorSearch', hybridSearch: 'híbrido BM25 + vetor', changeStream: 'change stream', graphLookup: '$graphLookup' };
const IDENTITIES = ['ana', 'bruno', 'carla', 'diego'];

// Prova visual do pitch "MongoDB reduz custo de LLM" direto na PoV, sem precisar abrir o
// Langfuse: cascata semântica (curto prazo + semantic_cache, HIT = zero chamada ao LLM,
// dado real de cascade_lookup) e prompt cache da Anthropic (cache_read/cache_write vêm do
// budget real do turno, orchestration.py:usage).
function MongoCacheSavings({ run, timeline = [], agentLabels = {} }) {
  if (!run) return null;
  const execution = describeExecution(run, timeline);
  const cacheRead = run.usage?.cache_read || 0;
  const cacheWrite = run.usage?.cache_write || 0;
  const promptTotal = (run.llm_calls || []).reduce((sum, call) => sum + (call.input_tokens || 0) + (call.cache_read_tokens || 0) + (call.cache_write_tokens || 0), 0);
  const promptPct = promptTotal > 0 ? Math.round((cacheRead / promptTotal) * 100) : 0;
  return (
    <details className="turn-summary">
      <summary>
        <span className="turn-summary-label">Resumo do turno</span>
        <span><strong>{run.economics?.estimated_cost_usd != null ? `$${run.economics.estimated_cost_usd.toFixed(4)}` : '—'}</strong> <span className="dim">USD estimados</span></span>
        <span>{run.cache_hit ? 'Resposta em cache' : 'Nova resposta'}</span>
        <span>{execution.calls.length} chamadas</span>
        {!run.cache_hit && (execution.parallel || execution.sequential) && <span className="execution-flag">{execution.label}</span>}
        <span className="turn-summary-more">Detalhes</span>
      </summary>
      <div className="turn-summary-body">
      <div className="cache-savings-row">
        <span>Cascata semântica (curto prazo + Atlas Vector Search)</span>
        {run.cache_hit
          ? <b className="cache-savings-hit">HIT ({run.cache_source}) — resposta reaproveitada (~{run.tokens_economizados ?? 0} tokens estimados evitados)</b>
          : <span className="dim">MISS — execução do fluxo</span>}
      </div>
      <div className="cache-savings-row">
        <span>Custo estimado de LLM neste turno</span>
        <b>{run.economics?.estimated_cost_usd != null ? `$${run.economics.estimated_cost_usd.toFixed(6)} USD` : 'Não disponível — tarifa ou consumo ausente'}</b>
      </div>
      {run.economics?.cost_bases?.includes('historical_blended_estimate') && <small>Estimativa pelas médias observadas por modelo no Grove; não representa cobrança exata.</small>}
      <div className="cache-savings-row">
        <span>{execution.label}</span>
        <span>{execution.calls.length} chamadas · {[...new Set(execution.calls.map(c => `${agentLabels[c.agent] || c.agent}: ${c.model}`))].join(' · ') || 'Sem chamada ao modelo'}</span>
      </div>
      {execution.parallel && !run.cache_hit && <small>{execution.overlaps ? 'Chamadas a modelos diferentes se sobrepuseram no tempo. Pedido e fatura são consultas independentes; os resultados são reunidos na resposta.' : 'Agentes despachados em paralelo; sem evidência de chamadas simultâneas a modelos diferentes neste turno.'}</small>}
      {execution.calls.some(c => c.fallback) && <small>Modelo alternativo acionado (fallback). Consulte as chamadas abaixo para identificar o agente.</small>}
      {!!run.llm_calls?.length && <details><summary>Consumo por chamada</summary>
        {run.llm_calls.map((call, i) => <div className="cache-savings-row" key={i}>
          <span>{call.agent} · {call.model}{call.fallback ? ' · fallback' : ''}</span>
          <span>{call.status} · {Math.round(call.latency_ms || 0)} ms · {call.usage_known ? `${(call.input_tokens || 0) + (call.cache_read_tokens || 0) + (call.cache_write_tokens || 0)} entrada / ${call.output_tokens || 0} saída` : 'consumo não informado'}</span>
        </div>)}
      </details>}
      {cacheRead > 0 && (
        <div className="cache-savings-row">
          <span>Prompt cache do provedor</span>
          <b className="cache-savings-hit">{cacheRead} tokens reaproveitados ({promptPct}% deste turno{cacheWrite ? `, ${cacheWrite} escritos no cache` : ''})</b>
        </div>
      )}
      </div>
    </details>
  );
}

function ChatPanel({ messages, input, setInput, send, busy, customerName, demos, suggestions, onSuggestion }) {
  return (
    <section className="chat-panel">
      <div className="panel-label">canal do cliente</div>
      <div className="messages">
        {messages.length === 0 && (
          <div className="welcome-message"><span>Olá, {customerName || 'cliente'}.</span><p>Em que posso ajudar com seu pedido, produto ou cobrança?</p></div>
        )}
        {messages.map((message, index) => (
          <div className={`message ${message.role}`} key={index}>
            <small>{message.role === 'user' ? 'você' : message.agent || 'assistente'}</small>
            {message.role === 'assistant' && (
              <span className={`cache-badge ${message.cacheHit ? 'hit' : 'miss'}`}>
                {message.cacheHit ? '⚡ cache hit · 0 tokens' : `cache miss · ${message.tokens ?? 0} tokens`}
              </span>
            )}
            <div>{message.text}</div>
          </div>
        ))}
      </div>
      {suggestions?.length > 0 && !busy && (
        /* Próximos passos derivados de query: cada chip carrega a mensagem exata que
           dispara, então clicar sempre resolve — nunca leva a um "não encontrei". */
        <div className="suggestions" aria-label="Próximos passos sugeridos">
          <span className="suggestions-label">posso seguir com</span>
          {suggestions.map((item) => (
            <button type="button" className="suggestion-chip" key={item.topic + item.label} onClick={() => onSuggestion(item.message)}>
              {item.label}
            </button>
          ))}
        </div>
      )}
      <form className="chat-form" onSubmit={(event) => { event.preventDefault(); send(); }}>
        <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } }} placeholder="Digite sua solicitação… (Enter envia, Shift+Enter quebra linha)" rows="3" />
        <button className="send-button" disabled={busy || !input.trim()}>{busy ? 'Coordenando…' : 'Enviar turno'}<span>↗</span></button>
      </form>
      <label className="demo-picker">
        <span>roteiro rápido</span>
        <select value="" onChange={(event) => {
          const demo = demos[Number(event.target.value)];
          if (demo) {
            setInput(demo.message);
          }
        }}>
          <option value="">Carregar um cenário de demonstração…</option>
          {demos.map((demo, index) => <option key={demo.scenario_id} value={index}>{demo.label}</option>)}
        </select>
      </label>
    </section>
  );
}

function AgentsPage({ agents, adminMode, setAdminMode, reload }) {
  const toggle = async (agent) => {
    if (!adminMode) return;
    await api.updateAgent(agent.agent_key, { active: !agent.active });
    reload();
  };
  return (
    <section className="full-page-section">
      <div className="section-copy">
        <span className="eyebrow">multiagent_brain.agent_registry</span><h2>O time de agentes é uma collection.</h2>
        <p>Modelo, persona, ferramentas e budget mudam por documento — sem deploy. {agents.length} agentes reais, todos respondem de fato — nenhum documento de enfeite.</p>
        <label className="admin-toggle">
          <input type="checkbox" checked={adminMode} onChange={(event) => setAdminMode(event.target.checked)} />
          <span className="admin-toggle-track"><span className="admin-toggle-thumb" /></span>
          modo admin {adminMode ? '(ligado)' : '(desligado)'}
        </label>
      </div>
      <div className="agent-grid">{agents.map((agent) => (
        <article className={`agent-card ${agent.active ? '' : 'disabled'}`} key={agent.agent_key}>
          <div className="agent-card-head"><span className="agent-index">{agent.agent_key === 'orchestrator' ? 'Ø' : agent.agent_key.charAt(0).toUpperCase()}</span><button className="switch" disabled={!adminMode} aria-label={`Ativar ${agent.label}`} aria-pressed={agent.active} onClick={() => toggle(agent)}><span /></button></div>
          <h3>{agent.label}</h3><code>{agent.agent_key}</code><p>{agent.persona}</p>
          <dl><div><dt>modelo</dt><dd>{agent.model}</dd></div><div><dt>budget</dt><dd>{agent.max_turn_tokens} tok</dd></div><div><dt>tools</dt><dd>{agent.allowed_tools.join(', ')}</dd></div></dl>
        </article>
      ))}</div>
    </section>
  );
}

function DataPage({ title, subtitle, children }) {
  return <section className="full-page-section"><div className="section-copy"><span className="eyebrow">plano de coordenação</span><h2>{title}</h2><p>{subtitle}</p></div><div className="data-surface">{children}</div></section>;
}

function MetricsPage({ metrics, evalRuns, adminMode }) {
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
    ['agentes exercitados', `${specialistAgents}/7`, 'especialistas com atuação real'],
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
      <div className="section-copy">
        <span className="eyebrow">observabilidade operacional</span>
        <h2>O valor aparece no fluxo, não no log.</h2>
        <p>Cobertura, coordenação e operações MongoDB em uma leitura executiva. Tokens e latência continuam no payload técnico, mas não são tratados como números de sucesso.</p>
      </div>
      <div className="metric-grid">
        {cards.map(([label, value, note]) => <article className="metric-card" key={label}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>)}
      </div>
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
        {!adminMode && <p className="metric-empty">Ligue o modo admin na aba Decisões para consultar o histórico de avaliação.</p>}
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

export default function App() {
  const [nav, setNav] = useState('Chat');
  const [health, setHealth] = useState(null);
  const [customer, setCustomer] = useState(null);
  const [agents, setAgents] = useState([]);
  const [demoScenarios, setDemoScenarios] = useState([]);
  const [handoffs, setHandoffs] = useState([]);
  const [memory, setMemory] = useState([]);
  const [guardrails, setGuardrails] = useState([]);
  const [metrics, setMetrics] = useState({});
  const [messages, setMessages] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const operationRef = useRef(false);
  const [error, setError] = useState('');
  const [conversationId, setConversationId] = useState(null);
  const [lastRun, setLastRun] = useState(null);
  const [suggestions, setSuggestions] = useState([]);
  const [adminMode, setAdminMode] = useState(false);
  const [liveStatus, setLiveStatus] = useState('connecting');
  const [liveError, setLiveError] = useState('');
  const [evalRuns, setEvalRuns] = useState([]);

  const loadCore = async () => {
    const [h, a] = await Promise.all([api.health(), api.agents()]);
    setHealth(h); setAgents(a);
  };
  // o boot abre sempre em conversa nova: uma demo que começa com o histórico do ensaio anterior
  // na tela parece que o cliente já falou com o agente. a retomada continua valendo ao trocar de
  // identidade no meio da sessão, que é quando ela realmente evita um painel vazio.
  const switchIdentity = async (customerKey, { resume = true } = {}) => {
    if (operationRef.current) return;
    operationRef.current = true; setBusy(true); setError('');
    // Never display the previous identity's conversation while its replacement loads.
    setCustomer(null); setConversationId(null); setMessages([]); setTimeline([]); setSuggestions([]); setLastRun(null);
    try {
      const who = await api.login(customerKey); setCustomer(who); await loadCore();
      const [mem, gr, met, lastConv, scenarios] = await Promise.all([api.memory(who.customer_key), api.guardrails('events'), api.metrics(), api.latestConversation(), api.demoScenarios()]);
      setMemory(mem); setGuardrails(gr); setMetrics(met);
      setDemoScenarios(scenarios);
      setHandoffs([]);
      if (resume && lastConv?.turns?.length) {
        setConversationId(lastConv.conversation_id);
        setMessages(lastConv.turns.map((turn) => ({ role: turn.role, agent: lastConv.active_agent, text: turn.content })));
        // sem isso a conversa retomada mostra o texto certo mas raio-x/esteira vazios — parece que o
        // multi-agent não rodou, quando só faltava recarregar o registro do último turno.
        setTimeline(lastConv.last_timeline || []);
        setLastRun(lastConv.last_timeline ? { active_agent: lastConv.active_agent, usage: lastConv.last_usage || {}, llm_calls: lastConv.last_llm_calls || [], economics: lastConv.last_economics || {} } : null);
      } else {
        setConversationId(null); setMessages([]); setTimeline([]); setLastRun(null); setSuggestions([]);
      }
    } catch (err) { setCustomer(null); setError(err.message); }
    finally { operationRef.current = false; setBusy(false); }
  };
  useEffect(() => { api.warmup(); switchIdentity('ana', { resume: false }); }, []);

  useEffect(() => {
    if (nav === 'Métricas' && adminMode) {
      api.evalRuns().then(setEvalRuns).catch(() => setEvalRuns([]));
    }
  }, [nav, adminMode]);

  useEffect(() => {
    if (!customer) return undefined;
    let cancelled = false;
    let controller = null;
    let running = false;
    const connect = async (activeController) => {
      running = true;
      while (!cancelled && !activeController.signal.aborted) {
        try {
          setLiveStatus('connecting');
          await api.streamEvents(() => {}, activeController.signal, () => {
            setLiveStatus('live');
            setLiveError('');
          });
        } catch (err) {
          if (!activeController.signal.aborted) setLiveError(err.message || 'Conexão encerrada');
        }
        if (!cancelled && !activeController.signal.aborted) setLiveStatus('offline');
        if (!cancelled && !activeController.signal.aborted) await new Promise((resolve) => setTimeout(resolve, 3000));
      }
      running = false;
      if (!cancelled && document.visibilityState === 'visible') {
        setTimeout(syncConnection, 0);
      }
    };
    const syncConnection = () => {
      if (document.visibilityState === 'visible') {
        if (running) return;
        controller = new AbortController();
        connect(controller);
      } else {
        controller?.abort();
        controller = null;
        setLiveStatus('offline');
      }
    };
    syncConnection();
    document.addEventListener('visibilitychange', syncConnection);
    return () => {
      cancelled = true;
      controller?.abort();
      document.removeEventListener('visibilitychange', syncConnection);
    };
  }, [customer?.customer_key]);

  const newConversation = () => {
    if (operationRef.current) return;
    setConversationId(null); setMessages([]); setTimeline([]); setLastRun(null); setHandoffs([]); setInput(''); setSuggestions([]);
    setGuardrails([]); setMetrics({});
  };

  const resetDemoMemory = async () => {
    if (operationRef.current || !customer) return;
    operationRef.current = true; setBusy(true); setError('');
    try {
      await api.demoReset();
      setMemory(await api.memory(customer.customer_key));
      operationRef.current = false;
      newConversation();
    } catch (err) { setError(err.message); }
    finally { operationRef.current = false; setBusy(false); }
  };

  const send = async (override) => {
    const raw = typeof override === 'string' ? override : input;
    if (!raw.trim() || operationRef.current || !customer) return;
    operationRef.current = true;
    const value = raw.trim(); setInput(''); setBusy(true); setError(''); setSuggestions([]);
    setMessages((items) => [...items, { role: 'user', text: value }]);
    try {
      const run = await api.chat(value, conversationId);
      setConversationId(run.conversation_id); setTimeline(run.timeline); setLastRun(run); setSuggestions(run.suggestions || []);
      const longTermUsed = (run.timeline || []).some((event) => event.collection === 'long_term_memory');
      setMessages((items) => [...items, { role: 'assistant', agent: run.active_agent, text: run.response, cacheHit: run.cache_hit, cacheSource: run.cache_source, tokens: run.usage?.total ?? 0, longTermUsed }]);
      const [hs, met, gr, mem] = await Promise.all([api.handoffs(run.conversation_id), api.metrics(), api.guardrails('events'), api.memory(customer.customer_key)]);
      setHandoffs(hs); setMetrics(met); setGuardrails(gr); setMemory(mem);
    } catch (err) { setError(err.message); }
    finally { operationRef.current = false; setBusy(false); }
  };

  const agentLabels = useMemo(() => Object.fromEntries(agents.map((agent) => [agent.agent_key, agent.label])), [agents]);
  const cast = useMemo(() => {
    const sequence = (timeline || [])
      .filter((event) => event.agent && event.category !== 'cache')
      .map((event) => event.agent);
    return sequence.filter((agent, index) => agent !== sequence[index - 1]);
  }, [timeline]);

  const collectionsTouched = useMemo(() => {
    const byCollection = new Map();
    for (const event of timeline || []) {
      if (!event.collection || !event.op || event.replayed) continue;
      if (!byCollection.has(event.collection)) byCollection.set(event.collection, []);
      byCollection.get(event.collection).push({ op: event.op, agent: event.agent, title: event.title });
    }
    return [...byCollection.entries()];
  }, [timeline]);

  return (
    <div data-pov-shell>
      <a className="pov-skip-link" href="#conteudo-principal">Pular para o conteúdo</a>
      <nav className="top-nav"><div className="nav-inner"><button className="brand" onClick={() => setNav('Chat')} aria-label="Ir para o Chat"><span className="leaf">◆</span><span>MongoDB</span><b>Agent Control Plane</b></button><div className="nav-tabs">{NAV.map((item) => <button className={nav === item ? 'active' : ''} aria-current={nav === item ? 'page' : undefined} key={item} onClick={() => setNav(item)}>{item}</button>)}</div><label className="identity-select">identidade<select disabled={busy} value={customer?.customer_key || 'ana'} onChange={(event) => switchIdentity(event.target.value)}>{IDENTITIES.map((key) => <option key={key} value={key}>{key}</option>)}</select></label><div className="live-pill" title={liveError || 'Change Stream do MongoDB Atlas em agent_handoffs'}><span className={liveStatus === 'live' ? 'ok' : liveStatus === 'connecting' ? 'connecting' : ''} />{liveStatus === 'live' ? 'ao vivo' : liveStatus === 'connecting' ? 'conectando' : 'offline'}</div><div className="health-pill"><span className={health ? 'ok' : ''} />{health ? `${health.storage} · ok` : 'conectando'}</div></div></nav>
      <main id="conteudo-principal" tabIndex={-1} className="content">
        {error && <div className="error-banner">{error}<button aria-label="Fechar aviso" onClick={() => setError('')}>×</button></div>}
        {nav === 'Chat' && <>
          <header className="stage-header"><div><h1>Agentes em ação.</h1></div>
            <div className="turn-actions">
              <details className="session-details"><summary>Detalhes da sessão</summary><div>
                <code>{conversationId || 'Nova sessão'}</code>
                <span>{agentLabels[lastRun?.active_agent] || lastRun?.active_agent || 'Aguardando mensagem'}</span>
                {lastRun?.langfuse_trace_url && <a href={lastRun.langfuse_trace_url} target="_blank" rel="noreferrer">Abrir trace no Langfuse ↗</a>}
              </div></details>
              <button className="new-conversation-btn" onClick={newConversation} disabled={busy}>Nova conversa</button>
              <button className="new-conversation-btn" onClick={resetDemoMemory} disabled={busy} title="Desfaz o que a demo gravou neste cliente (fatos, episódios, curto prazo, cache do cliente)">Reiniciar memória da demo</button>
            </div>
          </header>
          <MongoCacheSavings run={lastRun} timeline={timeline} agentLabels={agentLabels} />
          {cast.length > 0 && (
            <div className="agent-cast" title={lastRun?.route_source === 'fanout' ? 'Agentes despachados em paralelo (fan-out), não em cadeia' : 'Agentes que participaram deste turno, em ordem de atuação'}>
              <span className="agent-cast-label">{lastRun?.route_source === 'fanout' ? 'despacho paralelo' : 'agentes em ação'}</span>
              {cast.map((agentKey, index) => {
                const isFanout = lastRun?.route_source === 'fanout';
                const call = index > 0 && !isFanout ? handoffs.find((h) => h.from_agent === cast[index - 1] && h.to_agent === agentKey) : null;
                return (
                  <span className="agent-cast-item" key={`${agentKey}-${index}`}>
                    {index > 0 && (isFanout
                      ? <span className="agent-cast-arrow" title="rodou ao mesmo tempo, sem depender um do outro">+ (paralelo)</span>
                      : <span className="agent-cast-arrow" title={call ? `handoff: ${call.reason}` : 'handoff'}>chamou →</span>)}
                    <span className={`agent-cast-pill ${(lastRun?.active_agent || '').split('+').includes(agentKey) ? 'current' : ''}`}>{agentLabels[agentKey] || agentKey}</span>
                  </span>
                );
              })}
            </div>
          )}
          {collectionsTouched.length > 0 && (
            <details className="collections-panel">
              <summary>Dados consultados · {collectionsTouched.length} coleções</summary>
              <div className="collections-panel-grid">
                {collectionsTouched.map(([collection, ops]) => (
                  <div className="collection-chip" key={collection}>
                    <code>{collection}</code>
                    <div className="collection-chip-ops">
                      {ops.map((item, index) => (
                        <span className={`op-badge op-${item.op}`} key={index} title={`${item.agent || 'orquestrador'} — ${item.title}`}>{OP_LABELS[item.op] || item.op}</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </details>
          )}
          <ReplacementChain timeline={timeline} />
          <div className="workspace workspace--focus"><ChatPanel key={customer?.customer_key} {...{ messages, input, setInput, send, busy, suggestions }} customerName={customer?.name} demos={demoScenarios} onSuggestion={(message) => send(message)} /><section className="timeline-panel"><div className="panel-label"><span>execução</span><code>{timeline.length} eventos</code></div><Timeline events={timeline} /></section></div>
        </>}
        {nav === 'Métricas' && <MetricsPage metrics={metrics} evalRuns={evalRuns} adminMode={adminMode} />}
        {nav === 'Decisões' && <CompliancePage adminMode={adminMode} setAdminMode={setAdminMode} customerKey={customer?.customer_key} />}
      </main>
    </div>
  );
}
