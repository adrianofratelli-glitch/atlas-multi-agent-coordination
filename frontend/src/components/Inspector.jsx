function JsonList({ data, empty }) {
  if (!data?.length) return <p className="inspector-empty">{empty}</p>;
  return data.map((item, index) => <pre className="mini-json" key={item._id || index}>{JSON.stringify(item, null, 2)}</pre>);
}

export default function Inspector({ tab, agents, handoffs, memory, guardrails, metrics, castKeys = [] }) {
  return (
    <aside className="inspector">
      <div className="inspector-head"><span className="status-light" /> inspetor MongoDB</div>
      <div className="inspector-body">
        {tab === 'agents' && agents.map((agent) => {
          const inUse = castKeys.includes(agent.agent_key);
          return (
            <div className={`agent-row ${inUse ? 'in-use' : ''}`} key={agent.agent_key}>
              <span className={`agent-dot ${agent.active ? '' : 'inactive'} ${inUse ? 'pulsing' : ''}`} />
              <div><strong>{agent.label}</strong><code>{agent.agent_key}</code></div>
              {inUse ? <span className="agent-in-use-tag">em uso neste turno</span> : <code>{agent.model}</code>}
            </div>
          );
        })}
        {tab === 'handoffs' && <JsonList data={handoffs} empty="Nenhum handoff nesta conversa." />}
        {tab === 'memory' && <JsonList data={memory} empty="Nenhum fato de longa duração para este cliente." />}
        {tab === 'guardrails' && <JsonList data={guardrails} empty="Nenhum evento de guardrail." />}
        {tab === 'metrics' && <pre className="mini-json">{JSON.stringify(metrics, null, 2)}</pre>}
      </div>
    </aside>
  );
}

