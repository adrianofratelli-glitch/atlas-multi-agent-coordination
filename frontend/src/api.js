// O prazo cobre headers e corpo; nenhuma escrita é reenviada automaticamente.
async function boundedRequest(work, timeoutMs = 30000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try { return await work(controller.signal) }
  finally { clearTimeout(timer) }
}

const BASE = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8031';

let token = localStorage.getItem('multi-agent-token') || '';

async function request(path, options = {}) {
  return boundedRequest(async (signal) => {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    if (token) headers.Authorization = `Bearer ${token}`;
    let response;
    try {
      response = await fetch(`${BASE}${path}`, { ...options, headers, signal });
    } catch {
      throw new Error('Backend indisponível. Confirme que a API está ativa na porta 8031 e tente novamente.');
    }
    const data = await response.json().catch(() => {
      if (response.ok) throw new Error('Resposta incompleta ou inválida do backend. Tente novamente.')
      return {}
    });
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }, path === '/api/chat' ? 300000 : 30000)
}

export const api = {
  async login(customerKey = 'ana') {
    const data = await request('/api/auth/token', { method: 'POST', body: JSON.stringify({ customer_key: customerKey }) });
    token = data.access_token;
    localStorage.setItem('multi-agent-token', token);
    return data.customer;
  },
  health: () => request('/api/health'),
  // Desfaz o que a demo gravou NESTE cliente (fatos, episódios, curto prazo, cache do cliente) para repetir o roteiro.
  demoReset: () => request('/api/demo/reset', { method: 'POST' }),
  // Aquece o cache no servidor (segundo plano, com cooldown): a demo abre já quente, sem script manual.
  warmup: () => request('/api/warmup', { method: 'POST' }).catch(() => null),
  agents: () => request('/api/agents'),
  demoScenarios: () => request('/api/demo-scenarios'),
  metrics: () => request('/api/metrics'),
  handoffs: (id) => request(`/api/handoffs?conversation_id=${encodeURIComponent(id)}`),
  memory: (key) => request(`/api/memory/${encodeURIComponent(key)}`),
  // conversationId só importa para a visão 'short' (memória por sessão); as demais ignoram
  inspector: (view, conversationId) => request(
    `/api/inspector/${encodeURIComponent(view)}`
    + (conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : '')
  ),
  latestConversation: () => request('/api/conversations/latest'),
  guardrails: (view = 'events') => request(`/api/guardrails/${view}`, { headers: { 'X-Admin-Key': import.meta.env.VITE_ADMIN_KEY || '' } }),
  chat: (message, conversationId) => request('/api/chat', { method: 'POST', body: JSON.stringify({ message, conversation_id: conversationId || null }) }),
  updateAgent: (key, update) => request(`/api/admin/agents/${key}`, { method: 'PATCH', headers: { 'X-Admin-Key': import.meta.env.VITE_ADMIN_KEY || '' }, body: JSON.stringify(update) }),
  evalRuns: () => request('/api/eval/runs', { headers: { 'X-Admin-Key': import.meta.env.VITE_ADMIN_KEY || '' } }),
  // Trilha de conformidade do próprio chamador: a customer_key vem do JWT, nunca daqui.
  decisions: (subjectId) => request('/api/decisions' + (subjectId ? `?subject_id=${encodeURIComponent(subjectId)}` : '')),
  myReviews: (status = 'pending') => request(`/api/reviews?status=${encodeURIComponent(status)}`),
  adminReviews: (status = 'pending') => request(`/api/admin/reviews?status=${encodeURIComponent(status)}`, { headers: { 'X-Admin-Key': import.meta.env.VITE_ADMIN_KEY || '' } }),
  resolveReview: (reviewId, body) => request(`/api/admin/reviews/${encodeURIComponent(reviewId)}/resolve`, {
    method: 'POST',
    headers: { 'X-Admin-Key': import.meta.env.VITE_ADMIN_KEY || '' },
    body: JSON.stringify(body),
  }),
  async streamEvents(onEvent, signal, onOpen) {
    const response = await fetch(`${BASE}/api/events/stream`, { headers: { Authorization: `Bearer ${token}` }, signal });
    if (!response.ok) {
      const data = await response.json().catch(() => {
    if (response.ok) throw new Error('Resposta incompleta ou inválida do backend. Tente novamente.')
    return {}
  });
      throw new Error(data.detail || `Feed ao vivo indisponível (HTTP ${response.status})`);
    }
    if (!response.body) throw new Error('Feed ao vivo sem corpo de resposta');
    onOpen?.();
    const reader = response.body.getReader();
    try {
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');
      const chunks = buffer.split('\n\n');
      buffer = chunks.pop();
      for (const chunk of chunks) {
        if (chunk.startsWith('data: ')) {
          try { onEvent(JSON.parse(chunk.slice(6))); } catch { /* linha incompleta, ignora */ }
        }
      }
    }
    } finally {
      reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  },
};
