import { useEffect, useMemo, useState } from 'react';
import ChatPanel from './ChatPanel.jsx';
import AgentGraph from './AgentGraph.jsx';
import UnderTheHood from './UnderTheHood.jsx';
import BlackboardView from './BlackboardView.jsx';
import TypedMemory from './TypedMemory.jsx';
import TurnNarrative from './TurnNarrative.jsx';
import { TurnCost } from './CostPanel.jsx';
import { deriveTurn } from '../graphModel.js';
import { narrateTurn, stepByEvent } from '../turnNarrative.js';
import { hoodRows, rowsForStep } from '../mongoOps.js';
import { useMediaQuery, useReducedMotion } from '../hooks.js';

const TABS = [
  ['narrativa', 'Como o time resolveu'],
  ['quadro', 'Quadro do turno'],
  ['custo', 'Custo'],
  ['memoria', 'Memória'],
];
const CAPO_TAB = ['capo', 'Capô'];

// One line under the tab list: what the open panel shows, for an audience that is not technical.
const TAB_HINTS = {
  narrativa: 'Cada etapa do turno em linguagem simples, na ordem em que aconteceu. Passe o mouse para ver no grafo e no capô; clique para fixar.',
  quadro: 'O documento do turno em turn_blackboard: o plano do orquestrador e o que cada agente leu, gravou e concluiu.',
  custo: 'Tokens, chamadas ao modelo e custo estimado deste turno, por agente.',
  memoria: 'O que o servidor guardou sobre este cliente: fatos, episódios e memória de curto prazo.',
  capo: 'Cada operação no MongoDB e cada evento deste turno, com o filtro real. "ver detalhe" abre o resultado e a query.',
};

// Flash a capô row (chip of the narrative → exact operation).
function flash(row) {
  row.classList.remove('is-target');
  void row.offsetWidth;
  row.classList.add('is-target');
  setTimeout(() => row.classList.remove('is-target'), 1800);
}

// Scroll only the capô list (never the page) so the row sits near the top of it.
function scrollHoodTo(index, reducedMotion) {
  const row = document.getElementById(`hood-op-${index}`);
  const list = row?.closest('.hood-list');
  if (!row || !list) return null;
  // Wide: the list scrolls. Narrow (capô as a tab): the list grows and the tab panel scrolls.
  const scroller = list.scrollHeight > list.clientHeight + 1 ? list : (row.closest('.stage-panel') || list);
  const top = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 8;
  scroller.scrollTo({ top: Math.max(0, top), behavior: reducedMotion ? 'auto' : 'smooth' });
  return row;
}

/**
 * Tela de Chat: conversa fixa à esquerda; no centro o grafo compacto e, abaixo dele, a narrativa
 * "Como o time resolveu" como leitura principal (Quadro, Custo e Memória em abas secundárias); à
 * direita o capô, com uma linha por evento do turno. Etapa da narrativa ↔ linhas do capô ↔ nós do
 * grafo ficam ligados por `step.events` (turnNarrative.js) e pelo índice do evento na timeline.
 */
export default function ChatStage(props) {
  const {
    agents, labels, timeline, busy, lastRun, conversationId, board, typedMemory, streamFeed,
    header, chat,
  } = props;
  const wide = useMediaQuery('(min-width: 1240px)');
  const reducedMotion = useReducedMotion();
  const [tab, setTab] = useState('narrativa');
  const tabs = wide ? TABS : [...TABS.slice(0, 2), CAPO_TAB, ...TABS.slice(2)];
  const activeTab = tabs.some(([key]) => key === tab) ? tab : 'narrativa';
  const turn = useMemo(() => deriveTurn(timeline, busy ? null : lastRun, { live: busy }), [timeline, busy, lastRun]);
  const steps = useMemo(() => narrateTurn(timeline, { labels, live: busy }), [timeline, labels, busy]);
  const ownerOf = useMemo(() => stepByEvent(steps), [steps]);
  const rows = useMemo(() => hoodRows(timeline), [timeline]);
  // hovered: { step, from: 'narrative' | 'hood' }; pinned: a step (click).
  const [hovered, setHovered] = useState(null);
  const [pinned, setPinned] = useState(null);
  // A new turn starts with no highlight: a pin from the previous turn would point at stale steps.
  useEffect(() => { setPinned(null); setHovered(null); }, [lastRun, timeline.length === 0]);
  // Switching tabs unmounts the hovered list before its mouseleave fires: drop the hover with it.
  useEffect(() => { setHovered(null); }, [activeTab]);
  const focusStep = hovered?.step || pinned;
  const highlight = focusStep?.focus || null;
  const linkedRows = useMemo(() => new Set(focusStep?.events || []), [focusStep]);

  const afterPaint = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn));

  const jumpToOp = (index) => {
    if (!wide) setTab('capo');
    afterPaint(() => {
      const row = scrollHoodTo(index, reducedMotion);
      if (!row) return;
      flash(row);
      row.focus({ preventScroll: true });
    });
  };

  const pinStep = (step) => {
    setPinned(step);
    if (!step) return;
    const [first] = rowsForStep(step, rows);
    if (first !== undefined) afterPaint(() => scrollHoodTo(first, reducedMotion));
  };

  const hoverRow = (index) => {
    const step = index === null ? null : ownerOf.get(index);
    setHovered(step ? { step, from: 'hood' } : null);
  };

  const hood = (
    <UnderTheHood events={timeline} rows={rows} live={busy} streamFeed={streamFeed} labels={labels}
      linked={linkedRows} onHoverRow={hoverRow} />
  );

  return (
    <div className="chat-stage">
      {header}
      <div className="stage-grid">
        <ChatPanel {...chat} labels={labels} />
        <section className="stage-center" aria-label="Coordenação do turno">
          <AgentGraph agents={agents} turn={turn} live={busy} labels={labels} highlight={highlight} />
          <div className="stage-tabs">
            <div className="stage-tablist" role="tablist" aria-label="Detalhes do turno">
              {tabs.map(([key, label]) => (
                <button key={key} type="button" role="tab" id={`stage-tab-${key}`} aria-selected={activeTab === key}
                  aria-controls={`stage-panel-${key}`} className={activeTab === key ? 'is-on' : ''} onClick={() => setTab(key)}>
                  {label}{key === 'capo' && rows.length ? <span className="tab-count">{rows.length}</span> : null}
                </button>
              ))}
            </div>
            <p className="stage-tab-hint">{TAB_HINTS[activeTab]}</p>
            <div className={`stage-panel panel-${activeTab}`} role="tabpanel" id={`stage-panel-${activeTab}`} aria-labelledby={`stage-tab-${activeTab}`}>
              {activeTab === 'narrativa' && (
                <TurnNarrative steps={steps} live={busy} labels={labels} pinned={pinned?.id || null}
                  linked={hovered?.from === 'hood' ? hovered.step.id : null}
                  onFocus={(step) => setHovered(step ? { step, from: 'narrative' } : null)} onPin={pinStep} onJumpToOp={jumpToOp} />
              )}
              {activeTab === 'quadro' && <BlackboardView {...board} labels={labels} timeline={timeline} conversationId={conversationId} />}
              {activeTab === 'custo' && <TurnCost run={lastRun} timeline={timeline} labels={labels} />}
              {activeTab === 'memoria' && <TypedMemory {...typedMemory} labels={labels} />}
              {activeTab === 'capo' && hood}
            </div>
          </div>
        </section>
        {wide && <aside className="stage-side">{hood}</aside>}
      </div>
    </div>
  );
}
