// 全局应用状态（Zustand）：版本/特性、模式、连接、状态徽标、主题、工作区、限额。
import { create } from 'zustand';
import { apiGet, apiPost } from '../api/client';

export type Mode = 'chat' | 'work' | 'coding' | 'multi' | 'loop';
export type View = 'chat' | 'plan' | 'tools' | 'experts' | 'team' | 'kb'
  | 'stats' | 'schedule' | 'history' | 'audit' | 'router';

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
  wsState: 'connected' | 'disconnected' | 'running';
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
    apiPost('/config', { interaction: m }).catch(() => {});
  },

  setRunning: (on) => set((s) => ({
    running: on,
    wsState: on ? 'running' : (s.wsState === 'disconnected' ? 'disconnected' : 'connected'),
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
