// 🔀 工作流运行的实时状态（v1.8.0）。
//
// 数据源是 WebSocket 推送的 wf_run_start / wf_step_start / wf_step_done /
// wf_iteration / wf_agent_action / wf_run_done，与"工作流"视图、对话区里的运行卡片
// 共用这一份 —— 同一次运行在两个地方显示的进度永远一致。
// 页面刷新后内存状态没了，卡片会回退到 GET /api/workflow/runs/{id} 拉一次。
import { create } from 'zustand';
import { apiGet, apiPost } from '../api/client';

export type WfStepStatus = 'pending' | 'running' | 'ok' | 'failed' | 'aborted'
  | 'skipped' | 'cancelled';

export interface WfStep {
  id: string; type: string; line?: number; status: WfStepStatus;
  error?: string; duration_ms?: number; retries?: number; detail?: Record<string, any>;
  /** foreach：已完成的项数 / 总项数 */
  iter?: { done: number; total: number; failed: number };
  /** agent：最近几次工具调用 */
  actions?: { tool: string; ok: boolean; error?: string }[];
  /** parallel（v1.9.0）：组内子步骤，各自独立显示进度 */
  children?: WfStep[];
}

export interface WfRun {
  run_id: string; workflow: string; file?: string; session_id?: string;
  status: string;                // running | ok | partial | aborted | failed | cancelled
  steps: WfStep[]; current?: string; startedAt: number;
  summary?: Record<string, any>; summary_line?: string; error?: string;
  duration_ms?: number; resumable?: boolean; resumed_from?: string;
  reused?: string[]; report?: any; tokens?: { total: number };
  /** v1.9.0：manual | cron | webhook */
  trigger?: string;
  /** v1.10.0（企业版并发配额）：排队中的位次；null / undefined = 没在排队 */
  queuePosition?: number | null;
  /** 仅本地：已请求停止，等服务端回执 */
  stopping?: boolean;
}

export const RUN_STATUS_ZH: Record<string, string> = {
  running: '运行中', ok: '全部成功', partial: '部分成功', aborted: '已中止',
  failed: '失败', cancelled: '已取消', dry_run: '试运行',
  completed: '全部成功', interrupted: '已中断', resumed: '已续跑', dismissed: '已忽略',
};

export const STEP_ICON: Record<string, string> = {
  pending: '○', running: '◌', ok: '✓', failed: '✗', aborted: '✗', skipped: '–', cancelled: '⊘',
};

export const STEP_TYPE_ICON: Record<string, string> = {
  tool: '🔧', llm: '💬', human: '🙋', branch: '🔀', agent: '🤖', foreach: '🔁', parallel: '⚡',
};

export const TRIGGER_ZH: Record<string, string> = {
  manual: '手动', cron: '⏰ 定时', webhook: '🔗 webhook',
};

interface WfState {
  runs: Record<string, WfRun>;
  onEvent: (data: any) => void;
  /** 刷新后内存里没有这次运行：从服务端补一份 */
  fetchRun: (runId: string) => Promise<void>;
  cancel: (runId: string) => Promise<void>;
  resume: (runId: string, sessionId: string) => Promise<string>;
}

function stepsFromReport(report: any): WfStep[] {
  return (report?.steps || []).map((s: any) => ({
    id: s.id, type: s.type, line: s.line, status: s.status, error: s.error,
    duration_ms: s.duration_ms, retries: s.retries, detail: s.detail,
    // parallel 组：子步骤的结果在组的 detail.children 里（报告只列顶层步骤）
    children: Array.isArray(s.detail?.children)
      ? s.detail.children.map((c: any) => ({
        id: c.id, type: c.type, line: c.line, status: c.status, error: c.error,
        duration_ms: c.duration_ms, detail: c.resumed ? { resumed: true } : undefined,
      }))
      : undefined,
  }));
}

/** 实时视图里的步骤（含 parallel 子步骤，带 parent）→ 顶层步骤 + children。 */
function stepsFromLive(rows: any[]): WfStep[] {
  const top: WfStep[] = [];
  const kids: Record<string, WfStep[]> = {};
  for (const r of rows || []) {
    const st = { ...r } as WfStep & { parent?: string };
    if (st.parent) (kids[st.parent] ||= []).push(st);
    else top.push(st);
  }
  return top.map((t) => (kids[t.id] ? { ...t, children: kids[t.id] } : t));
}

