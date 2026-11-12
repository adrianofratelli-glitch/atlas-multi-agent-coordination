import { useEffect, useMemo, useRef, useState } from 'react';
import { hoodRows, summarizeOps } from '../mongoOps.js';
import EventDetail from './EventDetail.jsx';

const NATIVE = new Set(['$vectorSearch', '$rankFusion', '$rerank', '$graphLookup', 'BM25 + vetor', 'change stream', 'transação']);

function HoodRow({ row, labels, linked, onHover }) {
  const [open, setOpen] = useState(false);
  const agent = row.agent ? (labels[row.agent] || row.agent) : null;
  const classes = ['hood-row', `is-${row.kind}`, row.write ? 'is-write' : '', row.replayed ? 'is-replayed' : '',
    linked ? 'is-linked' : ''].filter(Boolean).join(' ');
  return (
    <li id={`hood-op-${row.index}`} tabIndex={-1} className={classes}
      onMouseEnter={() => onHover?.(row.index)} onMouseLeave={() => onHover?.(null)}
      onFocus={() => onHover?.(row.index)} onBlur={() => onHover?.(null)}>
      {row.kind === 'event' ? (
        <div className="hood-line">
          <span className={`hood-cat cat-${row.category || 'other'}`}>{row.categoryLabel}</span>
          <span className="hood-event-title">{row.title}</span>
          {agent && <span className="hood-agent">{agent}</span>}
        </div>
      ) : (
        <>
          <div className="hood-line">
            {row.external && <span className="hood-cat cat-web">fonte externa</span>}
            <code className="hood-collection">{row.collection}</code>
            <span className={`hood-op op-${row.op}`}>{row.opLabel}</span>
            {agent && <span className="hood-agent">{agent}</span>}
          </div>
          {row.filterText && <code className="hood-filter" title={row.filterText}>{row.filterText}</code>}
          {row.tags.length > 0 && (
            <div className="hood-tags">
              {row.tags.map((tag) => <span key={tag} className={`hood-tag ${NATIVE.has(tag) ? 'is-native' : ''}`}>{tag}</span>)}
            </div>
          )}
        </>
      )}
      <details className="hood-more" onToggle={(event) => setOpen(event.currentTarget.open)}>
        <summary>ver detalhe{row.replayed ? ' · histórico do cache' : ''}</summary>
        {open && <EventDetail event={row.event} label={row.external ? 'Ver chamada externa' : 'Ver query / chamada executada'} />}
      </details>
    </li>
  );
}

/**
 * "Por baixo do capô": cada evento do turno na ordem em que aconteceu. Operações no MongoDB vêm com a
 * coleção, a operação e o filtro real gravado pelo backend; a busca web aparece como fonte externa; os
 * eventos sem coleção (guardrail de saída, coordenação) entram como linhas mais leves. Cada linha abre
 * o detalhe (título, agente, duração, resultado mascarado, query). Os eventos do Change Stream
 * (agent_handoffs → navegador) entram no fim.
 */
export default function UnderTheHood({ events = [], rows: givenRows = null, live = false, streamFeed = [], labels = {}, linked = null, onHoverRow }) {
  const rows = useMemo(() => givenRows || hoodRows(events), [givenRows, events]);
  const summary = useMemo(() => summarizeOps(rows.filter((row) => row.kind !== 'event')), [rows]);
  const eventCount = rows.filter((row) => row.kind === 'event').length;
  const listRef = useRef(null);
  useEffect(() => {
    if (live && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [rows.length, live]);
  const linkedSet = linked instanceof Set ? linked : new Set(linked || []);

  return (
    <section className="hood" aria-label="Operações no MongoDB e eventos deste turno">
      <header className="hood-head">
        <h3>Por baixo do capô</h3>
        <p>{rows.length
          ? `${summary.total} operações em ${summary.collections} coleções · ${summary.writes} escritas · ${summary.native} com recurso nativo do Atlas${eventCount ? ` · ${eventCount} evento${eventCount === 1 ? '' : 's'} do turno` : ''}`
          : 'As operações do próximo turno aparecem aqui, com o filtro real.'}</p>
      </header>
      <ol className={`hood-list ${linkedSet.size ? 'has-link' : ''}`} ref={listRef}>
        {rows.map((row) => (
          <HoodRow key={row.index} row={row} labels={labels} linked={linkedSet.has(row.index)} onHover={onHoverRow} />
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
