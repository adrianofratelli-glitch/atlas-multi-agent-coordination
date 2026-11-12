import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';
import { isHumanResume, duplicateHandoffs } from './graphModel.js';
import ChatStage from './components/ChatStage.jsx';
import CompliancePage from './components/CompliancePage.jsx';
import AgentsPage from './components/AgentsPage.jsx';
import ComparePage from './components/ComparePage.jsx';
import { answerAuthor } from './turnNarrative.js';
import MetricsPage from './components/MetricsPage.jsx';
import CrashResumeDialog from './components/CrashResume.jsx';

// Cinco destinos (contrato de UI: no máximo cinco); Guardrails mora dentro de Agentes.
const NAV = ['Chat', 'A/B', 'Agentes', 'Decisões', 'Métricas'];
const IDENTITIES = ['ana', 'bruno', 'carla', 'diego'];
// Cadeia de dois agentes com retorno controlado: o turno cai antes do 2º hop e retoma do checkpoint.
const crashMessage = (customerKey) => {
  const n = Math.max(0, IDENTITIES.indexOf(customerKey)) + 1;
  return `quero trocar o pedido PED-${n}001; depois consulte a entrega e volte para confirmar que a troca ficou registrada.`;
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function assistantMessage(run, extra = {}) {
  const author = answerAuthor(run);
  return {
    role: 'assistant', agent: run.active_agent, text: run.response, cacheHit: run.cache_hit,
    consolidatedFrom: author.consolidated ? author.count : 0,
    tokens: run.usage?.total ?? 0, awaiting: run.awaiting_approval || null, ...extra,
  };
}

export default function App() {
  const [nav, setNav] = useState('Chat');
  const [health, setHealth] = useState(null);
  const [customer, setCustomer] = useState(null);
  const [agents, setAgents] = useState([]);
  const [topology, setTopology] = useState(null);
  const [demoScenarios, setDemoScenarios] = useState([]);
  const [metrics, setMetrics] = useState({});
  const [messages, setMessages] = useState([]);
  const [timeline, setTimeline] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const operationRef = useRef(false);
  const [error, setError] = useState('');
  const [conversationId, setConversationId] = useState(null);
  const conversationRef = useRef(null);
  const [lastRun, setLastRun] = useState(null);
  const [suggestions, setSuggestions] = useState([]);
  const [adminMode, setAdminMode] = useState(false);
  const [liveStatus, setLiveStatus] = useState('connecting');
  const [liveError, setLiveError] = useState('');
  const [evalRuns, setEvalRuns] = useState([]);
  const [board, setBoard] = useState({ board: null, loading: false, error: '' });
  const [typedMemory, setTypedMemory] = useState({ memory: null, error: '' });
  const [streamFeed, setStreamFeed] = useState([]);
  const [crash, setCrash] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const appliedResumes = useRef(new Set());
  // O change stream do DEMO_MODE (poll) reentrega handoffs antigos: o painel só mostra os desta sessão.
  const feedSinceRef = useRef(Date.now() - 5000);

  const messagesRef = useRef([]);
  useEffect(() => { conversationRef.current = conversationId; }, [conversationId]);
  useEffect(() => { messagesRef.current = messages; }, [messages]);

  const loadCore = async () => {
    const [h, a, topo] = await Promise.all([api.health(), api.agents(), api.topology().catch(() => null)]);
    setHealth(h); setAgents(a); setTopology(topo);
  };
  const loadBoard = async (convId, hasBoard = true) => {
    if (!convId || !hasBoard) { setBoard({ board: null, loading: false, error: '' }); return; }
    setBoard((current) => ({ ...current, loading: true, error: '' }));
    try { setBoard({ board: await api.blackboardLatest(convId), loading: false, error: '' }); }
    catch (err) { setBoard({ board: null, loading: false, error: err.message }); }
  };
  const loadTypedMemory = () => api.memoryTyped()
    .then((memory) => setTypedMemory({ memory, error: '' }))
    .catch((err) => setTypedMemory({ memory: null, error: err.message }));

  const resetTurnState = () => {
    setConversationId(null); setMessages([]); setTimeline([]); setSuggestions([]); setLastRun(null);
    setBoard({ board: null, loading: false, error: '' }); setStreamFeed([]);
    feedSinceRef.current = Date.now() - 5000;
  };

  // o boot abre sempre em conversa nova: uma demo que começa com o histórico do ensaio anterior
  // na tela parece que o cliente já falou com o agente. a retomada continua valendo ao trocar de
  // identidade no meio da sessão, que é quando ela realmente evita um painel vazio.
  const switchIdentity = async (customerKey, { resume = true } = {}) => {
    if (operationRef.current) return;
    operationRef.current = true; setBusy(true); setError('');
    // Never display the previous identity's conversation while its replacement loads.
    setCustomer(null); resetTurnState(); setTypedMemory({ memory: null, error: '' });
    try {
      const who = await api.login(customerKey); setCustomer(who); await loadCore();
      const [met, lastConv, scenarios] = await Promise.all([api.metrics(), api.latestConversation(), api.demoScenarios()]);
      setMetrics(met); setDemoScenarios(scenarios);
      loadTypedMemory();
      if (resume && lastConv?.turns?.length) {
        setConversationId(lastConv.conversation_id);
        // The last answer gets the same signature as a live turn (consolidated → orchestrator).
        const author = answerAuthor({ active_agent: lastConv.active_agent, timeline: lastConv.last_timeline || [] });
        const lastIndex = lastConv.turns.length - 1;
        setMessages(lastConv.turns.map((turn, index) => ({ role: turn.role, agent: lastConv.active_agent, text: turn.content,
          consolidatedFrom: index === lastIndex && turn.role === 'assistant' && author.consolidated ? author.count : 0,
          // pausa HITL ainda pendente: o cartão "Aguardando analista" volta junto com a conversa
          awaiting: index === lastIndex && turn.role === 'assistant' ? lastConv.awaiting_approval || null : null })));
        // sem isso a conversa retomada mostra o texto certo mas grafo/quadro vazios — parece que o
        // multi-agent não rodou, quando só faltava recarregar o registro do último turno.
        setTimeline(lastConv.last_timeline || []);
        setLastRun(lastConv.last_timeline ? { active_agent: lastConv.active_agent, usage: lastConv.last_usage || {}, llm_calls: lastConv.last_llm_calls || [], economics: lastConv.last_economics || {}, awaiting_approval: lastConv.awaiting_approval || null } : null);
        loadBoard(lastConv.conversation_id);
      }
    } catch (err) { setCustomer(null); setError(err.message); }
    finally { operationRef.current = false; setBusy(false); }
  };
  useEffect(() => { api.warmup(); switchIdentity('ana', { resume: false }); }, []);

  useEffect(() => {
    if (nav === 'Métricas' && adminMode) {
      api.evalRuns().then(setEvalRuns).catch(() => setEvalRuns([]));
    }
    if (nav === 'Métricas' && customer) api.metrics().then(setMetrics).catch(() => {});
    if (nav === 'Agentes' && customer) loadCore().catch(() => {});
  }, [nav, adminMode]);

  const afterTurn = async (run) => {
    setRefreshKey((key) => key + 1);
    await Promise.all([
      api.metrics().then(setMetrics).catch(() => {}),
      loadTypedMemory(),
      loadBoard(run.conversation_id, Boolean(run.turn_id)),
    ]);
  };

  // Turno retomado depois da decisão humana: chega pela resposta do resolve (mesmo navegador) ou
  // pelo Change Stream (outra aba/analista). O texto aplicado fica registrado para não duplicar.
  const applyResumedTurn = (run) => {
    if (!run?.response || run.conversation_id !== conversationRef.current) return;
    const key = `${run.conversation_id}|${run.turn_id || ''}|${run.response}`;
    if (appliedResumes.current.has(key)) return;
    appliedResumes.current.add(key);
    setMessages((items) => [...items.map((item) => ({ ...item, awaiting: null })), assistantMessage(run, { resumed: true })]);
    setTimeline(run.timeline || []); setLastRun(run); setSuggestions(run.suggestions || []);
    afterTurn(run);
  };
  const syncResumedFromStore = async () => {
    const convId = conversationRef.current;
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await sleep(attempt === 0 ? 600 : 1500);
      if (conversationRef.current !== convId) return;
      const latest = await api.latestConversation().catch(() => null);
      const last = latest?.turns?.[latest.turns.length - 1];
      if (latest?.conversation_id !== convId || last?.role !== 'assistant') continue;
      const key = `${convId}|resumed|${last.content}`;
      const shown = messagesRef.current.some((item) => item.role === 'assistant' && item.text === last.content);
      if (shown || appliedResumes.current.has(key)) return;
      appliedResumes.current.add(key);
      setMessages((items) => [...items.map((item) => ({ ...item, awaiting: null })), { role: 'assistant', agent: latest.active_agent, text: last.content, resumed: true }]);
      if (latest.last_timeline) {
        setTimeline(latest.last_timeline);
        setLastRun({ conversation_id: convId, active_agent: latest.active_agent, usage: latest.last_usage || {}, llm_calls: latest.last_llm_calls || [], economics: latest.last_economics || {} });
      }
      loadBoard(convId); loadTypedMemory(); setRefreshKey((k) => k + 1);
      return;
    }
  };
  const streamHandlerRef = useRef(() => {});
  streamHandlerRef.current = (event) => {
    if (!event || event._resync) return;
    const sameConversation = event.conversation_id && event.conversation_id === conversationRef.current;
    const at = Date.parse(event.at);
    if (!sameConversation && !(Number.isFinite(at) && at >= feedSinceRef.current)) return;
    setStreamFeed((items) => [...items.slice(-19), event]);
    if (isHumanResume(event) && event.conversation_id === conversationRef.current) syncResumedFromStore();
  };

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
          await api.streamEvents((event) => streamHandlerRef.current(event), activeController.signal, () => {
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
    resetTurnState(); setInput('');
  };

  const resetDemoMemory = async () => {
    if (operationRef.current || !customer) return;
    operationRef.current = true; setBusy(true); setError('');
    try {
      await api.demoReset();
      loadTypedMemory();
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
      setTimeline([]); setLastRun(null); setStreamFeed([]); feedSinceRef.current = Date.now() - 2000;
      const run = await api.chatStream(value, conversationId, (event) => setTimeline((items) => [...items, event]));
      setConversationId(run.conversation_id); conversationRef.current = run.conversation_id;
      setTimeline(run.timeline); setLastRun(run); setSuggestions(run.suggestions || []);
      setMessages((items) => [...items, assistantMessage(run)]);
      await afterTurn(run);
    } catch (err) { setError(err.message); }
    finally { operationRef.current = false; setBusy(false); }
  };

  const simulateCrash = async () => {
    if (operationRef.current || !customer) return;
    operationRef.current = true;
    const message = input.trim() || crashMessage(customer.customer_key);
    resetTurnState(); setInput(''); setBusy(true); setError('');
    setMessages([{ role: 'user', text: message }]);
    setCrash({ loading: true });
    try {
      const data = await api.crashResume(message, null);
      const run = data.result;
      let handoffs = null;
      if (run) {
        setConversationId(run.conversation_id); conversationRef.current = run.conversation_id;
        setTimeline(run.timeline || []); setLastRun(run); setSuggestions(run.suggestions || []);
        setMessages((items) => [...items, assistantMessage(run)]);
        handoffs = await api.handoffs(run.conversation_id).catch(() => null);
        afterTurn(run);
      }
      setCrash({ data, handoffs, duplicates: handoffs ? duplicateHandoffs(handoffs) : [] });
    } catch (err) { setCrash({ error: err.message }); }
    finally { operationRef.current = false; setBusy(false); }
  };

  const labels = useMemo(() => Object.fromEntries([...(topology?.agents || []), ...agents].map((agent) => [agent.agent_key, agent.label])), [agents, topology]);
  const graphAgents = topology?.agents?.length ? topology.agents : agents;

  const stageHeader = (
    <header className="stage-header">
      <h1>Agentes em ação</h1>
      <div className="turn-actions">
        <details className="session-details"><summary>Detalhes da sessão</summary><div>
          <code>{conversationId || 'Nova sessão'}</code>
          <span>{labels[lastRun?.active_agent] || lastRun?.active_agent || 'Aguardando mensagem'}</span>
          {lastRun?.turn_id && <span>turno <code>{lastRun.turn_id}</code></span>}
          {lastRun?.langfuse_trace_url && <a href={lastRun.langfuse_trace_url} target="_blank" rel="noreferrer">Abrir trace no Langfuse ↗</a>}
        </div></details>
        <button className="new-conversation-btn" onClick={newConversation} disabled={busy}>Nova conversa</button>
        <button className="new-conversation-btn" onClick={resetDemoMemory} disabled={busy} title="Desfaz o que a demo gravou neste cliente (fatos, episódios, curto prazo, cache do cliente)">Reiniciar memória da demo</button>
        <button className="new-conversation-btn crash-btn" onClick={simulateCrash} disabled={busy} title="Derruba o turno antes do 2º hop e retoma do checkpoint no Atlas (abre uma conversa nova)">Simular queda e retomar do checkpoint</button>
      </div>
    </header>
  );

  return (
    <div data-pov-shell>
      <a className="pov-skip-link" href="#conteudo-principal">Pular para o conteúdo</a>
      <nav className="top-nav"><div className="nav-inner"><button className="brand" onClick={() => setNav('Chat')} aria-label="Ir para o Chat"><span className="leaf">◆</span><span>MongoDB</span><b>Agent Control Plane</b></button><div className="nav-tabs">{NAV.map((item) => <button className={nav === item ? 'active' : ''} aria-current={nav === item ? 'page' : undefined} key={item} onClick={() => setNav(item)}>{item}</button>)}</div><label className="identity-select">identidade<select disabled={busy} value={customer?.customer_key || 'ana'} onChange={(event) => switchIdentity(event.target.value)}>{IDENTITIES.map((key) => <option key={key} value={key}>{key}</option>)}</select></label><div className="live-pill" title={liveError || 'Change Stream do MongoDB Atlas em agent_handoffs'}><span className={liveStatus === 'live' ? 'ok' : liveStatus === 'connecting' ? 'connecting' : ''} />{liveStatus === 'live' ? 'ao vivo' : liveStatus === 'connecting' ? 'conectando' : 'offline'}</div><div className="health-pill"><span className={health ? 'ok' : ''} />{health ? `${health.storage} · ok` : 'conectando'}</div></div></nav>
      <main id="conteudo-principal" tabIndex={-1} className={`content ${nav === 'Chat' ? 'content--chat' : ''}`}>
        {error && <div className="error-banner">{error}<button aria-label="Fechar aviso" onClick={() => setError('')}>×</button></div>}
        {nav === 'Chat' && (
          <ChatStage
            agents={graphAgents} labels={labels} timeline={timeline} busy={busy} lastRun={lastRun}
            conversationId={conversationId} board={board} typedMemory={typedMemory} streamFeed={streamFeed}
            header={stageHeader}
            chat={{ messages, input, setInput, send, busy, suggestions, customerName: customer?.name, demos: demoScenarios,
              onSuggestion: (message) => send(message), onOpenQueue: () => { setAdminMode(true); setNav('Decisões'); } }}
          />
        )}
        {nav === 'A/B' && <ComparePage key={customer?.customer_key || 'none'} labels={labels} customerKey={customer?.customer_key} demoScenarios={demoScenarios} />}
        {nav === 'Agentes' && <AgentsPage agents={agents} adminMode={adminMode} setAdminMode={setAdminMode} reload={loadCore} />}
        {nav === 'Métricas' && <MetricsPage metrics={metrics} evalRuns={evalRuns} adminMode={adminMode} labels={labels} refreshKey={refreshKey} />}
        {nav === 'Decisões' && <CompliancePage adminMode={adminMode} setAdminMode={setAdminMode} customerKey={customer?.customer_key} onResumed={applyResumedTurn} labels={labels} refreshKey={refreshKey} />}
      </main>
      <CrashResumeDialog state={crash} onClose={() => setCrash(null)} labels={labels} />
    </div>
  );
}
