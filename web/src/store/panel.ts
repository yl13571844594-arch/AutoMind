// 右栏观测面板状态：实时统计 / 计划树 / 刷新信号（tokens、改动、审计、HTML）。
import { create } from 'zustand';

export interface TaskStats { steps: number; backtracks: number; tokens: number; duration_ms: number }

/**
 * 执行进度（给输入框上方那条轻量指示用）。
 *
 * 观测中心的 DAG 信息更全，但普通用户不知道有那个页面、更不会在跑任务时切过去；
 * 而长任务在对话区看起来就是"没反应"。故把"第几步 / 共几步 + 正在做什么 + 已耗时"
 * 提到输入框旁边，任何时候都在视线内。
 *
 * total = 0 表示步数未知（对话/编程模式没有预先生成的计划），此时只显示阶段与耗时，
 * 不画成假的百分比进度条 —— 编不出来的数字不如不给。
 */
export interface TaskProgress {
  phase: string;        // 阶段文案，如「正在执行」「协同中」
  cur: number;          // 当前第几步（1 起；0 = 尚未开始具体步骤）
  total: number;        // 总步数；0 = 未知
  label: string;        // 当前在做什么
  startedAt: number;    // 起始时间戳，用于显示已耗时
}

interface PanelState {
  stats: TaskStats;
  plan: any | null;
  refreshTick: number;          // 任务完成后 +1 → 各观测区拉新
  approval: null | {
    approval_id: string; tool: string; tier: string; reason: string;
    editable?: Record<string, any>;
    params: Record<string, string>;
  };
  teamFeed: any[];

  progress: TaskProgress | null;

  setStats: (s: Partial<TaskStats>) => void;
  setPlan: (p: any) => void;
  startProgress: (phase: string, label?: string) => void;
  patchProgress: (p: Partial<TaskProgress>) => void;
  clearProgress: () => void;
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
  progress: null,

  setStats: (s) => set((st) => ({ stats: { ...st.stats, ...s } })),
  setPlan: (p) => set({ plan: p }),

  startProgress: (phase, label = '') => set({
    progress: { phase, cur: 0, total: 0, label, startedAt: Date.now() },
  }),
  // 进度只在任务进行中有意义；没有进行中的任务时补丁一律丢弃，
  // 避免收尾事件把已经清掉的进度条又"复活"出来。
  patchProgress: (p) => set((st) => (st.progress ? { progress: { ...st.progress, ...p } } : st)),
  clearProgress: () => set({ progress: null }),
  bumpRefresh: () => set((st) => ({ refreshTick: st.refreshTick + 1 })),
  setApproval: (a) => set({ approval: a }),
  pushTeam: (d) => set((st) => ({ teamFeed: [d, ...st.teamFeed].slice(0, 50) })),
}));
