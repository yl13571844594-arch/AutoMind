// 右栏观测面板状态：实时统计 / 计划树 / 刷新信号（tokens、改动、审计、HTML）。
import { create } from 'zustand';

export interface TaskStats { steps: number; backtracks: number; tokens: number; duration_ms: number }

interface PanelState {
  stats: TaskStats;
  plan: any | null;
  refreshTick: number;          // 任务完成后 +1 → 各观测区拉新
  approval: null | {
    approval_id: string; tool: string; tier: string; reason: string;
    params: Record<string, string>;
  };
  teamFeed: any[];

  setStats: (s: Partial<TaskStats>) => void;
  setPlan: (p: any) => void;
  bumpRefresh: () => void;
  setApproval: (a: PanelState['approval']) => void;
  pushTeam: (d: any) => void;
}

export const usePanel = create<PanelState>((set) => ({
  stats: { steps: 0, backtracks: 0, tokens: 0, duration_ms: 0 },
  plan: null,
  refreshTick: 0,
  approval: null,
  teamFeed: [],

  setStats: (s) => set((st) => ({ stats: { ...st.stats, ...s } })),
  setPlan: (p) => set({ plan: p }),
  bumpRefresh: () => set((st) => ({ refreshTick: st.refreshTick + 1 })),
  setApproval: (a) => set({ approval: a }),
  pushTeam: (d) => set((st) => ({ teamFeed: [d, ...st.teamFeed].slice(0, 50) })),
}));
