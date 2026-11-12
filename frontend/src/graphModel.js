// Pure derivation of the live coordination graph from the turn timeline (SSE events) and the
// final ChatResponse. No React here: everything the SVG draws is computed by these functions,
// so the drawing is testable with `node --test` and stays faithful to what the backend emitted.

export const HUB = 'orchestrator';
export const HUMAN = 'human_reviewer';

// Collections whose writes change the customer's world (the allowlist in AGENTS.md).
export const BUSINESS_COLLECTIONS = new Set(['orders', 'support_tickets', 'redemptions', 'shipments', 'loyalty_accounts']);

// Stable clockwise order around the hub, so the same agent always sits in the same place.
const SPECIALIST_ORDER = ['order_agent', 'billing_agent', 'warranty_agent', 'support_agent',
  'product_agent', 'loyalty_agent', 'logistics_agent'];

function specialistRank(key) {
  const index = SPECIALIST_ORDER.indexOf(key);
  return index === -1 ? SPECIALIST_ORDER.length : index;
}

/** Hub in the centre, specialists on an ellipse. Coordinates are in viewBox units. */
export function layoutGraph(agents = [], { width = 640, height = 380, padX = 78, padY = 56 } = {}) {
  const cx = width / 2;
  const cy = height / 2;
  const rx = Math.max(40, width / 2 - padX);
  const ry = Math.max(40, height / 2 - padY);
  const hub = agents.find((agent) => agent.agent_key === HUB);
  const ring = agents
    .filter((agent) => agent.agent_key !== HUB)
    .sort((a, b) => specialistRank(a.agent_key) - specialistRank(b.agent_key) || a.agent_key.localeCompare(b.agent_key));
  const nodes = [];
  nodes.push({ key: HUB, label: hub?.label || 'Orquestrador', active: hub ? hub.active !== false : true,
    model: hub?.model, x: cx, y: cy, hub: true });
  ring.forEach((agent, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / Math.max(1, ring.length);
    nodes.push({ key: agent.agent_key, label: agent.label || agent.agent_key, active: agent.active !== false,
      model: agent.model, x: round(cx + rx * Math.cos(angle)), y: round(cy + ry * Math.sin(angle)), hub: false });
  });
  return { width, height, nodes, byKey: Object.fromEntries(nodes.map((node) => [node.key, node])) };
}

function round(value) {
  return Math.round(value * 10) / 10;
}

/**
 * Quadratic curve between two nodes. The control point is pushed along the edge normal, so
 * A→B and B→A (a controlled revisit) bend to opposite sides and never overlap.
 */
export function edgePath(from, to, { bend = 0.16, radiusFrom = 30, radiusTo = 30 } = {}) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.hypot(dx, dy) || 1;
  const nx = -dy / length;
  const ny = dx / length;
  const mx = (from.x + to.x) / 2 + nx * length * bend;
  const my = (from.y + to.y) / 2 + ny * length * bend;
  const start = towards(from, { x: mx, y: my }, radiusFrom, 0);
  const end = towards(to, { x: mx, y: my }, radiusTo, 6);
  return { d: `M ${round(start.x)} ${round(start.y)} Q ${round(mx)} ${round(my)} ${round(end.x)} ${round(end.y)}`,
    mid: { x: round((start.x + 2 * mx + end.x) / 4), y: round((start.y + 2 * my + end.y) / 4) } };
}

// `distance` may be a number or a function of the unit direction (rectangular nodes).
function towards(point, target, distance, gap = 0) {
  const dx = target.x - point.x;
  const dy = target.y - point.y;
  const length = Math.hypot(dx, dy) || 1;
  const ux = dx / length;
  const uy = dy / length;
  const reach = (typeof distance === 'function' ? distance(ux, uy) : distance) + gap;
  return { x: point.x + ux * reach, y: point.y + uy * reach };
}

/** Distance from a rectangle's centre to its border along a unit direction. */
export function rectReach(halfWidth, halfHeight) {
  return (ux, uy) => Math.min(ux ? halfWidth / Math.abs(ux) : Infinity, uy ? halfHeight / Math.abs(uy) : Infinity);
}

const isTitle = (event, prefix) => typeof event?.title === 'string' && event.title.startsWith(prefix);

/**
 * Everything the graph needs to know about one turn.
 * - events: live SSE timeline (or the final run.timeline)
 * - run: final ChatResponse, when the turn finished (optional)
 */
