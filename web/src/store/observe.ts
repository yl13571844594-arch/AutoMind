// 观测中心状态：由 WebSocket 事件流实时构建当前任务的执行 DAG。
//
// 为什么在前端也建一份图（而不是只轮询后端 /api/observe/dag）：
//   事件本来就已经推到前端，直接就地累积即可做到**真正实时**（零轮询延迟、
//   零额外请求）。后端的同名接口用于刷新页面后的补图与专业版历史回看。
// 社区版语义：只保留当前任务，新任务开始即替换；只读，无历史。
import { create } from 'zustand';

export type NodeStatus = 'pending' | 'running' | 'ok' | 'fail' | 'backtrack' | 'cancelled';
export type NodeKind = 'task' | 'step' | 'action';

export interface DagNode {
  id: string;
  kind: NodeKind;
  label: string;
  status: NodeStatus;
  tool?: string | null;
  error?: string;
  t0?: number | null;
  t1?: number | null;
}
export interface DagEdge { f: string; t: string; kind: 'seq' | 'sub' | 'call' }

export interface DagCounters {
  steps: number; actions: number; backtracks: number; failures: number; truncated: number;
}

interface ObserveState {
  runId: string;
  task: string;
  interaction: string;
  status: 'idle' | 'running' | 'ok' | 'fail' | 'cancelled';
  startedAt: number | null;
  finishedAt: number | null;
  nodes: DagNode[];
  edges: DagEdge[];
  counters: DagCounters;
  selected: string | null;

  onEvent: (data: any) => void;
  select: (id: string | null) => void;
  hydrate: (graph: any) => void;
  clear: () => void;
}

const EMPTY: DagCounters = { steps: 0, actions: 0, backtracks: 0, failures: 0, truncated: 0 };
const MAX_NODES = 400;   // 与后端 observability.MAX_NODES 保持一致

const now = () => Date.now();

