import { useEffect, useMemo, useRef } from 'react';
import { mongoOps, summarizeOps } from '../mongoOps.js';
import QueryDetails from './QueryDetails.jsx';

const NATIVE = new Set(['$vectorSearch', '$rankFusion', '$rerank', '$graphLookup', 'BM25 + vetor', 'change stream', 'transação']);

/**
 * "Por baixo do capô": cada operação do turno no MongoDB, na ordem em que aconteceu, com o filtro
 * real gravado pelo backend. Os eventos do Change Stream (agent_handoffs → navegador) entram no fim.
 */
export default function UnderTheHood({ events = [], live = false, streamFeed = [], labels = {} }) {
  const rows = useMemo(() => mongoOps(events), [events]);
  const summary = useMemo(() => summarizeOps(rows), [rows]);
  const listRef = useRef(null);
  useEffect(() => {
    if (live && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [rows.length, live]);

  return (
    <section className="hood" aria-label="Operações no MongoDB neste turno">
      <header className="hood-head">
        <h3>Por baixo do capô</h3>
        <p>{rows.length ? `${summary.total} operações em ${summary.collections} coleções · ${summary.writes} escritas · ${summary.native} com recurso nativo do Atlas` : 'As operações do próximo turno aparecem aqui, com o filtro real.'}</p>
      </header>
      <ol className="hood-list" ref={listRef}>
        {rows.map((row) => (
          <li key={row.index} id={`hood-op-${row.index}`} tabIndex={-1} className={`hood-row ${row.write ? 'is-write' : ''} ${row.external ? 'is-external' : ''} ${row.replayed ? 'is-replayed' : ''}`}>
            <div className="hood-line">
              <code className="hood-collection">{row.collection}</code>
              <span className={`hood-op op-${row.op}`}>{row.opLabel}</span>
              {row.agent && <span className="hood-agent">{labels[row.agent] || row.agent}</span>}
            </div>
            {row.filterText && <code className="hood-filter" title={row.filterText}>{row.filterText}</code>}
            {row.tags.length > 0 && (
              <div className="hood-tags">
                {row.tags.map((tag) => <span key={tag} className={`hood-tag ${NATIVE.has(tag) ? 'is-native' : ''}`}>{tag}</span>)}
              </div>
            )}
            <QueryDetails operation={row.op} namespace={row.collection} query={row.filter ? { filter: row.filter } : undefined}
              label={row.external ? 'Ver chamada externa' : 'Ver query / chamada executada'} />
          </li>
        ))}
        {live && <li className="hood-pending"><span className="pending-dot" /> aguardando a próxima operação…</li>}
      </ol>
      {streamFeed.length > 0 && (
        <div className="hood-stream" aria-live="polite">
          <h4>Change stream em <code>agent_handoffs</code></h4>
          <ul>
            {streamFeed.slice(-6).map((item, index) => (
              <li key={`${item.at}-${index}`}>
                <span>{labels[item.from_agent] || (item.from_agent === 'human_reviewer' ? 'analista' : item.from_agent)} → {labels[item.to_agent] || item.to_agent}</span>
                <small>{item.at ? new Date(item.at).toLocaleTimeString('pt-BR') : ''}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
