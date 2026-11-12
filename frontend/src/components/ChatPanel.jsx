import { useEffect, useRef } from 'react';
import RichText from './RichText.jsx';
import { LockIcon } from './Icons.jsx';
import { authorLabel } from '../turnNarrative.js';

function argsSummary(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args).map(([key, value]) => `${key} = ${typeof value === 'object' ? JSON.stringify(value) : value}`).join(', ');
}

/** Escrita sensível pausada: a pausa mora no checkpoint do Atlas, não numa conexão aberta. */
export function ApprovalCard({ approval, labels = {}, onOpenQueue }) {
  if (!approval) return null;
  return (
    <div className="approval-card" role="status">
      <div className="approval-head">
        <LockIcon size={15} />
        <strong>Aguardando analista</strong>
        {approval.review_id && <code>{approval.review_id}</code>}
      </div>
      <p>{approval.reason || 'Esta escrita exige aprovação humana pela política em hitl_policies.'}</p>
      <dl>
        <div><dt>agente</dt><dd>{labels[approval.agent] || approval.agent || '—'}</dd></div>
        <div><dt>escrita</dt><dd><code>{approval.tool}</code> {argsSummary(approval.args)}</dd></div>
      </dl>
      <p className="approval-foot">O turno está parado no checkpoint (LangGraph <code>interrupt</code>). Quando o analista decidir, a resposta chega aqui pelo Change Stream.</p>
      {onOpenQueue && <button type="button" className="ghost-btn" onClick={onOpenQueue}>Abrir fila do analista</button>}
    </div>
  );
}

const agentName = (key, labels = {}) => (key || '').split('+').filter(Boolean).map((part) => labels[part] || part).join(' + ');
const signature = (message, labels) => (message.consolidatedFrom
  ? authorLabel(message.consolidatedFrom)
  : agentName(message.agent, labels) || 'assistente');

export default function ChatPanel({ messages, input, setInput, send, busy, customerName, demos, suggestions, onSuggestion, labels, onOpenQueue }) {
  const listRef = useRef(null);
  // a conversa segue a última mensagem; a página não rola
  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [messages.length]);
  return (
    <section className="chat-panel" aria-label="Conversa com o cliente">
      <div className="messages" aria-live="polite" ref={listRef}>
        {messages.length === 0 && (
          <div className="welcome-message"><span>Olá, {customerName || 'cliente'}.</span><p>Em que posso ajudar com seu pedido, produto ou cobrança?</p></div>
        )}
        {messages.map((message, index) => (
          <div className={`message ${message.role} ${message.resumed ? 'is-resumed' : ''}`} key={index}>
            <small title={message.consolidatedFrom ? `Último especialista: ${agentName(message.agent, labels)}` : undefined}>{message.role === 'user' ? 'você' : `${signature(message, labels)}${message.resumed ? ' · após decisão humana' : ''}`}</small>
            {message.role === 'assistant' && message.cacheHit !== undefined && (
              <span className={`cache-badge ${message.cacheHit ? 'hit' : 'miss'}`}>
                {message.cacheHit ? 'cache hit · 0 tokens' : `cache miss · ${message.tokens ?? 0} tokens`}
              </span>
            )}
            <div>{message.role === 'assistant' ? <RichText text={message.text} /> : message.text}</div>
            {message.awaiting && <ApprovalCard approval={message.awaiting} labels={labels} onOpenQueue={onOpenQueue} />}
          </div>
        ))}
      </div>
      {suggestions?.length > 0 && !busy && (
        <div className="suggestions" aria-label="Próximos passos sugeridos">
          <span className="suggestions-label">posso seguir com</span>
          {suggestions.map((item) => (
            <button type="button" className="suggestion-chip" key={item.topic + item.label} onClick={() => onSuggestion(item.message)}>{item.label}</button>
          ))}
        </div>
      )}
      <form className="chat-form" onSubmit={(event) => { event.preventDefault(); send(); }}>
        <label className="sr-only" htmlFor="chat-input">Mensagem do cliente</label>
        <textarea id="chat-input" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } }} placeholder="Digite sua solicitação… (Enter envia, Shift+Enter quebra linha)" rows="3" />
        <button className="send-button" disabled={busy || !input.trim()}>{busy ? 'Coordenando…' : 'Enviar turno'}<span aria-hidden="true">↗</span></button>
      </form>
      <label className="demo-picker">
        <span>roteiro rápido</span>
        <select value="" onChange={(event) => { const demo = demos[Number(event.target.value)]; if (demo) setInput(demo.message); }}>
          <option value="">Carregar um cenário de demonstração…</option>
          {demos.map((demo, index) => <option key={demo.scenario_id} value={index}>{demo.label}</option>)}
        </select>
      </label>
    </section>
  );
}
