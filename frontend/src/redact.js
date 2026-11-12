// Mask credentials and personal identifiers before showing a query/filter on screen.
const SENSITIVE = /(?:authorization|password|secret|token|uri|cpf|cnpj|email|keymaterial)/i;

export function redact(value, key = '') {
  if (SENSITIVE.test(key)) return '<mascarado>';
  if (Array.isArray(value)) {
    // Embedding vectors are noise on screen: show their size, not the floats.
    if (value.length > 16 && value.every((item) => typeof item === 'number')) return `<${value.length} floats omitidos>`;
    return value.map((item) => redact(item));
  }
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, redact(v, k)]));
  return value;
}
