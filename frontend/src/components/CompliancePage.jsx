import { useEffect, useState } from 'react';
import { api } from '../api.js';

const DECISIONS = [
  { value: 'quality_analysis', label: 'Análise de qualidade' },
  { value: 'approve_replacement', label: 'Aprovar troca' },
  { value: 'refund', label: 'Reembolsar' },
  { value: 'reject', label: 'Recusar' },
];
const DECISION_LABELS = Object.fromEntries(DECISIONS.map((item) => [item.value, item.label]));
const SEVERITY_LABELS = { info: 'info', warning: 'atenção', critical: 'crítico' };

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('pt-BR');
}

/** Fila do analista: os casos em que o agente PAROU e devolveu a decisão para um humano. */
function ReviewQueue({ adminMode, onResolved }) {
  const [reviews, setReviews] = useState([]);
  const [override, setOverride] = useState({});
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);

  const load = async () => {
    if (!adminMode) return;
    try {
      const data = await api.adminReviews('pending');
      setReviews(data.reviews || []);
      setOverride(data.override || {});
      setError('');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoaded(true);
    }
  };
  useEffect(() => { load(); }, [adminMode]);

  const resolve = async (review, decision) => {
    setBusy(review.review_id);
    try {
      await api.resolveReview(review.review_id, {
        decision,
        resolved_by: 'analista.qualidade',
        note: `Resolvido pelo painel: ${DECISION_LABELS[decision]}.`,
      });
      await load();
      onResolved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  if (!adminMode) {
    return <p className="metric-empty">Ligue o modo admin acima para abrir a fila do analista.</p>;
  }
  if (error) return <p className="metric-empty">{error}</p>;
  if (loaded && !reviews.length) {
    return (
      <p className="metric-empty">
        Nenhum caso aguardando decisão humana. Pergunte pela garantia do pedido <code>PED-3001</code> como
        a identidade <b>carla</b> — a cadeia de trocas dispara o gate.
      </p>
    );
  }

  return (
    <>
      {typeof override.resolved === 'number' && override.resolved > 0 && (
        <div className="override-strip">
          <div><strong>{override.resolved}</strong><span>casos resolvidos</span></div>
          <div><strong>{override.overrides}</strong><span>vezes que o humano discordou</span></div>
          <div><strong>{Math.round((override.override_rate || 0) * 100)}%</strong><span>taxa de override do agente</span></div>
        </div>
      )}
      {reviews.map((review) => (
        <article className="review-card" key={review.review_id}>
          <div className="review-head">
            <code>{review.review_id}</code>
            <span className="review-status pending">aguardando decisão humana</span>
            <small>{formatDate(review.created_at)}</small>
          </div>
          <div className="review-body">
            <p className="review-reason">“{review.reasoning}”</p>
            <div className="review-meta">
              <span><b>pedido</b> <code>{review.subject_id}</code></span>
              <span><b>cliente</b> <code>{review.customer_key}</code></span>
              <span><b>escalado por</b> <code>{review.agent}</code></span>
            </div>
            {review.evidence?.path?.length > 0 && (
              <div className="review-evidence">
                <span>evidência ($graphLookup)</span>
                <code>{review.evidence.path.join(' → ')}</code>
              </div>
            )}
            {review.risk_factors?.length > 0 && (
              <div className="review-risks">
                {review.risk_factors.map((factor) => <span className="risk-tag" key={factor}>{factor}</span>)}
              </div>
            )}
          </div>
          <div className="review-actions">
            <span className="review-recommendation">
              agente recomendou: <b>{DECISION_LABELS[review.recommended_action] || review.recommended_action}</b>
            </span>
            <div className="review-buttons">
              {DECISIONS.map((option) => (
                <button
                  type="button"
                  key={option.value}
                  className={`review-btn ${option.value === review.recommended_action ? 'recommended' : ''}`}
                  disabled={busy === review.review_id}
                  onClick={() => resolve(review, option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
        </article>
      ))}
    </>
  );
}

/** Trilha imutável: o que foi decidido, por quem, e o que o agente havia recomendado. */
function DecisionTrail({ trail }) {
  const decisions = trail?.decisions || [];
  const events = trail?.audit_events || [];
  if (!decisions.length && !events.length) {
    return (
      <p className="metric-empty">
        Nenhuma decisão registrada para esta identidade. Toda ação com efeito no mundo — trocar o status
        de um pedido, resgatar pontos, abrir chamado — grava um documento aqui.
      </p>
    );
  }
  return (
    <>
      <div className="trail-list">
        {decisions.map((decision) => {
          const overrode = decision.decided_by === 'human'
            && decision.recommended_action
            && decision.recommended_action !== decision.action;
          return (
            <article className={`trail-item by-${decision.decided_by}`} key={decision.decision_id}>
              <div className="trail-head">
                <span className={`decided-by ${decision.decided_by}`}>
                  {decision.decided_by === 'human' ? '👤 humano' : '🤖 agente'}
                </span>
                <code>{decision.decision_id}</code>
                <small>{formatDate(decision.at)}</small>
              </div>
              <h4>{DECISION_LABELS[decision.action] || decision.action}</h4>
              <p className="trail-reason">“{decision.reasoning}”</p>
              {overrode && (
                <p className="trail-override">
                  <span aria-hidden="true">↺</span>
                  humano decidiu <b>{DECISION_LABELS[decision.action] || decision.action}</b>; o agente havia
                  recomendado <b>{DECISION_LABELS[decision.recommended_action] || decision.recommended_action}</b>
                </p>
              )}
              <div className="trail-meta">
                <span><b>assunto</b> <code>{decision.subject_id}</code></span>
                <span><b>agente</b> <code>{decision.agent}</code></span>
                {decision.escalated && <span className="trail-tag">escalado</span>}
                {decision.supersedes && <span className="trail-tag">supersede {decision.supersedes}</span>}
              </div>
            </article>
          );
        })}
      </div>
      <div className="audit-list">
        <div className="panel-label"><span>trilha append-only</span><code>agent_audit_events</code></div>
        {events.map((event) => (
          <div className={`audit-row sev-${event.severity}`} key={event.event_id}>
            <span className="audit-sev">{SEVERITY_LABELS[event.severity] || event.severity}</span>
            <code>{event.event_type}</code>
            <small>{formatDate(event.at)}</small>
          </div>
        ))}
      </div>
    </>
  );
}

export default function CompliancePage({ adminMode, setAdminMode, customerKey }) {
  const [trail, setTrail] = useState(null);
  const [error, setError] = useState('');

  const loadTrail = async () => {
    try {
      setTrail(await api.decisions());
      setError('');
    } catch (err) {
      setError(err.message);
    }
  };
  useEffect(() => { loadTrail(); }, [customerKey]);

  return (
    <section className="full-page-section">
      <div className="section-copy">
        <span className="eyebrow">conformidade · registro imutável</span>
        <h2>O agente para quando não deve decidir sozinho.</h2>
        <p>
          Observabilidade expira em 30 dias; decisão e auditoria ficam. Quando o caso exige um humano,
          a pausa mora em um documento — não em uma conexão HTTP aberta — e a resolução devolve o caso
          ao agente por um handoff real, que acorda a tela do cliente pelo Change Stream.
        </p>
      </div>

      <div className="compliance-block">
        <div className="panel-label">
          <span>fila do analista</span>
          <label className="admin-toggle">
            <input type="checkbox" checked={adminMode} onChange={(event) => setAdminMode(event.target.checked)} />
            <span className="admin-toggle-track"><span className="admin-toggle-thumb" /></span>
            modo admin {adminMode ? '(ligado)' : '(desligado)'}
          </label>
          <code>pending_reviews</code>
        </div>
        <ReviewQueue adminMode={adminMode} onResolved={loadTrail} />
      </div>

      <div className="compliance-block">
        <div className="panel-label">
          <span>trilha de decisões · {customerKey || 'identidade atual'}</span>
          <code>agent_decisions</code>
        </div>
        {error ? <p className="metric-empty">{error}</p> : <DecisionTrail trail={trail} />}
      </div>
    </section>
  );
}