export const useObserve = create<ObserveState>((set, get) => ({
  runId: '',
  task: '',
  interaction: '',
  status: 'idle',
  startedAt: null,
  finishedAt: null,
  nodes: [],
  edges: [],
  counters: { ...EMPTY },
  selected: null,

  select: (id) => set({ selected: id }),

  clear: () => set({
    runId: '', task: '', interaction: '', status: 'idle', startedAt: null, finishedAt: null,
    nodes: [], edges: [], counters: { ...EMPTY }, selected: null,
  }),

  // 用后端快照补图（刷新页面后恢复当前任务视图）
  hydrate: (graph) => {
    if (!graph) return;
    set({
      runId: graph.id || '', task: graph.task || '', interaction: graph.interaction || '',
      status: graph.status === 'running' ? 'running' : (graph.status || 'idle'),
      startedAt: graph.started_at ?? null, finishedAt: graph.finished_at ?? null,
      nodes: (graph.nodes || []).map((n: any) => ({ ...n })),
      edges: (graph.edges || []).map((e: any) => ({ ...e })),
      counters: { ...EMPTY, ...(graph.counters || {}) },
    });
  },

  onEvent: (data: any) => {
    const type = data?.type;
    if (!type) return;
    const s = get();

    switch (type) {
      case 'task_start':
        set({
          runId: `live-${now()}`, task: '', interaction: data.interaction || '',
          status: 'running', startedAt: now(), finishedAt: null, selected: null,
          nodes: [{ id: 'root', kind: 'task', label: '任务', status: 'running', t0: now(), t1: null }],
          edges: [], counters: { ...EMPTY },
        });
        break;

      case 'plan_created': {
        if (s.status === 'idle') return;
        const nodes = [...s.nodes];
        const edges = [...s.edges];
        let steps = s.counters.steps;
        const rootGoalId = String(data.root_goal_id || '');
        // 按计划树的真实父子关系连边；叶子之间通常无先后依赖（实测并行/交错
        // 执行），串成链会显示出并不存在的依赖关系。
        for (const st of data.steps || []) {
          const id = String(st.goal_id || '');
          if (!id || nodes.some((n) => n.id === id)) continue;
          if (nodes.length >= MAX_NODES) break;
          nodes.push({
            id, kind: 'step', label: st.description || '', status: 'pending',
            tool: st.tool ?? null, t0: null, t1: null,
          });
          let parent = String(st.parent_id || '');
          if (!parent || parent === rootGoalId || !nodes.some((n) => n.id === parent)) {
            parent = 'root';
          }
          edges.push({ f: parent, t: id, kind: 'sub' });
          steps += 1;
        }
        const taskText = data.task || s.task;
        // 根节点显示任务描述本身（否则与 kind 标签重复显示"任务/任务"）
        const withRoot = taskText
          ? nodes.map((n) => (n.id === 'root' ? { ...n, label: taskText.slice(0, 60) } : n))
          : nodes;
        set({ nodes: withRoot, edges, task: taskText, counters: { ...s.counters, steps } });
        break;
      }

      // 注意：后端事件用 goal_id 标识步骤（没有 index 字段）
      case 'plan_step_start': {
        if (s.status === 'idle') return;
        const id = String(data.goal_id || '');
        if (!id) return;
        if (!s.nodes.some((n) => n.id === id)) {
          // 重规划补充的计划外步骤
          set({
            nodes: [...s.nodes, {
              id, kind: 'step', label: data.description || '', status: 'running',
              tool: data.tool ?? null, t0: now(), t1: null,
            }],
            edges: [...s.edges, { f: 'root', t: id, kind: 'sub' }],
            counters: { ...s.counters, steps: s.counters.steps + 1 },
          });
          return;
        }
        set({
          nodes: s.nodes.map((n) => (n.id === id
            ? { ...n, status: 'running' as NodeStatus, t0: now() } : n)),
        });
        break;
      }

      case 'plan_step_end': {
        const id = String(data.goal_id || '');
        if (!id) return;
        const ok = !!data.success;
        set({
          nodes: s.nodes.map((n) => (n.id === id
            ? { ...n, status: (ok ? 'ok' : 'fail') as NodeStatus, t1: now(), error: data.error || '' }
            : n)),
          counters: { ...s.counters, failures: s.counters.failures + (ok ? 0 : 1) },
        });
        break;
      }

      case 'plan_backtrack': {
        const id = String(data.goal_id || '');
        set({
          nodes: s.nodes.map((n) => (n.id === id
            ? { ...n, status: 'backtrack' as NodeStatus, error: data.reason || '' } : n)),
          counters: { ...s.counters, backtracks: s.counters.backtracks + 1 },
        });
        break;
      }

      case 'step_action': {
        if (s.status === 'idle') return;
        if (s.nodes.length >= MAX_NODES) {
          set({ counters: { ...s.counters, truncated: s.counters.truncated + 1 } });
          return;
        }
        // 归属优先级：事件自带 goal_id → 当前 running 的步骤 → root。
        // 步骤窗口外发生的调用如实挂在 root，不硬凑进某个步骤。
        const gid = String(data.goal_id || '');
        const parent = (gid ? s.nodes.find((n) => n.id === gid) : undefined)
          || [...s.nodes].reverse().find((n) => n.kind === 'step' && n.status === 'running');
        const id = `act${s.counters.actions}`;
        const ok = data.success !== false;
        set({
          nodes: [...s.nodes, {
            id, kind: 'action', label: data.tool || '工具调用',
            status: (ok ? 'ok' : 'fail') as NodeStatus, tool: data.tool,
            t0: now(), t1: now(), error: ok ? '' : String(data.output || '').slice(0, 300),
          }],
          edges: [...s.edges, { f: parent?.id || 'root', t: id, kind: 'call' }],
          counters: {
            ...s.counters, actions: s.counters.actions + 1,
            failures: s.counters.failures + (ok ? 0 : 1),
          },
        });
        break;
      }

      case 'task_complete':
      case 'chat_done':
      case 'task_error':
      case 'task_cancelled': {
        if (s.status === 'idle') return;
        const st = type === 'task_error' ? 'fail'
          : type === 'task_cancelled' ? 'cancelled' : 'ok';
        set({
          status: st as any, finishedAt: now(),
          nodes: s.nodes.map((n) => {
            if (n.id === 'root') {
              return { ...n, status: st as NodeStatus, t1: now(), error: data.error || '' };
            }
            // 收尾：仍在 running 的步骤归位，避免图上永远转圈
            if (n.kind === 'step' && n.status === 'running') {
              return { ...n, status: (type === 'task_cancelled' ? 'cancelled' : 'fail') as NodeStatus, t1: now() };
            }
            return n;
          }),
        });
        break;
      }
    }
  },
}));
