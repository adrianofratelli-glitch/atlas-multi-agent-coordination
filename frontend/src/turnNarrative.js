// Plain-language narrative of one turn ("Como o time resolveu"), derived only from the timeline
// events the backend emitted (SSE while live, run.timeline when finished). No React here, so the
// mapping is testable with `node --test` and never invents a step the backend did not record.
import { HUB } from './graphModel.js';
import { opTags } from './mongoOps.js';

// What each tool does, in words a customer audience understands. Collections with the same
// effect reuse the same sentence so a tool event and its collection event collapse into one.
const TOOL_VERBS = {
  get_order: 'consultou o pedido',
  request_order_status_change: 'registrou a mudança de status do pedido',
  get_invoice: 'consultou a fatura',
  check_warranty: 'verificou a garantia',
  get_loyalty_account: 'consultou o saldo de pontos',
  redeem_reward: 'registrou o resgate de pontos',
  get_shipment: 'consultou rastreio e prazo da entrega',
  request_delivery_reschedule: 'registrou o pedido de reagendamento',
  search_carrier_news: 'buscou notícias da transportadora na web',
  search_catalog: 'buscou opções parecidas no catálogo',
  search_knowledge_base: 'consultou a base de conhecimento',
  open_support_ticket: 'abriu um chamado para atendente',
};
const WRITE_TOOLS = new Set(['request_order_status_change', 'redeem_reward', 'request_delivery_reschedule', 'open_support_ticket']);
const COLLECTION_VERBS = {
  orders: ['consultou o pedido', 'registrou a mudança de status do pedido'],
  invoices: ['consultou a fatura'],
  warranty_policies: ['verificou a garantia'],
  products_catalog: ['buscou opções parecidas no catálogo'],
  kb_articles: ['consultou a base de conhecimento'],
  shipments: ['consultou rastreio e prazo da entrega', 'registrou o pedido de reagendamento'],
  loyalty_accounts: ['consultou o saldo de pontos', 'registrou o resgate de pontos'],
  redemptions: ['conferiu resgates recentes', 'registrou o resgate de pontos'],
  support_tickets: ['conferiu chamados abertos', 'abriu um chamado para atendente'],
  customer_memory: ['usou o orçamento guardado na memória como filtro'],
};

export const WHY = {
  guardrail: 'Fraude, jailbreak e pedido de dados de outro cliente param aqui, antes de gastar token com especialista.',
  blocked: 'Nenhum agente rodou: zero token de especialista, e o bloqueio fica auditado no Atlas.',
  routeRules: 'Regra de roteamento é documento no Atlas: muda sem deploy, e o LLM nunca sobrescreve uma regra confiante.',
  routeModel: 'Só quando nenhuma regra decide com confiança o orquestrador classifica a intenção.',
  cacheHit: 'Pergunta genérica já respondida: o cache semântico devolve a resposta sem rodar agente (0 token).',
  plan: 'Cada tarefa vira uma linha no quadro do turno: um agente lê o que o anterior registrou.',
  read: 'O LLM não monta a query: o servidor filtra pelo cliente do JWT e o modelo só redige sobre o documento lido.',
  write: 'Escrita só pela allowlist do servidor, e só quando o cliente pediu: o modelo nunca envia um $set livre.',
  kb: 'Busca híbrida: $rankFusion junta texto (BM25) e vetor numa única consulta no Atlas.',
  catalog: '$vectorSearch com pré-filtro: orçamento e estoque são filtro do servidor, não pedido ao modelo.',
  handoff: 'Cada passagem é gravada em agent_handoffs e chega à tela pelo Change Stream.',
  fanout: 'Partes independentes rodam ao mesmo tempo: a latência é a do agente mais lento, não a soma.',
  pause: 'A pausa mora no checkpoint do Atlas, não numa conexão aberta: nada foi alterado até o analista decidir.',
  resume: 'O mesmo thread retoma do checkpoint: a escrita aprovada roda uma única vez.',
  consolidate: 'O cliente recebe uma resposta só e coerente, em vez de várias respostas soltas.',
  memory: 'O servidor governa a memória: guarda fatos e rótulos (intenção + agente), nunca o texto cru.',
  guardrailOut: 'A resposta passa por um filtro antes de chegar ao cliente.',
  scope: 'Fora do domínio da loja: 0 token, nenhum agente e nada vai para o cache.',
  supervisor: 'O supervisor limita saltos e laços: uma falha fica contida e o turno ainda responde.',
};

