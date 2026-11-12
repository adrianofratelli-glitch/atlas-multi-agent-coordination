import RichText from './RichText.jsx';
import ReplacementChain from './ReplacementChain.jsx';

const STATUS = {
  open: 'em andamento', done: 'concluído', completed: 'concluído', consolidated: 'consolidado',
  awaiting_approval: 'aguardando aprovação humana', resumed: 'retomado', failed: 'falhou',
};
const MODE = { tool_calling: 'tool calling', deterministic: 'determinístico', degraded: 'degradado' };
const EFFECT = { read: 'leitura', write: 'escrita', external: 'externa' };

function argsText(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args).map(([key, value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value) : value}`).join(' · ');
}

/** O quadro do turno (`turn_blackboard`): o plano do orquestrador e o que cada hop registrou. */
export default function BlackboardView({ board, loading, error, labels = {}, timeline = [], conversationId }) {
  const name = (key) => labels[key] || key;
  return (
    <div className="board">
      {!conversationId && <p className="panel-empty">O quadro nasce com o primeiro turno da conversa: um documento por turno em <code>turn_blackboard</code>.</p>}
      {conversationId && loading && !board && <p className="panel-empty">Lendo <code>turn_blackboard</code>…</p>}
      {error && <p className="panel-empty">Não foi possível ler o quadro: {error}</p>}
      {conversationId && !loading && !board && !error && <p className="panel-empty">Este turno não abriu quadro (cache, guardrail ou despacho paralelo respondem sem cadeia de agentes).</p>}
      {board && (
        <>
          <div className="board-head">
            <code>{board.turn_id}</code>
            <span className={`board-status status-${board.status}`}>{STATUS[board.status] || board.status}</span>
            {board.review_id && <span className="board-review">revisão <code>{board.review_id}</code></span>}
          </div>
          {board.detected_specialties && Object.keys(board.detected_specialties).length > 0 && (
            <div className="board-detected">
              <span>especialidades detectadas</span>
              {Object.entries(board.detected_specialties).map(([agent, words]) => (
                <span className="board-chip" key={agent} title={(words || []).join(', ')}>{name(agent)}</span>
              ))}
            </div>
          )}
          {Array.isArray(board.plan) && board.plan.length > 0 && (
            <section className="board-plan">
              <h4>Plano do orquestrador</h4>
              <ol>{board.plan.map((step, index) => <li key={`${step.agent}-${index}`}><b>{name(step.agent)}</b><span>{step.task || 'sem tarefa descrita'}</span></li>)}</ol>
            </section>
          )}
          <section className="board-entries">
            <h4>Entradas ({(board.entries || []).length})</h4>
            <ol>
              {(board.entries || []).map((entry) => {
                const writes = (entry.actions || []).filter((action) => action.effect === 'write');
                return (
                  <li className="board-entry" key={`${entry.hop}-${entry.agent}`}>
                    <div className="board-entry-head">
                      <span className="board-hop">hop {entry.hop}</span>
                      <b>{name(entry.agent)}</b>
                      <span className={`board-mode mode-${entry.mode}`}>{MODE[entry.mode] || entry.mode}</span>
                      {writes.length > 0 && <span className="board-writes">{writes.length} escrita(s)</span>}
                      {entry.handoff_to && <span className="board-next">passou para {name(entry.handoff_to)}</span>}
                    </div>
                    {entry.task && <p className="board-task">{entry.task}</p>}
                    {(entry.actions || []).length > 0 ? (
                      <table className="board-actions">
                        <thead><tr><th>tool</th><th>efeito</th><th>argumentos / resultado</th></tr></thead>
                        <tbody>
                          {entry.actions.map((action, index) => (
                            <tr key={index} className={action.effect === 'write' ? 'is-write' : ''}>
                              <td><code>{action.tool}</code>{action.simulated ? ' (simulada)' : ''}</td>
                              <td>{EFFECT[action.effect] || action.effect || '—'}</td>
                              <td>{action.summary || argsText(action.args) || '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : <p className="board-noactions">Sem tool registrada (agente determinístico: a leitura aparece no capô).</p>}
                    {entry.answer && <details className="board-answer"><summary>O que este agente concluiu</summary><div><RichText text={entry.answer} /></div></details>}
                  </li>
                );
              })}
            </ol>
          </section>
          {board.final_response && <details className="board-answer"><summary>Resposta consolidada</summary><div><RichText text={board.final_response} /></div></details>}
        </>
      )}
      <ReplacementChain timeline={timeline} />
    </div>
  );
}
