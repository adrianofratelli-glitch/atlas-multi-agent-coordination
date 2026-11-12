// Ícones mínimos em SVG (sem dependência): cor vem de currentColor.
export function LockIcon({ size = 14, title }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden={title ? undefined : 'true'} role={title ? 'img' : undefined} focusable="false">
      {title && <title>{title}</title>}
      <rect x="3" y="7" width="10" height="7.5" rx="1.6" fill="currentColor" />
      <path d="M5.2 7V5.2a2.8 2.8 0 0 1 5.6 0V7" fill="none" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

export function PenIcon({ size = 12 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M2.5 13.5l.7-3.1 7.6-7.6a1.4 1.4 0 0 1 2 0l.4.4a1.4 1.4 0 0 1 0 2l-7.6 7.6z" fill="currentColor" />
    </svg>
  );
}