const NOTE_TITLE = /aprova[cç][aã]o humana|adiad|bloquead|paus[aá]vel|recusad|degradad|idempot|negad|não efetivad|fora da tabela/i;

const isTitle = (event, prefix) => typeof event?.title === 'string' && event.title.startsWith(prefix);

function joinPt(items) {
  if (items.length <= 1) return items[0] || '';
  return `${items.slice(0, -1).join(', ')} e ${items[items.length - 1]}`;
}

function argsText(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args).map(([key, value]) => `${key} = ${typeof value === 'object' ? JSON.stringify(value) : value}`).join(', ');
}

function mongoRef(event, index) {
  if (!event?.collection && event?.op !== 'external') return null;
  const tags = opTags(event).filter((tag) => tag !== 'escrita de negócio');
  return {
    index,
    collection: event.op === 'external' ? (event.filter?.provider || 'web') : event.collection,
    tag: tags[0] || (event.op === 'write' ? 'escrita' : event.op === 'external' ? 'externa' : null),
    write: event.op === 'write',
  };
}

/**
 * Steps of the turn, in the order things happened.
 * Each step: { id, kind, title, agent?, text, why, items?, next?, infos?, notes[], mongo[], events[], status, focus }
 * - events: indices (in `events`) of every timeline event that fed this step, to link the capô rows
 * - infos: neutral context (e.g. cache miss); notes: things worth a warning colour (held/refused writes)
 * - status: ok | write | blocked | paused | degraded | refused
 * - focus: { nodes: [agent keys], edges: [{ from, to }] } for highlighting the graph
 */
