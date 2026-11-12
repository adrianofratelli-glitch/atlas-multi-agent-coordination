import { useEffect, useMemo, useState } from 'react';
import ChatPanel from './ChatPanel.jsx';
import AgentGraph from './AgentGraph.jsx';
import UnderTheHood from './UnderTheHood.jsx';
import BlackboardView from './BlackboardView.jsx';
import Timeline from './Timeline.jsx';
import TypedMemory from './TypedMemory.jsx';
import TurnNarrative from './TurnNarrative.jsx';
import { TurnCost } from './CostPanel.jsx';
import { deriveTurn } from '../graphModel.js';
import { useMediaQuery, useReducedMotion } from '../hooks.js';

const TABS = [
  ['narrativa', 'Como o time resolveu'],
  ['quadro', 'Quadro do turno'],
  ['execucao', 'Execução'],
  ['custo', 'Custo'],
  ['memoria', 'Memória'],
];
const CAPO_TAB = ['capo', 'Capô'];

// One line under the tab list: what the open panel shows, for an audience that is not technical.
const TAB_HINTS = {
  narrativa: 'Cada etapa do turno em linguagem simples, na ordem em que aconteceu. Passe o mouse para ver no grafo; clique para fixar.',
  quadro: 'O documento do turno em turn_blackboard: o plano do orquestrador e o que cada agente leu, gravou e concluiu.',
  execucao: 'Todos os eventos do turno como chegaram do backend (SSE), com coleção, filtro e resultado.',
  custo: 'Tokens, chamadas ao modelo e custo estimado deste turno, por agente.',
  memoria: 'O que o servidor guardou sobre este cliente: fatos, episódios e memória de curto prazo.',
  capo: 'Cada operação no MongoDB deste turno, com o filtro real que o servidor executou.',
};

/**
 * Tela de Chat: conversa fixa à esquerda, grafo ao vivo no centro e o capô à direita. Os painéis
 * de detalhe ficam em abas sob o grafo, para a conversa nunca ser empurrada para baixo da dobra.
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
  const [hovered, setHovered] = useState(null);
  const [pinned, setPinned] = useState(null);
  // A new turn starts with no highlight: a pin from the previous turn would point at stale steps.
  useEffect(() => { setPinned(null); setHovered(null); }, [lastRun, timeline.length === 0]);
  const highlight = (hovered || pinned)?.focus || null;

  const jumpToOp = (index) => {
    if (!wide) setTab('capo');
    const reveal = () => {
      const row = document.getElementById(`hood-op-${index}`);
      if (!row) return;
      row.scrollIntoView({ block: 'nearest', behavior: reducedMotion ? 'auto' : 'smooth' });
      row.classList.remove('is-target');
      void row.offsetWidth;
      row.classList.add('is-target');
      row.focus({ preventScroll: true });
      setTimeout(() => row.classList.remove('is-target'), 1800);
    };
    requestAnimationFrame(() => requestAnimationFrame(reveal));
  };

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
                  {label}{key === 'execucao' && timeline.length ? <span className="tab-count">{timeline.length}</span> : null}
                </button>
              ))}
            </div>
            <p className="stage-tab-hint">{TAB_HINTS[activeTab]}</p>
            <div className="stage-panel" role="tabpanel" id={`stage-panel-${activeTab}`} aria-labelledby={`stage-tab-${activeTab}`}>
              {activeTab === 'narrativa' && (
                <TurnNarrative events={timeline} live={busy} labels={labels} pinned={pinned?.id || null}
                  onFocus={setHovered} onPin={setPinned} onJumpToOp={jumpToOp} />
              )}
              {activeTab === 'quadro' && <BlackboardView {...board} labels={labels} timeline={timeline} conversationId={conversationId} />}
              {activeTab === 'execucao' && <Timeline events={timeline} live={busy} />}
              {activeTab === 'custo' && <TurnCost run={lastRun} timeline={timeline} labels={labels} />}
              {activeTab === 'memoria' && <TypedMemory {...typedMemory} labels={labels} />}
              {activeTab === 'capo' && <UnderTheHood events={timeline} live={busy} streamFeed={streamFeed} labels={labels} />}
            </div>
          </div>
        </section>
        {wide && <aside className="stage-side"><UnderTheHood events={timeline} live={busy} streamFeed={streamFeed} labels={labels} /></aside>}
      </div>
    </div>
  );
}
