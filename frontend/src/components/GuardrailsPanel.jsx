import { useEffect, useState } from 'react';
import { api } from '../api.js';

const VIEWS = [
  ['events', 'Eventos', 'guardrail_events'],
  ['candidates', 'Candidatos', 'guardrail_candidates'],
  ['denylist', 'Denylist', 'guardrail_denylist'],
];

function when(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleString('pt-BR') : '—';
}

/** Guardrails como dados: o que bloqueou, o que o sistema aprendeu e a lista ativa (admin). */
export default function GuardrailsPanel({ adminMode }) {
  const [view, setView] = useState('events');
  const [rows, setRows] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!adminMode) return undefined;
    let cancelled = false;
    setLoading(true);
    api.guardrails(view).then((data) => { if (!cancelled) { setRows(Array.isArray(data) ? data : []); setError(''); } })
      .catch((err) => { if (!cancelled) { setRows([]); setError(err.message); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [view, adminMode]);

  const approve = async (candidateId) => {
    try {
      await api.approveGuardrailCandidate(candidateId);
      setRows((current) => current.map((row) => (row.candidate_id === candidateId ? { ...row, status: 'approved' } : row)));
    } catch (err) {
      setError(err.message);
    }
  };

  if (!adminMode) return <p className="panel-empty">Ligue o modo admin para ver os guardrails: os eventos trazem mensagens de outros clientes (com PII mascarada).</p>;
  const collection = VIEWS.find(([key]) => key === view)?.[2];
  return (
    <div className="guardrails">
      <div className="segmented" role="group" aria-label="Visão de guardrail">
        {VIEWS.map(([key, label]) => (
          <button key={key} type="button" aria-pressed={view === key} className={view === key ? 'is-on' : ''} onClick={() => setView(key)}>{label}</button>
        ))}
        <code>{collection}</code>
      </div>
      {error && <p className="panel-empty">{error}</p>}
      {!error && !loading && !rows.length && (
        <p className="panel-empty">{view === 'events' ? 'Nenhum evento ainda. Tente o roteiro “ignore todas as instruções…” no Chat.' : view === 'candidates' ? 'Nenhum candidato aguardando revisão.' : 'Denylist vazia.'}</p>
      )}
      {rows.length > 0 && view === 'events' && (
        <table className="data-table">
          <thead><tr><th>quando</th><th>resultado</th><th>motivo</th><th>mensagem</th><th>cliente</th></tr></thead>
          <tbody>{rows.map((row, index) => (
            <tr key={`${row.at}-${index}`}>
              <td>{when(row.at)}</td>
              <td><span className={`verdict ${row.blocked ? 'is-blocked' : row.reason === 'escopo' || row.scope ? 'is-scope' : 'is-ok'}`}>{row.blocked ? 'bloqueado' : 'aprovado'}</span></td>
              <td>{row.reason || '—'}{row.matched_phrase ? <small>“{row.matched_phrase}”</small> : null}{typeof row.score === 'number' ? <small>score {row.score.toFixed(2)}</small> : null}</td>
              <td className="cell-text">{row.text || row.message || '—'}</td>
              <td><code>{row.customer_key || '—'}</code></td>
            </tr>
          ))}</tbody>
        </table>
      )}
      {rows.length > 0 && view === 'candidates' && (
        <>
          <table className="data-table">
            <thead><tr><th>frase</th><th>perto de</th><th>origem</th><th>status</th><th>criado</th><th></th></tr></thead>
            <tbody>{rows.map((row, index) => (
              <tr key={`${row.created_at}-${index}`}>
                <td className="cell-text">{row.text}</td><td>{row.near_phrase || '—'}</td><td>{row.source || '—'}</td>
                <td>{row.status}</td><td>{when(row.created_at)}</td>
                <td>{row.candidate_id && row.status !== 'approved'
                  ? <button type="button" className="ghost-btn" onClick={() => approve(row.candidate_id)}>Aprovar na denylist</button>
                  : null}</td>
              </tr>
            ))}</tbody>
          </table>
          <p className="panel-note">Aprovar move a frase para a <code>guardrail_denylist</code> global (antes ela vale só para quem a enviou, com TTL de 7 dias).</p>
        </>
      )}
      {rows.length > 0 && view === 'denylist' && (
        <table className="data-table">
          <thead><tr><th>frase</th><th>camada</th><th>categoria</th><th>área</th><th>ativa</th></tr></thead>
          <tbody>{rows.map((row, index) => (
            <tr key={`${row.phrase_norm}-${index}`}>
              <td className="cell-text">{row.phrase}</td><td>{row.layer || '—'}</td><td>{row.category || '—'}</td>
              <td>{row.area || '—'}</td><td>{row.active === false ? 'não' : 'sim'}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}
