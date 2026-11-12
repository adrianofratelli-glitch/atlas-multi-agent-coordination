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

// Timeline results carry token counts ("tokens_economizados", "input_tokens"): a count is not a
// credential, so it stays visible; any other value under a sensitive key is still masked.
const TOKEN_COUNT = /(?:^|_)tokens?(?:_|$)/i;

export function redactResult(value, key = '') {
  if (typeof value === 'number' && TOKEN_COUNT.test(key)) return value;
  if (SENSITIVE.test(key)) return '<mascarado>';
  if (Array.isArray(value)) {
    if (value.length > 16 && value.every((item) => typeof item === 'number')) return `<${value.length} floats omitidos>`;
    return value.map((item) => redactResult(item));
  }
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, redactResult(v, k)]));
  return value;
}
