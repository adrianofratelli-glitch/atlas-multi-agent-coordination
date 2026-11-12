import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import PageHead from './PageHead.jsx';
import RichText from './RichText.jsx';
import { formatUsd, formatTokens } from '../costs.js';
import { buildCompareScenarios, compareWaitMs, summarizeCompare, isRateLimited, FREE_SCENARIO } from '../compareScenarios.js';
const SCORES = [['completude', 'Completude'], ['coerencia', 'Coerência'], ['grounding', 'Grounding']];
const ARMS = { single: 'Um agente generalista', multi: 'Time multiagente' };

function Score({ label, value }) {
  const share = value ? value / 5 : 0;
  return (
    <div className="score">
      <span>{label}</span>
      <span className="score-track" role="img" aria-label={`${label}: ${value ?? '—'} de 5`}><i style={{ transform: `scaleX(${share})` }} /></span>
      <b>{value ?? '—'}<small>/5</small></b>
    </div>
  );
}

function ArmCard({ arm, kind, labels, other }) {
  if (!arm) return null;
  if (arm.available === false) {
    return (
      <article className="arm-card is-unavailable">
        <h3>{ARMS[kind]}</h3>
        <p className="panel-empty">Este braço não rodou: tool calling exige o modelo configurado no gateway (em modo demonstração sem LLM não há A/B).</p>
      </article>
    );
  }
  const judge = arm.judge || {};
  const better = (key, lowerIsBetter = true) => (other && typeof arm[key] === 'number' && typeof other[key] === 'number'
    && (lowerIsBetter ? arm[key] < other[key] : arm[key] > other[key]));
  return (
    <article className={`arm-card arm-${kind}`}>
      <header>
        <h3>{ARMS[kind]}</h3>
        <p>{(arm.agents || []).map((key) => labels[key] || key).join(' → ')}</p>
      </header>
      <dl className="arm-stats">
        <div className={better('latency_ms') ? 'is-better' : ''}><dt>latência</dt><dd>{arm.latency_ms != null ? `${(arm.latency_ms / 1000).toFixed(1)} s` : '—'}</dd></div>
        <div className={better('tokens') ? 'is-better' : ''}><dt>tokens</dt><dd>{formatTokens(arm.tokens)}</dd></div>
        <div className={better('cost_usd') ? 'is-better' : ''}><dt>custo</dt><dd>{formatUsd(arm.cost_usd, 4)}</dd></div>
        <div><dt>chamadas</dt><dd>{arm.llm_calls ?? '—'}</dd></div>
      </dl>
      <section className="arm-judge">
        <h4>Juiz {judge.available ? <span>média {judge.media}</span> : <span>indisponível</span>}</h4>
        {judge.available ? SCORES.map(([key, label]) => <Score key={key} label={label} value={judge[key]} />) : <p className="panel-empty">O juiz precisa do modelo; as métricas de custo e latência continuam reais.</p>}
        {judge.problemas?.length > 0 && <ul className="arm-problems">{judge.problemas.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      </section>
      <section className="arm-tools">
        <h4>Tools chamadas ({(arm.tools_called || []).length})</h4>
        <p>{(arm.tools_called || []).map((tool, index) => <code key={`${tool}-${index}`}>{tool}</code>)}{!(arm.tools_called || []).length && 'nenhuma'}</p>
      </section>
      <details className="arm-response" open>
        <summary>Resposta ao cliente</summary>
        <div><RichText text={arm.response} /></div>
      </details>
    </article>
  );
}

// Start time of each A/B call, per customer, for the whole browser session: the backend allows 3
// per minute per customer, and leaving/returning to the page must not reset that memory.
const callHistory = {};
const RETRY_AFTER_MS = 20000;
const sleep = (ms, signal) => new Promise((resolve) => {
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
});

const seconds = (ms) => (typeof ms === 'number' ? `${(ms / 1000).toFixed(1)} s` : '—');
const judgeText = (value) => (typeof value === 'number' ? value.toFixed(1).replace('.', ',') : '—');

function HowToRead() {
  return (
    <aside className="compare-howto" aria-label="Como ler o resultado">
      <h3>Como ler o resultado</h3>
      <ul>
        <li>Em pedidos simples o agente único costuma ganhar em latência e custo: há menos chamadas ao modelo.</li>
        <li>Em pedidos compostos o time costuma ganhar em completude e coerência: cada parte tem um especialista e o orquestrador consolida.</li>
        <li>A nota é de um juiz LLM, não de um rótulo humano: serve para comparar as duas arquiteturas, não como verdade absoluta.</li>
      </ul>
    </aside>
  );
}

function SummaryTable({ scenarios, results, selected, onSelect }) {
  const rows = scenarios.filter((item) => results[item.id]);
  if (!rows.length) return null;
  const mark = (winner, kind) => (winner === kind ? 'is-better' : '');
  return (
    <section className="compare-summary">
      <h3>Resumo dos cenários</h3>
      <div className="compare-summary-scroll">
        <table className="data-table">
          <thead>
            <tr><th rowSpan="2">cenário</th><th colSpan="3">Agente único</th><th colSpan="3">Time multiagente</th></tr>
            <tr><th>latência</th><th>custo</th><th>juiz</th><th>latência</th><th>custo</th><th>juiz</th></tr>
          </thead>
          <tbody>
            {rows.map((item) => {
              const entry = results[item.id];
              const sum = entry.result ? summarizeCompare(entry.result) : null;
              return (
                <tr key={item.id} className={selected === item.id ? 'is-selected' : ''}>
                  <td><button type="button" className="link-btn" onClick={() => onSelect(item.id)}>{item.title}</button></td>
                  {entry.error ? <td colSpan="6" className="compare-cell-error">{entry.error}</td> : (
                    <>
                      <td className={mark(sum.faster, 'single')}>{seconds(sum.single?.latencyMs)}</td>
                      <td className={mark(sum.cheaper, 'single')}>{formatUsd(sum.single?.cost, 4)}</td>
                      <td className={mark(sum.betterJudge, 'single')}>{judgeText(sum.single?.judge)}</td>
                      <td className={mark(sum.faster, 'multi')}>{seconds(sum.multi?.latencyMs)}</td>
                      <td className={mark(sum.cheaper, 'multi')}>{formatUsd(sum.multi?.cost, 4)}</td>
                      <td className={mark(sum.betterJudge, 'multi')}>{judgeText(sum.multi?.judge)}</td>
                    </>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="panel-note">Verde marca o braço melhor em cada métrica. Clique no cenário para ver as respostas.</p>
    </section>
  );
}

/** A/B: a mesma pergunta para um generalista com todas as tools e para o time (POST /api/compare). */
export default function ComparePage({ labels = {}, customerKey, demoScenarios = [] }) {
  const scenarios = useMemo(() => buildCompareScenarios(customerKey, demoScenarios), [customerKey, demoScenarios]);
  const [selected, setSelected] = useState('composto');
  const [freeText, setFreeText] = useState('');
  const [results, setResults] = useState({});
  const [error, setError] = useState('');
  const [status, setStatus] = useState(null); // { phase: 'running'|'waiting', since|until, label, step, total }
  const [now, setNow] = useState(Date.now());
  const abortRef = useRef(null);
  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    if (!status) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [status]);

  const current = scenarios.find((item) => item.id === selected);
  const message = selected === FREE_SCENARIO ? freeText : (current?.message || '');
  const busy = Boolean(status);
  const history = () => { callHistory[customerKey] = callHistory[customerKey] || []; return callHistory[customerKey]; };

  // One A/B call, paced by the rate limit and retried (twice) if the backend still says 429.
  const runOne = async (id, text, signal, progress = {}) => {
    for (let attempt = 0; attempt < 3 && !signal.aborted; attempt += 1) {
      const wait = attempt === 0 ? compareWaitMs(history(), Date.now()) : RETRY_AFTER_MS;
      if (wait > 0) {
        setStatus({ ...progress, phase: 'waiting', until: Date.now() + wait });
        await sleep(wait, signal);
        if (signal.aborted) return;
      }
      setStatus({ ...progress, phase: 'running', since: Date.now() });
      setNow(Date.now());
      history().push(Date.now());
      try {
        const result = await api.compare(text);
        setResults((items) => ({ ...items, [id]: { result, message: text } }));
        return;
      } catch (err) {
        if (isRateLimited(err.message) && attempt < 2) continue;
        const text2 = err.message === 'signal is aborted without reason'
          ? 'O A/B passou de 4 minutos e foi interrompido. Tente de novo com uma pergunta mais curta.' : err.message;
        setResults((items) => ({ ...items, [id]: { error: text2, message: text } }));
        if (!progress.total) setError(text2);
        return;
      }
    }
  };

  const run = async () => {
    if (busy || message.trim().length < 3) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setError('');
    await runOne(selected, message.trim(), controller.signal, { label: selected === FREE_SCENARIO ? 'Pergunta livre' : current?.title });
    setStatus(null);
  };

  const runAll = async () => {
    if (busy) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setError('');
    for (let index = 0; index < scenarios.length && !controller.signal.aborted; index += 1) {
      const item = scenarios[index];
      setSelected(item.id);
      await runOne(item.id, item.message, controller.signal, { label: item.title, step: index + 1, total: scenarios.length });
    }
    setStatus(null);
  };
  const cancel = () => { abortRef.current?.abort(); setStatus(null); };

  const elapsed = status?.phase === 'running' ? Math.max(0, Math.round((now - status.since) / 1000)) : 0;
  const waitLeft = status?.phase === 'waiting' ? Math.max(0, Math.ceil((status.until - now) / 1000)) : 0;
  const stage = elapsed < 4 ? 'Carregando registry, regras e políticas do Atlas' : elapsed < 40 ? 'Dois braços em paralelo: generalista e time de agentes' : 'Juiz avaliando completude, coerência e grounding';
  const shown = results[selected];
  const result = shown?.result;
  const step = status?.total ? `Cenário ${status.step} de ${status.total} · ` : '';

  return (
    <section className="full-page-section">
      <PageHead source="POST /api/compare" title="Um agente ou um time?">
        A mesma pergunta, as mesmas tools, os mesmos portões e o mesmo modelo. Muda só a arquitetura. Escritas são simuladas nos dois braços: comparar nunca altera pedido, pontos ou entrega.
      </PageHead>
      <div className="compare-layout">
        <div className="compare-main">
          <fieldset className="compare-scenarios" disabled={busy}>
            <legend>Escolha um cenário ({customerKey || 'identidade atual'})</legend>
            {scenarios.map((item) => (
              <label key={item.id} className={`scenario-card ${selected === item.id ? 'is-on' : ''}`}>
                <input type="radio" name="compare-scenario" value={item.id} checked={selected === item.id} onChange={() => setSelected(item.id)} />
                <span className="scenario-title">{item.title}{results[item.id]?.result && <small className="scenario-done">rodado</small>}</span>
                <span className="scenario-tests">{item.tests}</span>
              </label>
            ))}
            <label className={`scenario-card ${selected === FREE_SCENARIO ? 'is-on' : ''}`}>
              <input type="radio" name="compare-scenario" value={FREE_SCENARIO} checked={selected === FREE_SCENARIO}
                onChange={() => { setFreeText((text) => text || message); setSelected(FREE_SCENARIO); }} />
              <span className="scenario-title">Pergunta livre</span>
              <span className="scenario-tests">Escreva a sua: os mesmos portões valem, inclusive o isolamento por cliente.</span>
            </label>
          </fieldset>
          <form className="compare-form" onSubmit={(event) => { event.preventDefault(); run(); }}>
            <label htmlFor="compare-message">Pergunta do cliente</label>
            <textarea id="compare-message" rows="3" value={message} disabled={busy}
              onChange={(event) => { setFreeText(event.target.value); setSelected(FREE_SCENARIO); }} />
            <div className="compare-actions">
              <button type="submit" className="primary-btn" disabled={busy || message.trim().length < 3}>{busy ? 'Comparando…' : 'Rodar A/B'}</button>
              <button type="button" className="ghost-btn" onClick={runAll} disabled={busy}
                title="Roda todos os cenários um depois do outro, respeitando o limite de 3 A/B por minuto">Rodar todos em sequência</button>
              {busy && <button type="button" className="ghost-btn" onClick={cancel}>{status?.total ? 'Parar depois deste' : 'Parar de esperar'}</button>}
              {status?.phase === 'running' && <span className="compare-progress" role="status"><span className="pending-dot" />{step}{status.label}: {stage} · {elapsed} s</span>}
              {status?.phase === 'waiting' && <span className="compare-progress" role="status"><span className="pending-dot" />{step}Limite de 3 A/B por minuto: próximo em {waitLeft} s</span>}
            </div>
            {!busy && <p className="panel-note">Com modelo real cada A/B leva de 30 a 90 segundos; todos em sequência levam alguns minutos.</p>}
          </form>
        </div>
        <HowToRead />
      </div>
      {error && <div className="error-banner">{error}<button aria-label="Fechar aviso" onClick={() => setError('')}>×</button></div>}
      <SummaryTable scenarios={scenarios} results={results} selected={selected} onSelect={setSelected} />
      {shown?.error && selected !== FREE_SCENARIO && <p className="panel-empty">Este cenário não rodou: {shown.error}</p>}
      {result && (
        <>
          <h3 className="compare-result-title">{selected === FREE_SCENARIO ? 'Pergunta livre' : current?.title}</h3>
          <div className="compare-grid">
            <ArmCard arm={result.single} kind="single" labels={labels} other={result.multi} />
            <ArmCard arm={result.multi} kind="multi" labels={labels} other={result.single} />
          </div>
          <p className="panel-note">{result.note} O juiz é um modelo, não rótulo humano: serve para comparar variantes, não como verdade absoluta.</p>
        </>
      )}
    </section>
  );
}
