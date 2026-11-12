import { useMemo, useState } from 'react';
import ChatPanel from './ChatPanel.jsx';
import AgentGraph from './AgentGraph.jsx';
import UnderTheHood from './UnderTheHood.jsx';
import BlackboardView from './BlackboardView.jsx';
import Timeline from './Timeline.jsx';
import TypedMemory from './TypedMemory.jsx';
import { TurnCost } from './CostPanel.jsx';
import { deriveTurn } from '../graphModel.js';
import { useMediaQuery } from '../hooks.js';

const TABS = [
  ['quadro', 'Quadro do turno'],
  ['execucao', 'Execução'],
  ['custo', 'Custo'],
  ['memoria', 'Memória'],
];

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
  const [tab, setTab] = useState('quadro');
  const tabs = wide ? TABS : [...TABS.slice(0, 1), ['capo', 'Capô'], ...TABS.slice(1)];
  const activeTab = tabs.some(([key]) => key === tab) ? tab : 'quadro';
  const turn = useMemo(() => deriveTurn(timeline, busy ? null : lastRun, { live: busy }), [timeline, busy, lastRun]);

  return (
    <div className="chat-stage">
      {header}
      <div className="stage-grid">
        <ChatPanel {...chat} labels={labels} />
        <section className="stage-center" aria-label="Coordenação do turno">
          <AgentGraph agents={agents} turn={turn} live={busy} labels={labels} />
          <div className="stage-tabs">
            <div className="stage-tablist" role="tablist" aria-label="Detalhes do turno">
              {tabs.map(([key, label]) => (
                <button key={key} type="button" role="tab" id={`stage-tab-${key}`} aria-selected={activeTab === key}
                  aria-controls={`stage-panel-${key}`} className={activeTab === key ? 'is-on' : ''} onClick={() => setTab(key)}>
                  {label}{key === 'execucao' && timeline.length ? <span className="tab-count">{timeline.length}</span> : null}
                </button>
              ))}
            </div>
            <div className="stage-panel" role="tabpanel" id={`stage-panel-${activeTab}`} aria-labelledby={`stage-tab-${activeTab}`}>
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
