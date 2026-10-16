import QueryDetails from './QueryDetails.jsx';
import { explainArticles } from '../kbExplain.js';

const CATEGORY_LABELS = {
  agent: 'agente', memory: 'memória', guardrail: 'guardrail', cache: 'cache', handoff: 'coordenação', fanout: 'paralelo',
  web: 'fonte externa',
};

// Chamada externa (busca web, ADR-005) é ator próprio, não collection: mostra provedor, consulta e fontes, e só
// diz "cache Atlas" quando o resultado saiu do `web_search_cache` sem chamar o provedor.
function ExternalCall({ event }) {
  const result = event.result || {};
  const sources = (result.sources || []).filter((url) => /^https?:\/\//.test(url));
  return (
    <div className="external-operation">
      <span>{(event.filter?.provider || 'web').toUpperCase()}{result.cache_hit ? ' · cache Atlas' : ''}</span>
      {event.filter?.query && <code>{event.filter.query}</code>}
      {sources.length > 0 && (
        <ul>{sources.map((url) => <li key={url}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a></li>)}</ul>
      )}
    </div>
  );
}

// "Por que este artigo": rank e peso de cada perna do $rankFusion (scoreDetails) e o $rerank.
function WhyArticles({ event }) {
  const ex = explainArticles(event);
  if (!ex) return null;
  return (
    <details className="why-articles">
      <summary>Por que estes artigos{ex.strategy ? ` · ${ex.strategy}` : ''}</summary>
      {ex.fallbacks.length > 0 && <p className="why-fallbacks">{ex.fallbacks.join(' · ')}</p>}
      <ol>
        {ex.rows.map((row) => (
          <li key={row.id}>
            <span className="why-title"><code>{row.id}</code> {row.title}</span>
            {row.available ? (
              <span className="why-legs">
                {row.legs.map((leg) => (
                  <span key={leg.name} className="why-leg">
                    {leg.name} <b>{leg.rank ? `#${leg.rank}` : 'fora'}</b>
                    <i style={{ transform: `scaleX(${leg.share})` }} />
                  </span>
                ))}
                {row.rerank !== null && <span className="why-leg">rerank <b>{row.rerank.toFixed(2)}</b></span>}
              </span>
            ) : <span className="why-legs">detalhe indisponível</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}

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
            {event.op === 'external' ? <ExternalCall event={event} />
              : event.collection && <div className="mongo-operation"><span>MongoDB</span><code>{event.collection}</code></div>}
            {event.reason && <p className="handoff-reason">“{event.reason}”</p>}
            <WhyArticles event={event} />
            <div className="event-data">
              {event.result !== undefined && <details open={event.category === 'handoff'}><summary>Resultado</summary><JsonBlock value={event.result} /></details>}
            </div>
            <QueryDetails
              operation={event.op}
              namespace={event.collection || (event.op === 'external' ? event.filter?.provider : undefined)}
              label={event.op === 'external' ? 'Ver chamada externa' : undefined}
              query={event.query || (event.filter ? { filter: event.filter } : undefined)}
              explain={event.explain}
            />
          </div>
        </article>
      ))}
    </div>
  );
}
