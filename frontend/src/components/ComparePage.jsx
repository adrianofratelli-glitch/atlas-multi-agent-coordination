import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import PageHead from './PageHead.jsx';
import RichText from './RichText.jsx';
import { formatUsd, formatTokens } from '../costs.js';

export const FLAGSHIP_MESSAGE = 'meu Fone Pulse X do pedido PED-1001 chegou com defeito; quero um fone parecido mais barato e já quero trocar. Depois veja a entrega e, no fim, confirme que a troca ficou registrada.';
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

/** A/B: a mesma pergunta para um generalista com todas as tools e para o time (POST /api/compare). */
export default function ComparePage({ labels = {}, customerKey, defaultMessage = FLAGSHIP_MESSAGE }) {
  const [message, setMessage] = useState(defaultMessage);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const timer = useRef(null);
  useEffect(() => () => clearInterval(timer.current), []);

  const run = async () => {
    if (running || message.trim().length < 3) return;
    setRunning(true); setError(''); setResult(null); setElapsed(0);
    const started = Date.now();
    timer.current = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000);
    try { setResult(await api.compare(message.trim())); }
    catch (err) { setError(err.message === 'signal is aborted without reason' ? 'O A/B passou de 4 minutos e foi interrompido. Tente de novo com uma pergunta mais curta.' : err.message); }
    finally { clearInterval(timer.current); setRunning(false); }
  };

  const stage = elapsed < 4 ? 'Carregando registry, regras e políticas do Atlas' : elapsed < 40 ? 'Dois braços em paralelo: generalista e time de agentes' : 'Juiz avaliando completude, coerência e grounding';
  return (
    <section className="full-page-section">
      <PageHead source="POST /api/compare" title="Um agente ou um time?">
        A mesma pergunta, as mesmas tools, os mesmos portões e o mesmo modelo. Muda só a arquitetura. Escritas são simuladas nos dois braços: comparar nunca altera pedido, pontos ou entrega.
      </PageHead>
      <form className="compare-form" onSubmit={(event) => { event.preventDefault(); run(); }}>
        <label htmlFor="compare-message">Pergunta do cliente ({customerKey || 'identidade atual'})</label>
        <textarea id="compare-message" rows="3" value={message} onChange={(event) => setMessage(event.target.value)} />
        <div className="compare-actions">
          <button type="submit" className="primary-btn" disabled={running || message.trim().length < 3}>{running ? 'Comparando…' : 'Rodar A/B'}</button>
          {message !== defaultMessage && <button type="button" className="ghost-btn" onClick={() => setMessage(defaultMessage)} disabled={running}>Voltar à pergunta principal</button>}
          {running && <span className="compare-progress" role="status"><span className="pending-dot" />{stage} · {elapsed} s</span>}
        </div>
      </form>
      {error && <div className="error-banner">{error}<button aria-label="Fechar aviso" onClick={() => setError('')}>×</button></div>}
      {result && (
        <>
          <div className="compare-grid">
            <ArmCard arm={result.single} kind="single" labels={labels} other={result.multi} />
            <ArmCard arm={result.multi} kind="multi" labels={labels} other={result.single} />
          </div>
          <p className="panel-note">{result.note} O juiz é um modelo, não rótulo humano: serve para comparar variantes, não como verdade absoluta.</p>
        </>
      )}
      {!result && !running && !error && <p className="panel-empty">Com modelo real o A/B leva de 30 a 90 segundos: os dois braços rodam em paralelo e depois o juiz avalia cada resposta.</p>}
    </section>
  );
}
