// REST 客户端 — 统一 JSON fetch，403 商业功能门控在调用侧按需处理。
const API = '/api';

export async function apiGet<T = any>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`);
  return r.json();
}

export async function apiPost<T = any>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return r.json();
}

export async function apiDelete<T = any>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`, { method: 'DELETE' });
  return r.json().catch(() => ({}));
}

// 会话 ID：每个浏览器独立（多用户会话隔离），持久化于 localStorage
export function getSessionId(): string {
  let s = localStorage.getItem('automind_sid');
  if (!s) {
    s = 's_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    localStorage.setItem('automind_sid', s);
  }
  return s;
}
export const SID = getSessionId();
