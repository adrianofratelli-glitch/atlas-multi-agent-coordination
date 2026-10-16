// "Por que este artigo": normaliza o result do evento hybridSearch do support_agent.
// O formato do scoreDetails não é garantido pela doc do $rankFusion: tudo que não bater vira
// "detalhe indisponível", nunca exceção.
const LEG = { vector: 'semântica', lexical: 'lexical' };
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

export function explainArticles(event) {
  if (!event || event.op !== 'hybridSearch' || !Array.isArray(event.result)) return null;
  const rows = event.result.filter((a) => a && typeof a === 'object').map((a) => {
    const legs = Array.isArray(a.por_que?.legs) ? a.por_que.legs.filter((l) => l && typeof l.name === 'string') : [];
    const total = legs.reduce((s, l) => s + Math.max(0, num(l.contribution) ?? 0), 0);
    return {
      id: String(a.article_id ?? ''),
      title: String(a.title ?? ''),
      rerank: num(a.rerank_score),
      available: legs.length > 0,
      legs: legs.map((l) => ({
        name: LEG[l.name] ?? l.name,
        rank: Number.isInteger(l.rank) && l.rank > 0 ? l.rank : null,
        share: total > 0 ? Math.max(0, num(l.contribution) ?? 0) / total : 0,
      })),
    };
  });
  if (!rows.length) return null;
  return { strategy: String(event.filter?.strategy ?? ''), fallbacks: (event.filter?.fallbacks ?? []).map(String).slice(0, 3), rows };
}
