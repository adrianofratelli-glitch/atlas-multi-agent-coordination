function when(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('pt-BR');
}

/** Memória por tipo: semântica (fatos), episódica (episódios) e procedural (como o time trabalha). */
export default function TypedMemory({ memory, error, labels = {} }) {
  if (error) return <p className="panel-empty">Não foi possível ler a memória: {error}</p>;
  if (!memory) return <p className="panel-empty">Lendo a memória do cliente…</p>;
  const semantic = memory.semantic?.items || [];
  const episodic = memory.episodic?.items || [];
  const procedural = memory.procedural || {};
  return (
    <div className="typed-memory">
      <section>
        <h4>Semântica <code>{memory.semantic?.collection}</code></h4>
        <p className="memory-hint">Fatos sobre o cliente, com supersessão em transação.</p>
        {semantic.length ? (
          <ul>{semantic.map((item, index) => (
            <li key={item.fact || index}><span>{item.fact || item.value || JSON.stringify(item)}</span>{item.category && <small>{item.category}</small>}</li>
          ))}</ul>
        ) : <p className="panel-empty">Nenhum fato ativo. Diga, por exemplo, “me chame de Aninha e nunca me ofereça acima de R$ 400”.</p>}
      </section>
      <section>
        <h4>Episódica <code>{memory.episodic?.collection}</code></h4>
        <p className="memory-hint">Só rótulos (intenção + agente), nunca o texto da conversa.</p>
        {episodic.length ? (
          <ul>{episodic.map((item, index) => (
            <li key={`${item.created_at}-${index}`}><span>{item.intent || item.kind} · {labels[item.agent] || item.agent}</span><small>{when(item.created_at)}</small></li>
          ))}</ul>
        ) : <p className="panel-empty">Nenhum episódio gravado ainda.</p>}
      </section>
      <section>
        <h4>Procedural <code>{(procedural.collections || []).join(' · ')}</code></h4>
        <p className="memory-hint">Como o time trabalha: persona e tools por agente, regras de roteamento, políticas de aprovação.</p>
        <ul>
          {(procedural.agents || []).map((agent) => (
            <li key={agent.agent_key}><span>{labels[agent.agent_key] || agent.agent_key}</span><small>{(agent.allowed_tools || []).join(', ') || 'sem tools'}</small></li>
          ))}
        </ul>
        <p className="memory-foot">{procedural.routing_rules ?? 0} regras de roteamento · {(procedural.hitl_policies || []).filter((policy) => policy.active !== false).length} política(s) de aprovação ativas</p>
      </section>
    </div>
  );
}