export function narrateTurn(events = [], { labels = {}, live = false } = {}) {
  const name = (key) => labels[key] || key || 'agente';
  const steps = [];
  const incoming = {};
  let pendingRefs = [];

  // Which step each event fed (owner of the current event), so the capô can link back to the
  // narrative. Events that arrive before their step exists (e.g. "Quadro do turno aberto") wait.
  let pendingEvents = [];
  let owner = null;
  const PENDING = Symbol('pending');

  const push = (step) => {
    const full = { notes: [], mongo: [], events: [], status: 'ok', focus: { nodes: [], edges: [] }, ...step };
    full.id = `${full.kind}-${steps.length}`;
    if (pendingRefs.length) { full.mongo = [...pendingRefs, ...full.mongo]; pendingRefs = []; }
    if (pendingEvents.length) { full.events = [...pendingEvents]; pendingEvents = []; }
    steps.push(full);
    owner = full;
    return full;
  };
  const addRef = (step, event, index) => {
    owner = step || PENDING;
    const ref = mongoRef(event, index);
    if (!ref) return;
    if (!step) { pendingRefs.push(ref); return; }
    if (!step.mongo.some((item) => item.collection === ref.collection && item.tag === ref.tag)) step.mongo.push(ref);
  };
  const last = () => steps[steps.length - 1];
  const lastOfKind = (kind) => [...steps].reverse().find((step) => step.kind === kind);
  const agentStep = (agent) => {
    const current = last();
    if (current?.kind === 'agent' && current.agent === agent && !current.next) { owner = current; return current; }
    const revisit = steps.some((step) => step.kind === 'agent' && step.agent === agent);
    return push({ kind: 'agent', agent, title: revisit ? `${name(agent)} (retorno)` : name(agent), verbs: [], writes: 0, task: null,
      focus: { nodes: [agent], edges: incoming[agent] ? [incoming[agent]] : [] } });
  };
  const addVerb = (step, verb, isWrite) => {
    if (!verb || step.verbs.includes(verb)) return;
    step.verbs.push(verb);
    if (isWrite) step.writes += 1;
  };

  const handle = (event, index) => {
    if (!event || event.replayed) return;
    const result = event.result && typeof event.result === 'object' ? event.result : {};
    const title = event.title || '';

    if (event.category === 'guardrail') {
      if (isTitle(event, 'Guardrail de entrada')) {
        const step = push({ kind: 'guardrail', title: 'Guardrail de entrada', focus: { nodes: [HUB], edges: [] } });
        if (result.blocked) {
          step.status = 'blocked';
          step.text = `A mensagem foi bloqueada antes de chegar a qualquer agente${result.reason ? ` (motivo: ${result.reason})` : ''}.`;
          step.why = WHY.blocked;
        } else if (result.uncertain) {
          step.text = 'O modelo ficou em dúvida sobre a mensagem: o turno seguiu e o caso foi para a fila de revisão.';
          step.why = WHY.guardrail;
        } else {
          step.text = 'A mensagem passou: nenhum sinal de fraude, jailbreak ou pedido de dados de outro cliente.';
          step.why = WHY.guardrail;
        }
        addRef(step, event, index);
      } else if (isTitle(event, 'Guardrail de saída')) {
        const step = push({ kind: 'guardrail_out', title: 'Guardrail de saída', focus: { nodes: [HUB], edges: [] } });
        step.status = result.blocked ? 'blocked' : 'ok';
        step.text = result.blocked ? 'O filtro de saída segurou a resposta antes de ela chegar ao cliente.' : 'A resposta final passou pelo filtro de saída e foi entregue ao cliente.';
        step.why = WHY.guardrailOut;
      } else if (isTitle(event, 'Guardrail de escopo')) {
        push({ kind: 'scope', title: 'Fora do escopo da loja', focus: { nodes: [HUB], edges: [] },
          text: 'A pergunta não é sobre pedido, produto, entrega ou cobrança: o orquestrador respondeu com o que existe para este cliente.',
          why: WHY.scope });
      } else if (isTitle(event, 'Escrita recusada') && event.agent) {
        const step = agentStep(event.agent);
        step.status = 'refused';
        step.notes.push(`O servidor recusou a escrita ${result.tool || ''}: o cliente não pediu isso explicitamente.`.replace('  ', ' '));
      }
      return;
    }

    if (event.category === 'cache') {
      if (result.hit) {
        const step = push({ kind: 'cache', title: 'Cache semântico', focus: { nodes: [HUB], edges: [] },
          text: 'A mesma pergunta já tinha resposta reaproveitável: nenhum agente precisou rodar.', why: WHY.cacheHit });
        addRef(step, event, index);
      } else {
        const route = last()?.kind === 'route' ? last() : null;
        const note = result.personal
          ? 'Pergunta pessoal: o cache compartilhado foi pulado de propósito.'
          : 'O cache semântico não tinha resposta reaproveitável, então os especialistas rodaram.';
        if (route) { route.infos = [...(route.infos || []), note]; addRef(route, event, index); }
      }
      return;
    }

    if (event.category === 'fanout') {
      const agents = result.agents || [];
      agents.forEach((agent) => { incoming[agent] = { from: HUB, to: agent }; });
      const step = push({ kind: 'fanout', title: 'Despacho paralelo', status: 'ok',
        text: `As partes não dependem uma da outra: ${joinPt(agents.map(name))} rodaram ao mesmo tempo.`,
        why: WHY.fanout, focus: { nodes: [HUB, ...agents], edges: agents.map((agent) => ({ from: HUB, to: agent })) } });
      addRef(step, event, index);
      return;
    }

    if (event.category === 'handoff' && result.to_agent && event.agent) {
      const from = event.agent;
      const to = result.to_agent;
      const step = agentStep(from);
      const degraded = /degradado/i.test(title);
      step.next = { agent: to, task: result.task || null, revisit: Boolean(result.revisit), degraded };
      step.focus = { ...step.focus, nodes: [...new Set([...step.focus.nodes, to])], edges: [...step.focus.edges, { from, to }] };
      if (degraded) step.status = 'degraded';
      incoming[to] = { from, to };
      addRef(step, event, index);
      return;
    }

    if (event.category === 'coordination') {
      if (Array.isArray(result.plan)) {
        const plan = result.plan.map((item) => ({ agent: item.agent, task: item.task }));
        const step = push({ kind: 'plan', title: 'Plano do orquestrador',
          text: `O orquestrador dividiu o pedido em ${plan.length} tarefa${plan.length === 1 ? '' : 's'}, cada uma com o especialista certo:`,
          items: plan.map((item) => ({ agent: item.agent, label: name(item.agent), task: item.task })),
          why: WHY.plan, focus: { nodes: [HUB, ...plan.map((item) => item.agent)], edges: [] } });
        addRef(step, event, index);
      } else if (/consolid/i.test(title)) {
        const agents = [...new Set(result.agents || steps.filter((step) => step.kind === 'agent').map((step) => step.agent))];
        const viaModel = /consolidou/i.test(title) || result.mode === 'llm';
        const step = push({ kind: 'consolidate', title: 'Consolidação',
          text: viaModel
            ? `O orquestrador juntou o que ${agents.length > 1 ? `os ${agents.length} especialistas` : 'o especialista'} registr${agents.length > 1 ? 'aram' : 'ou'} no quadro numa resposta única.`
            : 'As partes foram juntadas sem modelo (um especialista só ou modelo indisponível), na ordem do quadro.',
          why: WHY.consolidate,
          focus: { nodes: [HUB, ...agents], edges: agents.length ? [{ from: agents[agents.length - 1], to: HUB }] : [] } });
        addRef(step, event, index);
      } else if (isTitle(event, 'Quadro do turno: entrada de')) {
        const owner = [...steps].reverse().find((step) => step.kind === 'agent' && step.agent === event.agent);
        addRef(owner || last(), event, index);
      } else {
        // "Quadro do turno aberto" is emitted before the plan: it belongs to the plan step.
        addRef(lastOfKind('plan') || null, event, index);
      }
      return;
    }

    if (event.category === 'memory') {
      if (/recuperad/i.test(title)) {
        const step = push({ kind: 'memory_in', title: 'Memória do cliente', focus: { nodes: event.agent ? [event.agent] : [], edges: [] },
          text: 'O que já se sabe sobre o cliente (fatos e atendimentos anteriores) entrou no contexto do especialista.', why: WHY.memory });
        addRef(step, event, index);
      } else if (/vi[eé]s aplicado/i.test(title) && event.agent) {
        const step = agentStep(event.agent);
        addVerb(step, COLLECTION_VERBS.customer_memory[0], false);
        addRef(step, event, index);
      } else {
        let step = lastOfKind('memory_out');
        if (!step) {
          step = push({ kind: 'memory_out', title: 'Memória gravada', parts: [], focus: { nodes: [], edges: [] }, why: WHY.memory });
        }
        if (/fato/i.test(title)) step.parts.push('extraiu um fato da conversa e o gravou, substituindo o anterior se mudou');
        else if (/epis[oó]dio/i.test(title)) step.parts.push('registrou o episódio do atendimento (só intenção e agente)');
        else step.parts.push(title.toLowerCase());
        step.text = `O servidor ${joinPt([...new Set(step.parts)])}.`;
        addRef(step, event, index);
      }
      return;
    }

    if (event.category === 'web' && event.agent) {
      const step = agentStep(event.agent);
      addVerb(step, TOOL_VERBS.search_carrier_news, false);
      addRef(step, event, index);
      return;
    }

    if (event.category !== 'agent') return;

    if (isTitle(event, 'Roteamento inicial')) {
      const target = result.target_agent || event.agent;
      incoming[target] = { from: HUB, to: target };
      const confidence = typeof result.confidence === 'number' ? ` (confiança ${result.confidence.toFixed(2).replace('.', ',')})` : '';
      const intent = event.filter?.intent ? ` de ${String(event.filter.intent).replace(/_/g, ' ')}` : '';
      let text;
      let why = WHY.routeModel;
      if (result.source === 'rules') {
        text = `Uma regra determinística reconheceu um pedido${intent} e escolheu ${name(target)}, sem chamar o modelo${confidence}.`;
        why = WHY.routeRules;
      } else if (result.source === 'orchestrator') {
        text = `Nenhuma regra decidiu com confiança: o orquestrador classificou a intenção e escolheu ${name(target)}${confidence}.`;
      } else if (result.source === 'fallback') {
        text = `Sem sinal claro de domínio, o turno começou pelo agente padrão: ${name(target)}.`;
      } else {
        text = `O roteador escolheu ${name(target)} para começar${confidence}.`;
      }
      const step = push({ kind: 'route', title: 'Roteamento', text, why, agent: target,
        focus: { nodes: [HUB, target], edges: [{ from: HUB, to: target }] } });
      addRef(step, event, index);
      return;
    }

    if (isTitle(event, 'Escrita pausada')) {
      const agent = event.agent;
      const what = TOOL_VERBS[result.tool] ? `quis executar ${result.tool}` : `quis executar ${result.tool || 'uma escrita'}`;
      const args = argsText(result.args);
      push({ kind: 'pause', title: 'Pausa para aprovação humana', agent, status: 'paused',
        text: `${name(agent)} ${what}${args ? ` (${args})` : ''}. A política ${result.policy || 'de aprovação'} exige um analista: o turno parou aqui e nada foi alterado.`,
        notes: event.reason ? [event.reason] : [], why: WHY.pause,
        focus: { nodes: [agent], edges: [] } });
      addRef(last(), event, index);
      return;
    }

    if (isTitle(event, 'Turno retomado')) {
      const approved = result.approved !== false;
      push({ kind: 'resume', title: 'Decisão do analista', agent: event.agent, status: approved ? 'write' : 'ok',
        text: approved
          ? `Um analista aprovou: a escrita ${result.tool || ''} rodou uma única vez e o turno seguiu do checkpoint.`.replace('  ', ' ')
          : 'O analista não aprovou: nada foi alterado, e o cliente recebe a explicação.',
        why: WHY.resume, focus: { nodes: [event.agent], edges: [] } });
      addRef(last(), event, index);
      return;
    }

    if (isTitle(event, 'Supervisor interrompeu')) {
      push({ kind: 'supervisor', title: 'Supervisor', status: 'degraded', text: 'O supervisor detectou um laço entre agentes e encerrou a cadeia com o que já havia.',
        why: WHY.supervisor, focus: { nodes: [HUB], edges: [] } });
      return;
    }

    if (event.agent === HUB) {
      // Greeting / thanks / out of scope: the orchestrator answers alone with the customer's real data.
      const scope = last()?.kind === 'scope' ? last() : null;
      const step = scope || push({ kind: 'orchestrator', title: 'Resposta do orquestrador', focus: { nodes: [HUB], edges: [] },
        text: `${title.split(' — ')[0]}: o orquestrador respondeu direto, com os dados reais do cliente, sem acionar especialista.`,
        why: WHY.scope });
      addRef(step, event, index);
      return;
    }

    if (!event.agent) return;
    const step = agentStep(event.agent);
    if (Array.isArray(result.tools_called)) {
      for (const tool of result.tools_called) {
        if (tool === 'transfer_to_agent') continue;
        addVerb(step, TOOL_VERBS[tool] || `usou ${tool}`, WRITE_TOOLS.has(tool) && (result.writes || []).includes(tool));
      }
      if (result.task && !step.task) step.task = result.task;
      step.tools = [...new Set([...(step.tools || []), ...result.tools_called.filter((tool) => tool !== 'transfer_to_agent')])];
    }
    const toolMatch = /^Tool ([a-z_]+)/.exec(title);
    if (toolMatch) {
      const tool = toolMatch[1];
      const simulated = result.simulated === true;
      addVerb(step, `${TOOL_VERBS[tool] || `usou ${tool}`}${simulated ? ' (simulado)' : ''}`, WRITE_TOOLS.has(tool) && !simulated);
    } else if (event.collection && COLLECTION_VERBS[event.collection]) {
      const [read, write] = COLLECTION_VERBS[event.collection];
      addVerb(step, event.op === 'write' ? (write || read) : read, event.op === 'write');
    }
    if (NOTE_TITLE.test(title) && !step.notes.includes(title)) step.notes.push(title);
    if (/degradado/i.test(title)) step.status = 'degraded';
    if (event.collection !== 'multiagent_brain.agent_registry') addRef(step, event, index);
  };

  (events || []).forEach((event, index) => {
    owner = null;
    handle(event, index);
    if (owner === PENDING) pendingEvents.push(index);
    else if (owner && !owner.events.includes(index)) owner.events.push(index);
  });

  // Final wording of agent steps (verbs are collected while events stream in).
  for (const step of steps) {
    if (step.kind !== 'agent') continue;
    step.text = step.verbs.length
      ? `${name(step.agent)} ${joinPt(step.verbs)}.`
      : `${name(step.agent)} analisou a tarefa com o que já estava no quadro do turno.`;
    if (step.writes && step.status === 'ok') step.status = 'write';
    const kb = step.verbs.includes(TOOL_VERBS.search_knowledge_base);
    const catalog = step.verbs.includes(TOOL_VERBS.search_catalog);
    step.why = step.writes ? WHY.write : kb ? WHY.kb : catalog ? WHY.catalog : WHY.read;
    if (step.next) step.why = `${step.why} ${WHY.handoff}`;
  }
  if (steps.length && live) steps[steps.length - 1].current = true;
  return steps;
}

