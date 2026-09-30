import QueryDetails from './QueryDetails.jsx';

const CATEGORY_LABELS = {
  agent: 'agente', memory: 'memória', guardrail: 'guardrail', cache: 'cache', handoff: 'coordenação', fanout: 'paralelo',
};

function JsonBlock({ value }) {
  if (value === undefined || value === null) return null;
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

export default function Timeline({ events }) {
  if (!events?.length) {
    return (
      <div className="empty-state">
        <div className="orbit-mark"><span /><span /><span /></div>
        <h3>A coordenação aparece aqui</h3>
        <p>Envie uma mensagem para ver o roteamento e a bola passar entre os agentes.</p>
      </div>
    );
  }
  return (
    <div className="timeline" aria-label="Timeline do turno">
      {events.map((event, index) => (
        <article className={`timeline-event event-${event.category}`} key={`${event.title}-${index}`} style={{ '--delay': `${index * 65}ms` }}>
          <div className="event-rail"><span>{String(index + 1).padStart(2, '0')}</span></div>
          <div className="event-card">
            <div className="event-heading">
              <span className={`category-badge ${event.category}`}>{CATEGORY_LABELS[event.category]}</span>
              {event.replayed && <small>Histórico do cache · não executado neste turno</small>}
              {event.agent && <code>{event.agent}</code>}
              {!event.replayed && event.duration_ms > 0 && <small>{Math.round(event.duration_ms)} ms</small>}
            </div>
            <h4>{event.title}</h4>
            {event.collection && <div className="mongo-operation"><span>MongoDB</span><code>{event.collection}</code></div>}
            {event.reason && <p className="handoff-reason">“{event.reason}”</p>}
            <div className="event-data">
              {event.result !== undefined && <details open={event.category === 'handoff'}><summary>Resultado</summary><JsonBlock value={event.result} /></details>}
            </div>
            <QueryDetails
              operation={event.op}
              namespace={event.collection}
              query={event.query || (event.filter ? { filter: event.filter } : undefined)}
              explain={event.explain}
            />
          </div>
        </article>
      ))}
    </div>
  );
}
