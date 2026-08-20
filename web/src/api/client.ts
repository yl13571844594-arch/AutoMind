// REST 客户端 — 统一 JSON fetch，403 商业功能门控在调用侧按需处理。
const API = '/api';

/**
 * 带上下文的接口错误。
 *
 * 此前 `apiGet` 只是 `return r.json()`：HTTP 500/502、鉴权 401、后端返回的
 * `{"error": "..."}`，统统被当成正常数据往下传；而调用方普遍写着
 * `.catch(() => {})`，于是**"加载失败"被渲染成"没有数据"** ——
 * 用户看到的是"暂无记录"，实际是服务挂了或令牌过期。
 */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public path: string,
    /** 原始响应体（若能解析），便于调用方读取 feature 等附加字段 */
    public body?: any,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  /** 面向用户的一句话说明（含可操作建议）。 */
  get friendly(): string {
    if (this.status === 0) return '无法连接到服务，请确认 AutoMind 服务仍在运行。';
    if (this.status === 401 || this.status === 403) {
      return this.message || '没有访问权限：请检查访问令牌，或该功能需要更高版本。';
    }
    if (this.status === 404) return '接口不存在（可能是前后端版本不一致，试试强制刷新）。';
    if (this.status >= 500) return `服务端错误（${this.status}）：${this.message || '请查看服务端日志'}`;
    return this.message || `请求失败（${this.status}）`;
  }
}

/** 解析响应体；非 2xx、非 JSON、或体内带 error 字段都转成 ApiError 抛出。 */
async function parse<T>(r: Response, path: string): Promise<T> {
  let data: any;
  try {
    data = await r.json();
  } catch {
    if (!r.ok) throw new ApiError(r.status, r.statusText || '响应不是合法 JSON', path);
    throw new ApiError(r.status, '响应不是合法 JSON', path);
  }
  if (!r.ok) {
    // 403 + feature 字段不是"失败"，而是**版本门控的正常答复**：
    // 社区版访问专业版接口本就该拿到这个，界面据 `feature` 渲染升级提示。
    // 若把它当错误抛出，统计/知识库/专家等页面会卡在加载态转圈。
    if (r.status === 403 && data && typeof data.feature === 'string') {
      return data as T;
    }
    throw new ApiError(
      r.status, (data && (data.error || data.detail)) || r.statusText, path, data);
  }
  return data as T;
}

export async function apiGet<T = any>(path: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${API}${path}`);
  } catch (e: any) {
    // fetch 只在网络层失败时 reject —— 服务没起来 / 断网 / 被代理拦截
    throw new ApiError(0, e?.message || '网络请求失败', path);
  }
  return parse<T>(r, path);
}

export async function apiPost<T = any>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  const ctrl = timeoutMs ? new AbortController() : undefined;
  const timer = ctrl ? window.setTimeout(() => ctrl.abort(), timeoutMs) : undefined;
  try {
    const r = await fetch(`${API}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl?.signal,
    });
    return r.json();
  } finally {
    if (timer) window.clearTimeout(timer);
  }
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
