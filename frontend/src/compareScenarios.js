// Scenarios for the A/B page (POST /api/compare). Messages come from the identity's versioned demo
// scenarios (GET /api/demo-scenarios) when one exists for the slot; otherwise a fixed message that
// only uses ids this identity owns (seed: PED-n001 and FAT-n001 exist for every identity n).
// Pure module: no React, tested with node --test.

const IDENTITY = {
  ana: { n: 1, warranty: { order: 'PED-1002', product: 'Teclado Air', kind: 'teclado' } },
  bruno: { n: 2, warranty: { order: 'PED-2001', product: 'Monitor View 27', kind: 'monitor' } },
  carla: { n: 3, warranty: { order: 'PED-3002', product: 'Carregador Duo', kind: 'carregador' } },
  diego: { n: 4, warranty: { order: 'PED-4001', product: 'Caixa Sonora Mini', kind: 'caixa de som' } },
};

// Slot → demo scenario ids that fit it (first one present for the identity wins).
const SLOTS = [
  {
    id: 'simples', title: 'Pergunta simples',
    tests: 'Um especialista basta. Tende a favorecer o agente único: menos chamadas e menos latência.',
    demoIds: [],
    fixed: ({ n }) => `onde está meu pedido PED-${n}001?`,
  },
  {
    id: 'pedido-fatura', title: 'Pedido + fatura',
    tests: 'Duas perguntas independentes. Mede se dividir o trabalho ajuda quando as partes não dependem uma da outra.',
    demoIds: (key) => [`${key}-parallel-fanout`, `${key}-parallel-paid`],
    fixed: ({ n }) => `onde está meu pedido PED-${n}001 e qual é o valor e o vencimento da fatura FAT-${n}001?`,
  },
  {
    id: 'composto', title: 'Pedido composto (principal)',
    tests: 'Vários especialistas em sequência, com escrita no meio. É onde o time costuma ganhar em completude.',
    demoIds: (key) => [`${key}-controlled-return`, `${key}-order-return`, `${key}-warranty-trade-billing`, `${key}-support-trade-billing`],
    fixed: ({ n }) => `quero trocar o pedido PED-${n}001; depois consulte a entrega e volte para confirmar que a troca ficou registrada.`,
  },
  {
    id: 'garantia-catalogo', title: 'Garantia → catálogo',
    tests: 'Política de garantia e depois busca vetorial no catálogo, sem escrita: duas áreas que precisam conversar.',
    demoIds: (key) => [`${key}-warranty-catalog`],
    fixed: ({ warranty }) => `o ${warranty.product} do pedido ${warranty.order} ainda está na garantia? Quero comparar com um ${warranty.kind} similar; apenas compare as opções.`,
  },
  {
    id: 'reembolso', title: 'Reembolso com aprovação',
    tests: 'Escrita que a política HITL-REEMBOLSO manda para um analista. No A/B a escrita é simulada: a política vale e vira uma pausa simulada, sem gravar nada.',
    demoIds: (key) => [`${key}-refund-return`],
    fixed: ({ n }) => `solicite o reembolso do pedido PED-${n}001, consulte a entrega e volte para confirmar que o reembolso ficou registrado.`,
  },
];

export const FREE_SCENARIO = 'livre';

export function buildCompareScenarios(customerKey, demoScenarios = []) {
  const identity = IDENTITY[customerKey] || IDENTITY.ana;
  const byId = new Map((demoScenarios || []).filter((item) => item?.scenario_id && item?.message).map((item) => [item.scenario_id, item]));
  return SLOTS.map((slot) => {
    const ids = typeof slot.demoIds === 'function' ? slot.demoIds(customerKey || 'ana') : slot.demoIds;
    const demo = ids.map((id) => byId.get(id)).find(Boolean);
    return {
      id: slot.id, title: slot.title, tests: slot.tests,
      message: demo ? demo.message : slot.fixed(identity),
      source: demo ? 'roteiro' : 'fixo', scenarioId: demo?.scenario_id || null,
    };
  });
}

/**
 * How long to wait before the next A/B call so the backend's sliding window (3 per 60 s per
 * customer) is never exceeded. `history` holds the start time (ms) of previous calls.
 */
export function compareWaitMs(history = [], now = Date.now(), { limit = 3, windowMs = 60000, marginMs = 1500 } = {}) {
  const recent = history.filter((at) => now - at < windowMs).sort((a, b) => a - b);
  if (recent.length < limit) return 0;
  return Math.max(0, recent[recent.length - limit] + windowMs + marginMs - now);
}

/** Row of the "run all" summary: latency, cost and judge per arm, and who did better on each. */
export function summarizeCompare(result) {
  const arm = (data) => {
    if (!data || data.available === false) return null;
    const judge = data.judge?.available ? Number(data.judge.media) : null;
    return { latencyMs: data.latency_ms ?? null, cost: data.cost_usd ?? null, judge: Number.isFinite(judge) ? judge : null };
  };
  const single = arm(result?.single);
  const multi = arm(result?.multi);
  const winner = (key, lowerIsBetter) => {
    const a = single?.[key];
    const b = multi?.[key];
    if (typeof a !== 'number' || typeof b !== 'number' || a === b) return null;
    return (lowerIsBetter ? a < b : a > b) ? 'single' : 'multi';
  };
  return { single, multi, faster: winner('latencyMs', true), cheaper: winner('cost', true), betterJudge: winner('judge', false) };
}

export const isRateLimited = (message = '') => /limite do A\/B|3 por minuto|HTTP 429/i.test(message);
