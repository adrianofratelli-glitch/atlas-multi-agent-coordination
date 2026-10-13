// Markdown mínimo das respostas do LLM (negrito e listas) em blocos estruturados — sem HTML cru,
// sem dangerouslySetInnerHTML: o React escapa todo o texto. O resto do Markdown vira texto limpo.

const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

// Link: só http(s), como segmento com `href` (o renderer cria o <a> pelo React; nunca innerHTML). Cobre o
// Markdown "[rótulo](url)" e a URL solta que a resposta com fonte web cita; pontuação final fica fora do link.
const SAFE_URL = /^https?:\/\/[^\s<>"']+$/;
const TRAILING = /[.,;:!?)\]]+$/;

export function inline(text) {
  // "**x**" / "__x__" → negrito; asteriscos/sublinhados soltos ficam como texto.
  const out = [];
  const re = /\*\*(.+?)\*\*|__(.+?)__|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"']+)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    if (m[1] !== undefined || m[2] !== undefined) {
      out.push({ text: m[1] ?? m[2], bold: true });
    } else if (m[3] !== undefined) {
      out.push(SAFE_URL.test(m[4]) ? { text: m[3], href: m[4] } : { text: m[0] });
    } else {
      const tail = (m[5].match(TRAILING) || [''])[0];
      const url = tail ? m[5].slice(0, -tail.length) : m[5];
      out.push(SAFE_URL.test(url) ? { text: url, href: url } : { text: url });
      if (tail) out.push({ text: tail });
    }
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