export function deriveTurn(events = [], run = null, { live = false } = {}) {
  const fresh = (events || []).filter((event) => event && !event.replayed);
  const edges = [];
  const plan = [];
  const writes = {};
  const tools = {};
  const touched = new Set();
  let paused = null;
  let fanout = false;
  let consolidated = false;
  let resumed = false;
  let blocked = false;
  let cacheHit = false;
  let active = null;
  let step = 0;

  const addWrite = (agent, what) => {
    if (!agent || !what) return;
    writes[agent] = writes[agent] || [];
    if (!writes[agent].includes(what)) writes[agent].push(what);
  };
  const addTool = (agent, name) => {
    if (!agent || !name) return;
    tools[agent] = tools[agent] || [];
    if (!tools[agent].includes(name)) tools[agent].push(name);
  };

  for (const event of fresh) {
    const result = event.result && typeof event.result === 'object' ? event.result : {};
    if (event.agent) touched.add(event.agent);
    if (event.category === 'guardrail' && result.blocked === true && !isTitle(event, 'Escrita recusada')) blocked = true;
    if (event.category === 'cache' && result.hit === true) cacheHit = true;

    if (event.category === 'agent' && isTitle(event, 'Roteamento')) {
      if (event.agent && event.agent !== HUB) {
        step += 1;
        edges.push({ from: HUB, to: event.agent, kind: 'route', step });
      }
    } else if (event.category === 'fanout') {
      fanout = true;
      step += 1;
      for (const agent of result.agents || []) edges.push({ from: HUB, to: agent, kind: 'fanout', step });
    } else if (event.category === 'handoff' && result.to_agent && event.agent) {
      step += 1;
      const kind = /degradado/i.test(event.title) ? 'degraded' : result.revisit ? 'revisit' : 'handoff';
      edges.push({ from: event.agent, to: result.to_agent, kind, step, task: result.task, reason: event.reason });
    } else if (event.category === 'coordination') {
      if (Array.isArray(result.plan)) {
        plan.splice(0, plan.length, ...result.plan.map((item, index) => ({ agent: item.agent, task: item.task, step: index + 1 })));
      }
      if (/consolid/i.test(event.title || '')) consolidated = true;
      for (const tool of result.writes || []) addWrite(event.agent, tool);
    }

    if (event.category === 'agent' && Array.isArray(result.tools_called)) {
      for (const name of result.tools_called) addTool(event.agent, name);
      for (const name of result.writes || []) addWrite(event.agent, name);
    }
    const toolMatch = /^Tool ([a-z_]+)/.exec(event.title || '');
    if (toolMatch) addTool(event.agent, toolMatch[1]);
    if (event.op === 'write' && BUSINESS_COLLECTIONS.has(event.collection)) addWrite(event.agent, event.collection);

    if (isTitle(event, 'Escrita pausada')) {
      paused = { agent: event.agent, review_id: result.review_id, tool: result.tool, args: result.args,
        policy: result.policy, reason: event.reason };
    }
    if (isTitle(event, 'Turno retomado')) {
      resumed = true;
      paused = null;
    }
    if (event.agent && event.category !== 'cache') active = event.agent;
  }

  if (run?.awaiting_approval) {
    const approval = run.awaiting_approval;
    paused = { agent: approval.agent || paused?.agent, review_id: approval.review_id || paused?.review_id,
      tool: approval.tool || paused?.tool, args: approval.args || paused?.args,
      policy: paused?.policy, reason: approval.reason || paused?.reason };
  }

  // Path in order of action. The backend's agent_path is authoritative once the turn ends; while
  // live (or for older payloads without it) we rebuild it from route + handoff edges.
  let path = Array.isArray(run?.agent_path) && run.agent_path.length ? [...run.agent_path] : [];
  if (!path.length) {
    for (const edge of edges) {
      if (edge.kind === 'route' || edge.kind === 'fanout') {
        if (!path.includes(edge.to) || edge.kind === 'route') path.push(edge.to);
      } else {
        path.push(edge.to);
      }
    }
    if (!path.length && run?.active_agent) path = run.active_agent.split('+').filter(Boolean);
  }

  // A finished payload with agent_path but no handoff events (e.g. a resumed conversation that
  // only kept the path): draw the chain from the path so the graph is never blank.
  if (!edges.length && path.length && !cacheHit) {
    if (run?.route_source === 'fanout') {
      path.forEach((agent) => edges.push({ from: HUB, to: agent, kind: 'fanout', step: 1 }));
      fanout = true;
    } else {
      path.forEach((agent, index) => edges.push({ from: index === 0 ? HUB : path[index - 1], to: agent,
        kind: index === 0 ? 'route' : 'handoff', step: index + 1 }));
    }
  }
  if (run?.route_source === 'fanout') fanout = true;
  if (consolidated && path.length) {
    edges.push({ from: path[path.length - 1], to: HUB, kind: 'return', step: step + 1 });
  }

  const visited = [...new Set([...path, ...edges.flatMap((edge) => [edge.from, edge.to])])].filter((key) => key !== HUMAN);
  const planIndex = Object.fromEntries(plan.map((item) => [item.agent, item.step]));
  return {
    edges, plan, planIndex, path, visited, writes, tools, paused, fanout, consolidated, resumed, blocked, cacheHit,
    active: live ? active : null,
    touched: [...touched],
    hops: path.length,
  };
}

