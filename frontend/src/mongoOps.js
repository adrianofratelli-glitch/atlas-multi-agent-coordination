// "Por baixo do capô": one row per MongoDB (or external) operation in the turn, with the real
// filter the backend recorded and tags for the Atlas capabilities worth pointing at on stage.
import { redact } from './redact.js';
import { BUSINESS_COLLECTIONS } from './graphModel.js';

const OP_LABELS = {
  read: 'find', write: 'escrita', vectorSearch: '$vectorSearch', hybridSearch: 'busca híbrida',
  changeStream: 'change stream', graphLookup: '$graphLookup', external: 'chamada externa',
};

export function opTags(event) {
  const title = event.title || '';
  const tags = [];
  if (event.op === 'vectorSearch') tags.push('$vectorSearch');
  if (event.op === 'hybridSearch') tags.push(/rankFusion/i.test(title) ? '$rankFusion' : 'BM25 + vetor');
  if (/rerank/i.test(title)) tags.push('$rerank');
  if (event.op === 'graphLookup') tags.push('$graphLookup');
  if (event.op === 'changeStream') tags.push('change stream');
  if (/transa[cç][aã]o|supersess|at[oô]mic/i.test(title)) tags.push('transação');
  if (event.collection === 'turn_blackboard') tags.push('quadro do turno');
  if (event.collection === 'agent_handoffs' && event.op === 'write') tags.push('dispara change stream');
  if (event.collection === 'pending_reviews' && event.op === 'write') tags.push('interrupt no checkpoint');
  if (event.op === 'write' && BUSINESS_COLLECTIONS.has(event.collection)) tags.push('escrita de negócio');
  if (event.op === 'external') tags.push('fora do MongoDB');
  return tags;
}

export function compactJson(value, max = 180) {
  if (value === undefined || value === null) return '';
  let text;
  try { text = JSON.stringify(redact(value)); } catch { text = String(value); }
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Operations in timeline order. Events without a collection (pure narration) are skipped. */
export function mongoOps(events = []) {
  const rows = [];
  (events || []).forEach((event, index) => {
    if (!event || (!event.collection && event.op !== 'external')) return;
    const external = event.op === 'external';
    rows.push({
      index,
      agent: event.agent || null,
      collection: external ? (event.filter?.provider || 'web') : event.collection,
      op: event.op || 'read',
      opLabel: OP_LABELS[event.op] || event.op || 'operação',
      filter: event.filter || null,
      filterText: compactJson(event.filter),
      tags: opTags(event),
      title: event.title,
      replayed: Boolean(event.replayed),
      external,
      write: event.op === 'write',
    });
  });
  return rows;
}

/** Counts for the feed header: total ops, writes, and how many hit native Atlas capabilities. */
export function summarizeOps(rows = []) {
  const native = new Set(['$vectorSearch', '$rankFusion', '$rerank', '$graphLookup', 'BM25 + vetor', 'change stream', 'transação']);
  return {
    total: rows.filter((row) => !row.external).length,
    writes: rows.filter((row) => row.write && !row.replayed).length,
    native: rows.filter((row) => row.tags.some((tag) => native.has(tag))).length,
    collections: new Set(rows.filter((row) => !row.external).map((row) => row.collection)).size,
  };
}

// Plain-language name of the timeline category, for rows that are not a MongoDB operation.
export const CATEGORY_LABELS = {
  agent: 'agente', memory: 'memória', guardrail: 'guardrail', cache: 'cache', handoff: 'coordenação',
  coordination: 'coordenação', fanout: 'paralelo', web: 'fonte externa',
};

/**
 * Every event of the turn, in timeline order, for the capô: MongoDB operations (kind `op`), the
 * external web search (kind `external`, never labelled as MongoDB) and the remaining events with no
 * collection (kind `event`: guardrail of output, coordination, handoff without write...), so nothing
 * the old "Execução" tab showed is lost. Each row keeps `event` for the expandable detail.
 */
export function hoodRows(events = []) {
  const ops = new Map(mongoOps(events).map((row) => [row.index, row]));
  const rows = [];
  (events || []).forEach((event, index) => {
    if (!event) return;
    const op = ops.get(index);
    if (op) {
      rows.push({ ...op, kind: op.external ? 'external' : 'op', category: event.category || null, event });
      return;
    }
    rows.push({
      index,
      kind: 'event',
      agent: event.agent || null,
      category: event.category || null,
      categoryLabel: CATEGORY_LABELS[event.category] || event.category || 'evento',
      title: event.title || 'evento do turno',
      replayed: Boolean(event.replayed),
      external: false,
      write: false,
      tags: [],
      event,
    });
  });
  return rows;
}

/** Indices of the capô rows that belong to a narrative step (in row order). */
export function rowsForStep(step, rows = []) {
  const wanted = new Set(step?.events || []);
  return rows.filter((row) => wanted.has(row.index)).map((row) => row.index);
}