export const useWorkflow = create<WfState>((set, get) => ({
  runs: {},

  onEvent: (data) => {
    const id = String(data.run_id || '');
    if (!id) return;
    const patch = (fn: (r: WfRun) => WfRun) => set((s) => {
      const cur = s.runs[id];
      if (!cur) return s;
      return { runs: { ...s.runs, [id]: fn(cur) } };
    });
    const patchStep = (sid: string, fn: (st: WfStep) => WfStep) => patch((r) => ({
      ...r, steps: r.steps.map((st) => (st.id === sid ? fn(st) : st)),
    }));
    // parallel 组内的子步骤：事件带 parent，改的是组下面那一行
    const patchChild = (parent: string, sid: string, fn: (st: WfStep) => WfStep) =>
      patchStep(parent, (g) => {
        const list = g.children || [];
        const has = list.some((c) => c.id === sid);
        const base: WfStep = { id: sid, type: '', status: 'pending' };
        return {
          ...g,
          children: has ? list.map((c) => (c.id === sid ? fn(c) : c)) : [...list, fn(base)],
        };
      });

    switch (data.type) {
      case 'wf_run_queued':
        patch((r) => ({ ...r, queuePosition: data.position ?? null }));
        break;
      case 'wf_run_start':
        set((s) => ({
          runs: {
            ...s.runs,
            [id]: {
              run_id: id, workflow: data.workflow, file: data.file, session_id: data.session_id,
              status: 'running', startedAt: Date.now(), resumed_from: data.resumed_from || '',
              reused: data.reused_steps || [], trigger: data.trigger || 'manual',
              steps: (data.steps || []).map((st: any) => ({
                id: st.id, type: st.type, line: st.line, status: 'pending' as WfStepStatus,
                children: Array.isArray(st.children)
                  ? st.children.map((c: any) => ({
                    id: c.id, type: c.type, line: c.line, status: 'pending' as WfStepStatus,
                  }))
                  : undefined,
              })),
            },
          },
        }));
        break;
      case 'wf_step_start': {
        const s = data.step || {};
        if (s.parent) {
          patchChild(s.parent, s.id, (st) => ({ ...st, type: st.type || s.type, status: 'running' }));
          break;
        }
        patch((r) => ({ ...r, current: s.id }));
        patchStep(s.id, (st) => ({ ...st, status: 'running' }));
        break;
      }
      case 'wf_step_done': {
        const s = data.step || {};
        const apply = (st: WfStep): WfStep => ({
          ...st, type: st.type || s.type, status: s.status, error: s.error,
          duration_ms: s.duration_ms, retries: s.retries, detail: s.detail,
        });
        if (s.parent) patchChild(s.parent, s.id, apply);
        else patchStep(s.id, (st) => ({ ...apply(st), children: st.children }));
        break;
      }
      case 'wf_iteration': {
        const s = data.step || {};
        patchStep(s.id, (st) => ({
          ...st, iter: {
            done: Number(s.index ?? 0) + 1, total: Number(s.total || 0),
            failed: (st.iter?.failed || 0) + (s.status === 'ok' ? 0 : 1),
          },
        }));
        break;
      }
      case 'wf_agent_action': {
        const s = data.step || {};
        patchStep(s.id, (st) => ({
          ...st, actions: [...(st.actions || []), { tool: s.tool, ok: !!s.ok, error: s.error }].slice(-8),
        }));
        break;
      }
      case 'wf_run_done':
        if (!get().runs[id]) {
          // 开始事件没收到（例如页面是中途打开的）：用报告补齐
          set((s) => ({
            runs: {
              ...s.runs,
              [id]: {
                run_id: id, workflow: data.workflow, file: data.file, session_id: data.session_id,
                status: data.status, startedAt: Date.now(), steps: stepsFromReport(data.report),
              },
            },
          }));
        }
        patch((r) => ({
          ...r, status: data.status, summary: data.summary, summary_line: data.summary_line,
          error: data.error, duration_ms: data.duration_ms, resumable: !!data.resumable,
          report: data.report, tokens: data.tokens, stopping: false, current: '',
          steps: data.report?.steps?.length ? stepsFromReport(data.report).map((st) => {
            const old = r.steps.find((o) => o.id === st.id);
            return { ...st, iter: old?.iter, actions: old?.actions };
          }) : r.steps,
        }));
        break;
      default:
    }
  },

  fetchRun: async (runId) => {
    const d = await apiGet(`/workflow/runs/${encodeURIComponent(runId)}`);
    const live = !!d.live;
    const report = d.report || null;
    const steps: WfStep[] = live ? stepsFromLive(d.steps || []) : stepsFromReport(report);
    set((s) => ({
      runs: {
        ...s.runs,
        [runId]: {
          ...(s.runs[runId] || {}),
          run_id: runId, workflow: d.workflow, file: d.file, session_id: d.session_id,
          status: live ? 'running' : (report?.status || d.run_status || d.status),
          startedAt: (d.started_at || Date.now() / 1000) * 1000,
          steps: steps.length ? steps : (s.runs[runId]?.steps || []),
          current: d.current, summary: report?.summary, error: report?.error || d.error,
          resumable: !!d.resumable, report, resumed_from: d.resumed_from,
          tokens: report?.tokens, trigger: d.trigger || 'manual',
          queuePosition: d.queue_position ?? null,
        },
      },
    }));
  },

  cancel: async (runId) => {
    set((s) => (s.runs[runId]
      ? { runs: { ...s.runs, [runId]: { ...s.runs[runId], stopping: true } } } : s));
    await apiPost(`/workflow/runs/${encodeURIComponent(runId)}/cancel`);
  },

  resume: async (runId, sessionId) => {
    const r = await apiPost(`/workflow/runs/${encodeURIComponent(runId)}/resume`,
      { session_id: sessionId });
    set((s) => (s.runs[runId]
      ? { runs: { ...s.runs, [runId]: { ...s.runs[runId], resumable: false } } } : s));
    return String(r.run_id || '');
  },
}));

/** 报告下载（JSON）：评审方要能把"这一次到底跑了什么"原样存档。 */
export function downloadReport(run: { run_id: string; workflow?: string; report?: any }): void {
  const blob = new Blob([JSON.stringify(run.report || run, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `workflow-${(run.workflow || 'run').replace(/[\\/:*?"<>|\s]+/g, '_')}-${run.run_id}.json`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}