/** One-line status of the turn for the caption under the graph (pt-BR, user-facing). */
export function describeTurn(turn, { live = false, labels = {} } = {}) {
  if (!turn) return '';
  if (turn.blocked) return 'Guardrail bloqueou o turno antes de qualquer agente.';
  if (turn.cacheHit) return 'Resposta reaproveitada do cache semântico: nenhum agente precisou rodar.';
  if (turn.paused) return `Escrita pausada em ${labels[turn.paused.agent] || turn.paused.agent || 'agente'}: aguardando aprovação do analista.`;
  const parts = [];
  if (turn.plan.length) parts.push(`plano com ${turn.plan.length} tarefa(s)`);
  if (turn.fanout) parts.push('despacho paralelo');
  if (turn.hops) parts.push(`${turn.hops} passo(s) de agente`);
  const writeCount = Object.values(turn.writes).reduce((sum, list) => sum + list.length, 0);
  if (writeCount) parts.push(`${writeCount} escrita(s)`);
  if (turn.consolidated) parts.push('resposta consolidada pelo orquestrador');
  if (turn.resumed) parts.push('retomado após decisão humana');
  if (!parts.length) return live ? 'Roteando o turno…' : '';
  const text = parts.join(', ');
  return live ? `Em andamento: ${text}…` : `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

/** Handoffs recorded more than once for the same hop (same from, to and task) — a crash/resume bug. */
export function duplicateHandoffs(handoffs = []) {
  const seen = new Map();
  for (const item of handoffs || []) {
    const key = `${item.from_agent}→${item.to_agent}|${item.task || item.reason || ''}`;
    seen.set(key, (seen.get(key) || 0) + 1);
  }
  return [...seen.entries()].filter(([, count]) => count > 1).map(([key, count]) => ({ key, count }));
}

/** Change-stream event that means "a human resolved the paused write; the turn resumed". */
export function isHumanResume(event) {
  return Boolean(event && event.from_agent === HUMAN && event.to_agent);
}

/**
 * Fit a label inside a node: estimated width = chars × fontSize × charEm (Source Code Pro advances
 * 0.6em per glyph; Special Gothic averages below 0.6em, so 0.6 is a safe upper bound for both).
 * Returns the text, cut with an ellipsis when it would cross `maxWidth` (viewBox units). The SVG
 * scales text and nodes together, so a fit in viewBox units holds at every viewport width.
 */
export function fitText(text, maxWidth, fontSize, charEm = 0.6) {
  const value = String(text || '');
  const capacity = Math.max(1, Math.floor(maxWidth / (fontSize * charEm)));
  if (value.length <= capacity) return value;
  return `${value.slice(0, Math.max(1, capacity - 1))}…`;
}

/** Estimated rendered width of `text` (same model as fitText). */
export function textWidth(text, fontSize, charEm = 0.6) {
  return String(text || '').length * fontSize * charEm;
}

/** Second line of a specialist node: a compact status, never the raw tool list (that goes in the tooltip). */
export function nodeSubtitle({ active = true, paused = false, hub = false, isActive = false, visited = false,
  tools = [], consolidated = false, planLength = 0 } = {}) {
  if (!active) return 'desligado';
  if (paused) return 'aguarda analista';
  if (hub) return consolidated ? 'consolidou' : planLength ? `plano: ${planLength} tarefa${planLength === 1 ? '' : 's'}` : 'roteia e consolida';
  if (isActive) return 'em ação';
  if (tools.length) return `${tools.length} tool${tools.length === 1 ? '' : 's'}`;
  if (visited) return 'participou';
  return '';
}
