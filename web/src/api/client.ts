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
    if (this.status === 404) {
      // 后端对"这条记录不存在"也用 404 + {"error": "..."}，那句话比
      // "接口不存在"有用得多；只有真的路由缺失（FastAPI 的 detail=Not Found）
      // 才提示版本不一致。
      const m = (this.message || '').trim();
      return m && m.toLowerCase() !== 'not found'
        ? m : '接口不存在（可能是前后端版本不一致，试试强制刷新）。';
    }
    if (this.status === 408) return this.message || '请求超时：服务端一直没有回应。';
    if (this.status >= 500) return `服务端错误（${this.status}）：${this.message || '请查看服务端日志'}`;
    return this.message || `请求失败（${this.status}）`;
  }
}

/** 把任意异常翻译成一句能直接 toast 给用户看的话。 */
export function errText(e: unknown): string {
  if (e instanceof ApiError) return e.friendly;
  if (e instanceof Error) return e.message || '未知错误';
  return String(e ?? '未知错误');
}

/** 解析响应体；非 2xx、非 JSON、或体内带 error 字段都转成 ApiError 抛出。 */
async function parse<T>(r: Response, path: string): Promise<T> {
  // 先取文本再解析：204 / 空体是**成功**（DELETE 常见），不该被当成"非法 JSON"。
  let raw: string;
  try {
    raw = await r.text();
  } catch (e: any) {
    throw new ApiError(r.status, e?.message || '响应读取失败', path);
  }
  if (!raw.trim()) {
    if (r.ok) return {} as T;
    throw new ApiError(r.status, r.statusText || '服务端返回了空响应', path);
  }
  let data: any;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new ApiError(r.status, r.ok ? '响应不是合法 JSON' : (r.statusText || '响应不是合法 JSON'),
                       path, raw.slice(0, 300));
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

/**
 * 写操作（POST）。
 *
 * v1.6.3 之前这里是 `return r.json()` —— **写失败被完全吞掉**：
 * 500 的响应体解析出来是 `{detail: ...}`，调用方一律只看 `r.error`，
 * 于是"保存设置""启用插件""添加 MCP"失败时界面什么都不说，
 * 用户以为改好了，其实什么也没发生。现在与 apiGet 走同一套 parse()：
 * 非 2xx 一律抛 ApiError，由调用方 toast（未捕获的兜底见 installGlobalApiErrorToast）。
 */
export async function apiPost<T = any>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  const ctrl = timeoutMs ? new AbortController() : undefined;
  const timer = ctrl ? window.setTimeout(() => ctrl.abort(), timeoutMs) : undefined;
  let r: Response;
  try {
    r = await fetch(`${API}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl?.signal,
    });
  } catch (e: any) {
    // 超时（AbortError）要和断网分开说 —— 前者是"服务端太慢"，后者是"根本没连上"
    if (e?.name === 'AbortError') {
      throw new ApiError(408, `请求超时（${timeoutMs}ms 内没有响应）`, path);
    }
    throw new ApiError(0, e?.message || '网络请求失败', path);
  } finally {
    if (timer) window.clearTimeout(timer);
  }
  return parse<T>(r, path);
}

export async function apiDelete<T = any>(path: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${API}${path}`, { method: 'DELETE' });
  } catch (e: any) {
    throw new ApiError(0, e?.message || '网络请求失败', path);
  }
  return parse<T>(r, path);
}

/**
 * 全局兜底：没有被就地 try/catch 的写操作失败时，至少弹一条提示。
 *
 * 写操作遍布几十处（开关、导入、删除、保存……），逐处包 try/catch 既啰嗦又
 * 容易漏。真正不能接受的是**失败后界面一声不吭**，所以在这里统一收口：
 * 未处理的 ApiError 一律 toast。就地已经处理过的调用不会走到这里。
 */
export function installGlobalApiErrorToast(toast: (msg: string) => void): () => void {
  const seen = new Map<string, number>();
  const onRejection = (ev: PromiseRejectionEvent) => {
    const e = ev.reason;
    if (!(e instanceof ApiError)) return;
    // 同一接口 3 秒内只提示一次：断网时几十个请求同时失败，
    // 刷屏的 toast 会把界面糊死，反而看不清到底出了什么事。
    const now = Date.now();
    const last = seen.get(e.path) || 0;
    if (now - last < 3000) { ev.preventDefault(); return; }
    seen.set(e.path, now);
    toast(`${e.friendly}（${e.path}）`);
    ev.preventDefault();   // 已经告诉用户了，不必再往控制台抛红
  };
  window.addEventListener('unhandledrejection', onRejection);
  return () => window.removeEventListener('unhandledrejection', onRejection);
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
