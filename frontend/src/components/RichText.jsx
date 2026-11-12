import { blocks } from '../richtext.js';

function Segments({ segments }) {
  return segments.map((seg, i) => {
    if (seg.href) return <a key={i} href={seg.href} target="_blank" rel="noopener noreferrer">{seg.text}</a>;
    return seg.bold ? <strong key={i}>{seg.text}</strong> : <span key={i}>{seg.text}</span>;
  });
}

// Resposta do modelo: negrito e listas renderizados, sem HTML cru (ver richtext.js).
export default function RichText({ text }) {
  return blocks(text || '').map((block, i) => {
    if (block.type === 'list') {
      return <ul className="rich-list" key={i}>{block.items.map((item, j) => <li key={j}><Segments segments={item} /></li>)}</ul>;
    }
    if (block.type === 'gap') return <span className="rich-gap" key={i} aria-hidden="true" />;
    return <span className="rich-line" key={i}><Segments segments={block.segments} /></span>;
  });
}
