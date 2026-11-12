import QueryDetails from './QueryDetails.jsx';
import { explainArticles } from '../kbExplain.js';
import { redactResult } from '../redact.js';

// Chamada externa (busca web, ADR-005) é ator próprio, não collection: mostra provedor, consulta e fontes, e só
// diz "cache Atlas" quando o resultado saiu do `web_search_cache` sem chamar o provedor.
function ExternalCall({ event }) {
  const result = event.result || {};
  const sources = (result.sources || []).filter((url) => /^https?:\/\//.test(url));
  return (
    <div className="external-operation">
      <span>Fonte externa · {(event.filter?.provider || 'web').toUpperCase()}{result.cache_hit ? ' · cache Atlas' : ''}</span>
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

/**
 * Detalhe de um evento do turno no capô ("ver detalhe"): título, agente, duração, resultado (mascarado
 * por redact.js) e a query/chamada executada. É o que a antiga aba Execução mostrava por evento.
 */
export default function EventDetail({ event, label }) {
  if (!event) return null;
  const external = event.op === 'external';
  return (
    <div className="hood-detail">
      <p className="hood-detail-title">{event.title || 'evento do turno'}</p>
      <dl className="hood-detail-meta">
        {event.agent && <div><dt>agente</dt><dd><code>{event.agent}</code></dd></div>}
        {!event.replayed && event.duration_ms > 0 && <div><dt>duração</dt><dd>{Math.round(event.duration_ms)} ms</dd></div>}
        {event.replayed && <div><dt>origem</dt><dd>histórico do cache, não executado neste turno</dd></div>}
      </dl>
      {external && <ExternalCall event={event} />}
      {event.reason && <p className="handoff-reason">“{event.reason}”</p>}
      <WhyArticles event={event} />
      {event.result !== undefined && event.result !== null && (
        <div className="hood-detail-result">
          <span>Resultado</span>
          <pre>{JSON.stringify(redactResult(event.result), null, 2)}</pre>
        </div>
      )}
      <QueryDetails
        operation={event.op}
        namespace={event.collection || (external ? event.filter?.provider : undefined)}
        label={label || (external ? 'Ver chamada externa' : undefined)}
        query={event.query || (event.filter ? { filter: event.filter } : undefined)}
        explain={event.explain}
      />
    </div>
  );
}
