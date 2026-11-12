import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import PageHead from './PageHead.jsx';
import GuardrailsPanel from './GuardrailsPanel.jsx';

const EFFECT = { read: 'leitura', write: 'escrita', external: 'externa' };

function AdminToggle({ adminMode, setAdminMode }) {
  return (
    <label className="admin-toggle">
      <input type="checkbox" checked={adminMode} onChange={(event) => setAdminMode(event.target.checked)} />
      <span className="admin-toggle-track"><span className="admin-toggle-thumb" /></span>
      modo admin {adminMode ? '(ligado)' : '(desligado)'}
    </label>
  );
}

function Switch({ on, disabled, label, onToggle }) {
  return (
    <button type="button" className={`switch ${on ? 'is-on' : ''}`} aria-pressed={on} aria-label={label} disabled={disabled} onClick={onToggle}>
      <span />
    </button>
  );
}

function AgentCard({ agent, tools, enabled, adminMode, models, onPatch, busy }) {
  const [model, setModel] = useState(agent.model || '');
  useEffect(() => setModel(agent.model || ''), [agent.model]);
  const own = tools.filter((tool) => tool.agent === agent.agent_key);
  const enabledSet = new Set(enabled || []);
  const toggleTool = (name) => {
    const next = enabledSet.has(name) ? [...enabledSet].filter((item) => item !== name) : [...enabledSet, name];
    onPatch(agent.agent_key, { allowed_tools: next });
  };
  return (
    <article className={`agent-card ${agent.active ? '' : 'disabled'}`}>
      <div className="agent-card-head">
        <div>
          <h3>{agent.label}</h3>
          <code>{agent.agent_key}</code>
        </div>
        <Switch on={agent.active} disabled={!adminMode || busy} label={`${agent.active ? 'Desativar' : 'Ativar'} ${agent.label}`}
          onToggle={() => onPatch(agent.agent_key, { active: !agent.active })} />
      </div>
      <p className="agent-persona">{agent.persona}</p>
      <form className="agent-model" onSubmit={(event) => { event.preventDefault(); if (model && model !== agent.model) onPatch(agent.agent_key, { model }); }}>
        <label htmlFor={`model-${agent.agent_key}`}>modelo</label>
        <input id={`model-${agent.agent_key}`} list="known-models" value={model} disabled={!adminMode || busy} onChange={(event) => setModel(event.target.value)} />
        {adminMode && model !== agent.model && <button type="submit" className="ghost-btn" disabled={busy}>Salvar</button>}
      </form>
      <dl className="agent-budget"><div><dt>budget do turno</dt><dd>{agent.max_turn_tokens ?? '—'} tok</dd></div><div><dt>saída máx.</dt><dd>{agent.max_output_tokens ?? '—'} tok</dd></div></dl>
      {own.length ? (
        <ul className="tool-list" aria-label={`Tools de ${agent.label}`}>
          {own.map((tool) => (
            <li key={tool.name} className={enabledSet.has(tool.name) ? '' : 'is-off'}>
              <div>
                <code>{tool.name}</code>
                <span className={`effect effect-${tool.effect}`}>{EFFECT[tool.effect] || tool.effect}</span>
                <p>{tool.description}</p>
              </div>
              <Switch on={enabledSet.has(tool.name)} disabled={!adminMode || busy} label={`${enabledSet.has(tool.name) ? 'Desligar' : 'Ligar'} ${tool.name}`} onToggle={() => toggleTool(tool.name)} />
            </li>
          ))}
        </ul>
      ) : <p className="tool-none">Sem tool calling: {(agent.allowed_tools || []).join(', ') || 'roteia e consolida'}.</p>}
    </article>
  );
}

function HitlPolicies({ adminMode }) {
  const [policies, setPolicies] = useState([]);
  const [error, setError] = useState('');
  const load = () => api.hitlPolicies().then((data) => { setPolicies(Array.isArray(data) ? data : []); setError(''); }).catch((err) => setError(err.message));
  useEffect(() => { if (adminMode) load(); }, [adminMode]);
  if (!adminMode) return <p className="panel-empty">Ligue o modo admin para ver e alterar as políticas de aprovação humana.</p>;
  if (error) return <p className="panel-empty">{error}</p>;
  if (!policies.length) return <p className="panel-empty">Nenhuma política cadastrada: vale a política padrão do servidor.</p>;
  const toggle = async (policy) => {
    try { await api.updateHitlPolicy(policy.policy_id, !policy.active); await load(); } catch (err) { setError(err.message); }
  };
  return (
    <ul className="policy-list">
      {policies.map((policy) => (
        <li key={policy.policy_id} className={policy.active ? '' : 'is-off'}>
          <div>
            <code>{policy.policy_id}</code>
            <p>{policy.reason}</p>
            <small><code>{policy.tool}</code> quando <code>{JSON.stringify(policy.when || {})}</code></small>
          </div>
          <Switch on={policy.active !== false} label={`${policy.active ? 'Desativar' : 'Ativar'} ${policy.policy_id}`} onToggle={() => toggle(policy)} />
        </li>
      ))}
    </ul>
  );
}

export default function AgentsPage({ agents, adminMode, setAdminMode, reload }) {
  const [view, setView] = useState('time');
  const [catalog, setCatalog] = useState({ tools: [], enabled_by_agent: {} });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const loadTools = () => api.tools().then(setCatalog).catch((err) => setError(err.message));
  useEffect(() => { loadTools(); }, []);
  const models = useMemo(() => [...new Set(agents.flatMap((agent) => [agent.model, agent.fallback_model]).filter(Boolean))], [agents]);
  const patch = async (key, update) => {
    setBusy(true); setError('');
    try { await api.updateAgent(key, update); await Promise.all([reload(), loadTools()]); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  return (
    <section className="full-page-section">
      <PageHead source="multiagent_brain.agent_registry" title="O time de agentes é uma collection" actions={<AdminToggle adminMode={adminMode} setAdminMode={setAdminMode} />}>
        Modelo, tools e políticas mudam por documento, sem deploy. {agents.length} agentes no registry, todos respondem de fato. Desligar uma tool tira ela do tool calling no próximo turno.
      </PageHead>
      <div className="segmented page-switch" role="group" aria-label="Seção">
        <button type="button" aria-pressed={view === 'time'} className={view === 'time' ? 'is-on' : ''} onClick={() => setView('time')}>Time e tools</button>
        <button type="button" aria-pressed={view === 'guardrails'} className={view === 'guardrails' ? 'is-on' : ''} onClick={() => setView('guardrails')}>Guardrails</button>
      </div>
      {error && <div className="error-banner">{error}<button aria-label="Fechar aviso" onClick={() => setError('')}>×</button></div>}
      {view === 'time' && (
        <>
          <datalist id="known-models">{models.map((model) => <option key={model} value={model} />)}</datalist>
          <div className="agent-grid">
            {agents.map((agent) => (
              <AgentCard key={agent.agent_key} agent={agent} tools={catalog.tools || []} enabled={catalog.enabled_by_agent?.[agent.agent_key]}
                adminMode={adminMode} models={models} onPatch={patch} busy={busy} />
            ))}
          </div>
          <section className="page-block">
            <h3>Aprovação humana antes de escrever <code>hitl_policies</code></h3>
            <HitlPolicies adminMode={adminMode} />
          </section>
        </>
      )}
      {view === 'guardrails' && <GuardrailsPanel adminMode={adminMode} />}
    </section>
  );
}
