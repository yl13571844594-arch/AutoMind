// 全局应用状态（Zustand）：版本/特性、模式、连接、状态徽标、主题、工作区、限额。
import { message } from 'antd';
import { create } from 'zustand';
import { apiGet, apiPost, errText } from '../api/client';

export type Mode = 'chat' | 'work' | 'coding' | 'multi' | 'loop';
export type View = 'chat' | 'plan' | 'tools' | 'experts' | 'team' | 'kb'
  | 'stats' | 'schedule' | 'history' | 'audit' | 'router' | 'observe';

export const MODE_LABELS: Record<string, string> = {
  chat: '对话', work: '工作', coding: '编程', multi: '协同', loop: '循环',
};
export const MODE_FEATURE: Partial<Record<Mode, string>> = {
  multi: 'multi_agent', loop: 'loop_engine',
};
export const EDITION_LABELS: Record<string, string> = {
  community: '社区版', pro: '专业版', enterprise: '企业版',
};

export interface StatusInfo {
  provider: string; model: string; llm_ready: boolean; has_api_key: boolean;
  llm_error: string; project: string; approval_mode: string;
  // 自动选路提示（如"已检测到 DeepSeek Key，将使用 deepseek-chat"）；无提示时为空串
  llm_notice?: string;
  mode_specific: boolean; interaction: string;
  quota?: { daily_used: number; daily_limit: number | null; workspace_limit: number | null };
}

interface AppState {
  edition: string;
  features: Record<string, boolean>;
  version: string;
  mode: Mode;
  view: View;
  running: boolean;
  // reconnecting：已断开且退避重连计时中。与 disconnected 分开，是为了让界面
  // 能明确告诉用户"正在自动重连"，而不是丢下一个静悄悄的"未连接"让人以为坏了。
  wsState: 'connected' | 'disconnected' | 'running' | 'reconnecting';
  wsAttempt: number;      // 已重连次数（0 = 尚未重连过）
  wsNextRetryAt: number;  // 下次重连的时间戳（ms），用于界面倒计时
  status: StatusInfo | null;
  theme: 'dark' | 'light';
  wsActive: string;       // 当前工作区名（'' = 默认）
  wsSuffix: string;       // 会话隔离后缀
  activeExpert: { id: string; name: string; icon: string } | null;

  featureOn: (key?: string) => boolean;
  setView: (v: View) => void;
  setMode: (m: Mode) => Promise<void>;
  setRunning: (on: boolean) => void;
  loadStatus: (forMode?: string) => Promise<void>;
  loadHealth: () => Promise<void>;
  toggleTheme: () => void;
  setWorkspace: (name: string, suffix: string) => void;
  refreshExpert: () => Promise<void>;
}

const savedTheme = ((): 'dark' | 'light' => {
  const s = localStorage.getItem('automind_theme');
  if (s === 'light' || s === 'dark') return s;
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
})();

export const useApp = create<AppState>((set, get) => ({
  edition: 'community',
  features: {},
  version: '',
  mode: 'chat',
  view: 'chat',
  running: false,
  wsState: 'disconnected',
  wsAttempt: 0,
  wsNextRetryAt: 0,
  status: null,
  theme: savedTheme,
  wsActive: localStorage.getItem('automind_ws_active') || '',
  wsSuffix: localStorage.getItem('automind_ws_suffix') || '',
  activeExpert: null,

  featureOn: (key?: string) => !key || !!get().features[key],

  setView: (v) => set({ view: v }),

  setMode: async (m) => {
    const { featureOn } = get();
    if (!featureOn(MODE_FEATURE[m])) return;   // 调用侧提示升级
    set({ mode: m, view: 'chat' });
    get().loadStatus(m);
    // 模式没存到服务端就静默过去，下次开界面又跳回旧模式 —— 说清楚。
    apiPost('/config', { interaction: m }).catch((e) => {
      message.warning(`模式已在本地切换，但未能保存到服务端：${errText(e)}`);
    });
  },

  // 任务跑完回到 connected 时，不能把"已断开/重连中"覆盖成"已连接"——
  // 那会在链路其实还断着的时候谎报连上了。
  setRunning: (on) => set((s) => ({
    running: on,
    wsState: on ? 'running'
      : (s.wsState === 'disconnected' || s.wsState === 'reconnecting' ? s.wsState : 'connected'),
  })),

  loadStatus: async (forMode?: string) => {
    try {
      const q = forMode ? `?interaction=${encodeURIComponent(forMode)}` : '';
      const s = await apiGet(`/status${q}`);
      set({
        status: s,
        edition: s.edition || 'community',
        features: s.features || {},
        ...(forMode ? {} : { mode: (s.interaction || 'chat') as Mode }),
      });
    } catch { /* ignore */ }
  },

  loadHealth: async () => {
    try {
      const h = await apiGet('/health');
      if (h.version) set({ version: h.version });
    } catch { /* ignore */ }
  },

  toggleTheme: () => {
    const next = get().theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('automind_theme', next);
    set({ theme: next });
    // 同步给服务端：桌面版启动画面在浏览器起来之前就要知道该用深色还是浅色，
    // 它读不到 localStorage，只能读配置文件。失败无所谓（纯外观降级）。
    apiPost('/config/ui', { theme: next }).catch((e) => {
      // 纯外观降级（只影响桌面版启动画面的配色），不打断用户，记一条即可
      console.warn('[automind] 主题未能同步到服务端:', errText(e));
    });
  },

  setWorkspace: (name, suffix) => {
    localStorage.setItem('automind_ws_active', name);
    localStorage.setItem('automind_ws_suffix', suffix);
    set({ wsActive: name, wsSuffix: suffix });
  },

  refreshExpert: async () => {
    try {
      const { SID } = await import('../api/client');
      const d = await apiGet(`/experts?session_id=${encodeURIComponent(SID + get().wsSuffix)}`);
      if (d.active) {
        const e = (d.installed || []).find((x: any) => x.id === d.active);
        set({ activeExpert: e ? { id: e.id, name: e.name, icon: e.icon || '🎓' } : { id: d.active, name: d.active, icon: '🎓' } });
      } else {
        set({ activeExpert: null });
      }
    } catch { /* ignore */ }
  },
}));

export function chatSid(): string {
  const { wsSuffix } = useApp.getState();
  return (localStorage.getItem('automind_sid') || 'default') + (wsSuffix || '');
}
