// v1.11.0 输入框的"常驻引用"：计划模式开关 + 当前推进的长期目标。
//
// 与 overrides（模式 / 专家 / 附件 / MCP / 技能，发完即清）不同，这两样是**状态**：
// 进入计划模式后每条消息都先出计划等你批准，直到你退出；挂上一个目标后每轮都
// 围绕它推进，直到你摘掉。所以按工作区持久化在本地，刷新后仍然生效。
import { create } from 'zustand';
import { apiDelete, apiGet, apiPatch, apiPost, SID } from '../api/client';
import { useApp } from './app';

export type GoalStatus = 'active' | 'paused' | 'done' | 'archived';
export const GOAL_STATUS_ZH: Record<GoalStatus, string> = {
  active: '进行中', paused: '已暂停', done: '已完成', archived: '已归档',
};

export interface GoalRun {
  at: number; task: string; status: string; success: boolean; summary: string;
  progress: string; interaction: string; tokens: number; duration_ms: number;
}
export interface Goal {
  id: string; title: string; deliverable: string; criteria: string; notes: string;
  status: GoalStatus; progress: string; suggest_done: boolean;
  created: number; updated: number; run_count: number; success_count: number; tokens: number;
  last_run?: GoalRun | null; runs?: GoalRun[];
}

interface Sticky { planMode: boolean; goal: { id: string; title: string } | null }

const KEY = 'automind_sticky_refs';
const scopeKey = () => useApp.getState().wsSuffix || '_';

function readAll(): Record<string, Sticky> {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { return {}; }
}
function readScope(): Sticky {
  const s = readAll()[scopeKey()];
  return { planMode: !!s?.planMode, goal: s?.goal && s.goal.id ? s.goal : null };
}
function writeScope(v: Sticky): void {
  try {
    const all = readAll();
    all[scopeKey()] = v;
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch { /* 隐私模式等写不进去：仅本页生效 */ }
}

const scope = () => ({ owner: SID, workspace: useApp.getState().wsSuffix || '' });

interface RefsState extends Sticky {
  goals: Goal[] | null;
  setPlanMode: (v: boolean) => void;
  setGoal: (g: { id: string; title: string } | null) => void;
  /** 切工作区后重新读本工作区的常驻引用 */
  reloadScope: () => void;
  loadGoals: () => Promise<Goal[]>;
  createGoal: (g: { title: string; deliverable?: string; criteria?: string }) => Promise<Goal>;
  updateGoal: (id: string, fields: Partial<Goal>) => Promise<Goal>;
  deleteGoal: (id: string) => Promise<void>;
  getGoal: (id: string) => Promise<Goal>;
  /** 任务结束时服务端推来的目标进度（goal_progress 事件） */
  patchGoal: (g: Partial<Goal> & { id: string }) => void;
}

export const useRefs = create<RefsState>((set, get) => ({
  ...readScope(),
  goals: null,

  setPlanMode: (v) => { set({ planMode: v }); writeScope({ planMode: v, goal: get().goal }); },
  setGoal: (g) => { set({ goal: g }); writeScope({ planMode: get().planMode, goal: g }); },
  reloadScope: () => set({ ...readScope(), goals: null }),

  loadGoals: async () => {
    const { owner, workspace } = scope();
    const d = await apiGet(`/goals?owner=${encodeURIComponent(owner)}&workspace=${encodeURIComponent(workspace)}`);
    const goals: Goal[] = d.goals || [];
    set({ goals });
    // 挂着的目标被删了 / 归档了 → 摘掉，别让每条消息都带一个失效的目标
    const cur = get().goal;
    if (cur) {
      const g = goals.find((x) => x.id === cur.id);
      if (!g || g.status === 'archived') get().setGoal(null);
      else if (g.title !== cur.title) get().setGoal({ id: g.id, title: g.title });
    }
    return goals;
  },
  createGoal: async (g) => {
    const d = await apiPost('/goals', { ...scope(), ...g });
    await get().loadGoals();
    return d.goal;
  },
  updateGoal: async (id, fields) => {
    const d = await apiPatch(`/goals/${encodeURIComponent(id)}`, { owner: SID, ...fields });
    await get().loadGoals();
    return d.goal;
  },
  deleteGoal: async (id) => {
    await apiDelete(`/goals/${encodeURIComponent(id)}?owner=${encodeURIComponent(SID)}`);
    if (get().goal?.id === id) get().setGoal(null);
    await get().loadGoals();
  },
  getGoal: async (id) => {
    const d = await apiGet(`/goals/${encodeURIComponent(id)}?owner=${encodeURIComponent(SID)}`);
    return d.goal;
  },
  patchGoal: (g) => set((s) => ({
    goals: s.goals ? s.goals.map((x) => (x.id === g.id ? { ...x, ...g } : x)) : s.goals,
  })),
}));
