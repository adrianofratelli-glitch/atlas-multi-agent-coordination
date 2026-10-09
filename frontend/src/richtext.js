// Markdown mínimo das respostas do LLM (negrito e listas) em blocos estruturados — sem HTML cru,
// sem dangerouslySetInnerHTML: o React escapa todo o texto. O resto do Markdown vira texto limpo.

const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

export function inline(text) {
  // "**x**" / "__x__" → negrito; asteriscos/sublinhados soltos ficam como texto.
  const out = [];
  const re = /\*\*(.+?)\*\*|__(.+?)__/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ text: m[1] ?? m[2], bold: true });
    last = re.lastIndex;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out.map((seg) => ({ ...seg, text: seg.text.replace(/`([^`]+)`/g, '$1') }));
}

export function blocks(text) {
  const result = [];
  let list = null;
  for (const raw of String(text ?? '').split('\n')) {
    const line = raw.replace(/^\s*#{1,6}\s+/, '');
    if (BULLET.test(line)) {
      if (!list) { list = { type: 'list', items: [] }; result.push(list); }
      list.items.push(inline(line.replace(BULLET, '')));
      continue;
    }
    list = null;
    if (line.trim() === '') { result.push({ type: 'gap' }); continue; }
    result.push({ type: 'p', segments: inline(line) });
  }
  return result;
}
