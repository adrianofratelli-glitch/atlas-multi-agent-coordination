import { useEffect, useRef } from 'react';

/**
 * Resultado de "Simular queda e retomar do checkpoint": o turno cai antes do 2º hop e é retomado
 * do `langgraph_checkpoints` no Atlas. A prova é o hop concluído não rodar de novo.
 */
export default function CrashResumeDialog({ state, onClose, labels = {} }) {
  const ref = useRef(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (state && !dialog.open) dialog.showModal?.();
    if (!state && dialog.open) dialog.close();
  }, [state]);
  const data = state?.data;
  const before = data?.checkpoint_before_resume;
  const path = data?.result?.agent_path || [];
  const handoffCount = state?.handoffs ? state.handoffs.length : data?.handoffs_written;
  const duplicates = state?.duplicates || [];
  return (
    <dialog ref={ref} className="crash-dialog" onClose={onClose} aria-labelledby="crash-title">
      <header>
        <h3 id="crash-title">Queda simulada e retomada do checkpoint</h3>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Fechar">×</button>
      </header>
      {state?.loading && <p className="crash-progress"><span className="pending-dot" /> Derrubando o turno antes do 2º hop e retomando do checkpoint no Atlas… (até 1 min com modelo real)</p>}
      {state?.error && <p className="crash-error">{state.error}</p>}
      {data && !data.crashed && <p>O turno terminou antes do 2º hop ({data.note || 'cadeia de um agente'}): não houve queda para retomar. Use uma mensagem que envolva dois ou mais agentes.</p>}
      {data?.crashed && (
        <>
          <ol className="crash-steps">
            <li><b>Queda</b><span>{data.crash}</span></li>
            <li><b>Checkpoint antes de retomar <code>langgraph_checkpoints</code></b>
              <dl>
                <div><dt>checkpoint_id</dt><dd><code>{before?.checkpoint_id || '—'}</code></dd></div>
                <div><dt>próximo nó</dt><dd><code>{(before?.next_nodes || []).join(', ') || '—'}</code></dd></div>
                <div><dt>hops já concluídos</dt><dd>{before?.hops_done ?? '—'}</dd></div>
              </dl>
            </li>
            <li><b>Retomada</b><span>{path.length ? path.map((key) => labels[key] || key).join(' → ') : 'turno concluído'}</span></li>
            <li><b>Sem retrabalho</b>
              <span>{handoffCount ?? 0} handoff(s) gravados em <code>agent_handoffs</code>, {duplicates.length ? <strong className="crash-dup">{duplicates.length} duplicado(s)</strong> : 'nenhum duplicado'}: o hop concluído antes da queda não rodou de novo.</span>
            </li>
          </ol>
          <p className="crash-foot">A resposta retomada foi adicionada à conversa e o grafo mostra o caminho completo.</p>
        </>
      )}
    </dialog>
  );
}