/** Map timeline index → step that consumed it (capô row → narrative step). */
export function stepByEvent(steps = []) {
  const map = new Map();
  for (const step of steps) for (const index of step.events || []) if (!map.has(index)) map.set(index, step);
  return map;
}

/** Sentence for the "passou a vez" line under an agent step. */
export function describeNext(next, labels = {}) {
  if (!next) return '';
  const to = labels[next.agent] || next.agent;
  const verb = next.degraded ? `passou a vez (degradado) para ${to}` : next.revisit ? `voltou para ${to} para confirmar` : `passou a vez para ${to}`;
  return next.task ? `${verb}: ${next.task}` : verb;
}

/**
 * Who signs the assistant bubble. A turn that was consolidated (a "consolidou" coordination event)
 * or that went through more than one specialist is the orchestrator's answer, not the last agent's.
 */
export function answerAuthor(run = {}) {
  const timeline = Array.isArray(run?.timeline) ? run.timeline : [];
  const consolidation = timeline.find((event) => event?.category === 'coordination' && /consolidou/i.test(event.title || ''));
  const fromPath = Array.isArray(run?.agent_path) ? run.agent_path : [];
  const fromActive = (run?.active_agent || '').split('+').filter(Boolean);
  const fromEvent = Array.isArray(consolidation?.result?.agents) ? consolidation.result.agents : [];
  const agents = [...new Set([...fromPath, ...fromEvent, ...(fromPath.length ? [] : fromActive)])].filter((key) => key && key !== HUB);
  const consolidated = !run?.cache_hit && (Boolean(consolidation) || agents.length > 1);
  return { consolidated, count: consolidated ? Math.max(agents.length, 1) : 0, agent: run?.active_agent || null };
}

export function authorLabel(count) {
  return `Orquestrador · resposta consolidada de ${count} agente${count === 1 ? '' : 's'}`;
}
