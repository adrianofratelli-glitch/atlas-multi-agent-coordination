import { useMemo } from 'react';
import { narrateTurn, describeNext } from '../turnNarrative.js';

const STATUS_LABEL = {
  write: 'gravou dado', blocked: 'bloqueado', paused: 'aguarda analista', degraded: 'degradado', refused: 'escrita recusada',
};

/**
 * "Como o time resolveu": o turno contado em etapas numeradas, em linguagem simples, derivado
 * só dos eventos do backend (turnNarrative.js). Passar o mouse/foco numa etapa destaca o nó e a
 * aresta no grafo; clicar fixa o destaque; o chip da coleção leva à linha no "Por baixo do capô".
 */
export default function TurnNarrative({ events = [], live = false, labels = {}, pinned = null, onFocus, onPin, onJumpToOp }) {
  const steps = useMemo(() => narrateTurn(events, { labels, live }), [events, labels, live]);

  if (!steps.length) {
    return (
      <p className="panel-empty">
        {live ? 'Coordenando o turno: as etapas aparecem aqui assim que o backend as emite.'
          : 'Envie uma mensagem: cada etapa do turno aparece aqui, em linguagem simples, enquanto acontece.'}
      </p>
    );
  }

  return (
    <ol className="narrative" aria-live={live ? 'polite' : undefined}>
      {steps.map((step, index) => {
        const isPinned = pinned === step.id;
        return (
          <li key={step.id} className={`nstep kind-${step.kind} is-${step.status} ${step.current ? 'is-current' : ''} ${isPinned ? 'is-pinned' : ''}`}
            onMouseEnter={() => onFocus?.(step)} onMouseLeave={() => onFocus?.(null)}
            onFocus={() => onFocus?.(step)} onBlur={() => onFocus?.(null)}>
            <span className="nstep-num" aria-hidden="true">{index + 1}</span>
            <div className="nstep-body">
              <h4>
                <button type="button" className="nstep-title" aria-pressed={isPinned} onClick={() => onPin?.(isPinned ? null : step)}
                  title={isPinned ? 'Soltar o destaque no grafo' : 'Fixar o destaque desta etapa no grafo'}>
                  <span className="sr-only">Etapa {index + 1}: </span>{step.title}
                </button>
                {STATUS_LABEL[step.status] && <span className={`nstep-status status-${step.status}`}>{STATUS_LABEL[step.status]}</span>}
              </h4>
              {step.task && <p className="nstep-task">Tarefa: {step.task}</p>}
              <p className="nstep-text">{step.text}</p>
              {step.items?.length > 0 && (
                <ol className="nstep-items">
                  {step.items.map((item, i) => <li key={`${item.agent}-${i}`}><b>{item.label}</b> {item.task || 'sem tarefa descrita'}</li>)}
                </ol>
              )}
              {(step.infos || []).map((info) => <p key={info} className="nstep-info">{info}</p>)}
              {step.notes.map((note) => <p key={note} className="nstep-note">{note}</p>)}
              {step.next && <p className={`nstep-next ${step.next.revisit ? 'is-revisit' : ''}`}><span aria-hidden="true">↳ </span>{describeNext(step.next, labels)}</p>}
              {step.why && <p className="nstep-why"><span>Por que importa:</span> {step.why}</p>}
              {step.mongo.length > 0 && (
                <div className="nstep-mongo" aria-label="MongoDB nesta etapa">
                  {step.mongo.map((ref) => (
                    <a key={`${ref.index}-${ref.collection}`} href={`#hood-op-${ref.index}`} className={`nstep-ref ${ref.write ? 'is-write' : ''}`}
                      onClick={(event) => { event.preventDefault(); onJumpToOp?.(ref.index); }} title="Ver esta operação em Por baixo do capô">
                      <code>{ref.collection}</code>{ref.tag && <span>{ref.tag}</span>}
                    </a>
                  ))}
                </div>
              )}
            </div>
          </li>
        );
      })}
      {live && <li className="nstep-pending"><span className="pending-dot" /> aguardando a próxima etapa…</li>}
    </ol>
  );
}
