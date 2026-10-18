// 右栏观测面板状态：实时统计 / 计划树 / 刷新信号（tokens、改动、审计、HTML）。
import { create } from 'zustand';

export interface TaskStats { steps: number; backtracks: number; tokens: number; duration_ms: number }

/**
 * v1.11.10 实时缓存命中 / 上下文占用（右栏「⚡ 缓存与上下文」）。
 *
 * 为什么不并进 TaskStats：那四个字段是任务终态一次性回填的"结果"，
 * 而缓存命中率与上下文占用是**每次 LLM 调用**都在变的过程量 —— 任务进行中要一直动，
 * 任务结束后还要留着给用户看"这一轮省了多少"。故单独一份状态，由 usage_update 覆盖。
 *
 * cache_rate 用 null 而不是 0 表示"提供商没给缓存统计"：两者在界面上是不同的话
 * （「—」vs「0%」），0% 会被读成"缓存一次都没命中"，那是另一回事。
 */
export interface LiveUsage {
  input_tokens: number;        // 本轮累计输入（含命中缓存的 token）
  cached_tokens: number;       // 其中命中提示缓存的 token
  calls: number;               // LLM 调用次数
  cache_hit_calls: number;     // 有缓存命中的调用次数
  cache_rate: number | null;   // 命中率 %；null = 提供商未提供缓存用量
  peak_prompt_tokens: number;  // 单次请求输入的峰值 —— 上下文占用看它，不看累计值
  context_max: number;         // 上下文窗口上限
  context_pct: number;         // 峰值 / 窗口上限（%）
  /** 本轮是否收到过 usage_update：false 时右栏回退 /api/tokens 的会话累计值 */
  active: boolean;
}

export const EMPTY_USAGE: LiveUsage = {
  input_tokens: 0, cached_tokens: 0, calls: 0, cache_hit_calls: 0,
  cache_rate: null, peak_prompt_tokens: 0, context_max: 0, context_pct: 0,
  active: false,
};

// ── v1.11.11 待办 / 提问 / 子智能体 ─────────────────────────────

/** 模型维护的待办（``todo_write`` 工具整表替换；按会话持久化）。 */
export interface TodoItem {
  id: string;
  content: string;
  status: 'pending' | 'in_progress' | 'completed' | string;
}

export interface TodoCounts {
  pending: number;
  in_progress: number;
  completed: number;
}

/** 一个问题及其选项（与 ``ask_user_question`` 工具的规范化结构一致）。 */
export interface UserQuestion {
  id: string;
  question: string;
  header?: string;
  options?: { label: string; description?: string }[];
  multi?: boolean;
}

/**
 * 当前待用户回答的提问。
 *
 * 为什么把 ``askedAt`` 存在前端而不是只用 ``timeout_s``：倒计时需要一个起点，
 * 而事件到达时刻与工具开始等待的时刻有网络/队列延迟 —— 用收到事件的本地时间做起点，
 * 界面上的"剩余 N 秒"才会和用户实际等待的时长一致。
 */
export interface PendingQuestion {
  question_id: string;
  questions: UserQuestion[];
  timeout_s: number;
  askedAt: number;
}

/** 一个子智能体的实时状态（专业版 multi_agent；事件只来自 WS）。 */
export interface SubAgentInfo {
  agent_id: string;
  title: string;
  task?: string;
  status: string;          // running / done / failed / cancelled …
  output_tail?: string;    // 最近一段输出（用于长任务"还在动"的证明）
  output?: string;
  error?: string;
  steps?: number;
  tokens?: number;
}

/** 子智能体列表上限：再多也看不过来，且长任务里数量可以很大。 */
const MAX_SUBAGENTS = 20;

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

/** 浏览器自动化的一帧画面（v1.11.1 右侧「浏览器」实时预览）。 */
export interface BrowserFrame {
  id: number;
  b64: string;
  fmt: 'png' | 'jpeg';
  url: string;
  title: string;
  action: string;       // navigate / click / type / screenshot …
  at: number;
}

export type RightTab = 'summary' | 'panel' | 'browser' | 'code';

//: 只留最近这么多帧：每帧 ~15-40KB 的 base64，留多了长任务会把内存吃起来
const MAX_FRAMES = 16;

/** 右栏初始是否收起：用户选过就按他的；没选过时窄窗口（< 1100px）默认收起，给对话区让位。 */
function initialRightCollapsed(): boolean {
  try {
    const v = localStorage.getItem('automind_right_collapsed');
    if (v === '1' || v === '0') return v === '1';
  } catch { /* ignore */ }
  return typeof window !== 'undefined' && window.innerWidth < 1100;
}

interface PanelState {
  stats: TaskStats;
  /** v1.11.10：本轮任务的缓存命中 / 上下文占用（过程量，独立于任务终态的 stats） */
  usage: LiveUsage;
  plan: any | null;
  refreshTick: number;          // 任务完成后 +1 → 各观测区拉新
  approval: null | {
    approval_id: string; tool: string; tier: string; reason: string;
    editable?: Record<string, any>;
    timeoutS?: number;
    askedAt?: number;
    params: Record<string, string>;
    /** v1.8.0：来自工作流的审批（human 步骤或 agent 步骤内部的工具调用） */
    workflow?: {
      name: string; runId: string; stepId: string; stepIndex: number; stepType: string;
      /** v1.10.0（企业版多级审批）：这一级只接受哪些审批人、第几级 */
      required?: string[]; level?: number; levels?: number;
    };
  };
  teamFeed: any[];

  progress: TaskProgress | null;

  /** 右栏当前标签 / 是否收起 */
  rightTab: RightTab;
  rightCollapsed: boolean;
  /** 浏览器画面：最近若干帧 + 本轮是否允许自动切到「浏览器」标签 */
  frames: BrowserFrame[];
  browserAutoSwitch: boolean;

  /** v1.11.5：从「摘要」里点产物文件 → 切到代码标签并打开它（n 自增保证同一路径可重复触发） */
  codeRequest: { path: string; n: number } | null;
  openInCode: (path: string) => void;

  setRightTab: (t: RightTab, manual?: boolean) => void;
  toggleRight: () => void;
  pushFrame: (f: Omit<BrowserFrame, 'id' | 'at'>) => void;
  clearFrames: () => void;
  /** 新任务开始：重新允许"第一帧画面到来时自动切到浏览器标签" */
  armBrowserAutoSwitch: () => void;

  setStats: (s: Partial<TaskStats>) => void;
  /** v1.11.10：合并式更新本轮缓存 / 上下文（缺字段的兜底口径由 ws.ts 的 usage_update 统一处理） */
  setUsage: (u: Partial<LiveUsage>) => void;
  resetUsage: () => void;
  setPlan: (p: any) => void;
  startProgress: (phase: string, label?: string) => void;
  patchProgress: (p: Partial<TaskProgress>) => void;
  clearProgress: () => void;
  bumpRefresh: () => void;
  setApproval: (a: PanelState['approval']) => void;
  pushTeam: (d: any) => void;

  // ── v1.11.11 ──
  /** 模型维护的待办（todo_updated 事件 / GET /api/todo 初始化） */
  todos: TodoItem[];
  todoCounts: TodoCounts | null;
  setTodos: (todos: TodoItem[], counts?: Partial<TodoCounts>) => void;
  clearTodos: () => void;
  /** 当前待用户回答的提问（user_question 事件推送；null = 没有） */
  question: PendingQuestion | null;
  setQuestion: (q: Omit<PendingQuestion, 'askedAt'> & { askedAt?: number }) => void;
  clearQuestion: () => void;
  /** 子智能体（专业版 multi_agent 功能；subagent_* 事件推送） */
  subagents: SubAgentInfo[];
  pushSubagent: (a: SubAgentInfo) => void;
  updateSubagent: (agentId: string, patch: Partial<SubAgentInfo>) => void;
  clearSubagents: () => void;
  /** /compact 正在等结果：防重复点击，也让输入区的指令给得出"进行中"反馈 */
  compactBusy: boolean;
  setCompactBusy: (busy: boolean) => void;
}

export const usePanel = create<PanelState>((set) => ({
  stats: { steps: 0, backtracks: 0, tokens: 0, duration_ms: 0 },
  usage: EMPTY_USAGE,
  plan: null,
  refreshTick: 0,
  approval: null,
  teamFeed: [],
  progress: null,
  rightTab: 'summary',
  rightCollapsed: initialRightCollapsed(),
  frames: [],
  browserAutoSwitch: true,
  codeRequest: null,
  todos: [],
  todoCounts: null,
  question: null,
  subagents: [],
  compactBusy: false,

  openInCode: (path) => set((st) => ({
    rightTab: 'code', rightCollapsed: false, browserAutoSwitch: false,
    codeRequest: { path, n: (st.codeRequest?.n || 0) + 1 },
  })),

  setRightTab: (t, manual = true) => set((st) => ({
    rightTab: t,
    // 用户在任务进行中手动切走，就别再把他拽回来
    browserAutoSwitch: manual ? false : st.browserAutoSwitch,
  })),
  toggleRight: () => set((st) => {
    const v = !st.rightCollapsed;
    try { localStorage.setItem('automind_right_collapsed', v ? '1' : '0'); } catch { /* ignore */ }
    return { rightCollapsed: v };
  }),
  pushFrame: (f) => set((st) => {
    const frame: BrowserFrame = { ...f, id: (st.frames[0]?.id || 0) + 1, at: Date.now() };
    // 第一帧到来时自动切到「浏览器」：但不打断正在看代码的人
    const jump = st.browserAutoSwitch && (st.rightTab === 'panel' || st.rightTab === 'summary');
    return {
      frames: [frame, ...st.frames].slice(0, MAX_FRAMES),
      ...(jump ? { rightTab: 'browser' as RightTab, browserAutoSwitch: false } : {}),
    };
  }),
  clearFrames: () => set({ frames: [] }),
  armBrowserAutoSwitch: () => set({ browserAutoSwitch: true }),

  setStats: (s) => set((st) => ({ stats: { ...st.stats, ...s } })),
  setUsage: (u) => set((st) => ({ usage: { ...st.usage, ...u } })),
  // 复用同一个空对象：usage 只在 setUsage 里整体替换，不会被就地改写
  resetUsage: () => set({ usage: EMPTY_USAGE }),
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

  // ── v1.11.11 待办 / 提问 / 子智能体 / 压缩 ──
  // 计数优先用服务端给的（它按整表统计，口径与我们一致）；缺了就本地数，
  // 免得界面上出现"清单有 3 条、计数全是 0"这种自相矛盾的画面。
  setTodos: (todos, counts) => set(() => ({
    todos,
    todoCounts: {
      pending: counts?.pending ?? todos.filter((t) => t.status === 'pending').length,
      in_progress: counts?.in_progress ?? todos.filter((t) => t.status === 'in_progress').length,
      completed: counts?.completed ?? todos.filter((t) => t.status === 'completed').length,
    },
  })),
  clearTodos: () => set({ todos: [], todoCounts: null }),
  setQuestion: (q) => set({ question: { ...q, askedAt: q.askedAt || Date.now() } }),
  clearQuestion: () => set({ question: null }),
  pushSubagent: (a) => set((st) => ({
    subagents: [a, ...st.subagents.filter((x) => x.agent_id !== a.agent_id)]
      .slice(0, MAX_SUBAGENTS),
  })),
  // 更新即 upsert：页面刷新后只收到 update/done 也不该丢条目
  updateSubagent: (agentId, patch) => set((st) => {
    if (!agentId) return st;
    const at = st.subagents.findIndex((x) => x.agent_id === agentId);
    if (at < 0) {
      const base: SubAgentInfo = {
        ...patch,
        agent_id: agentId,
        title: String(patch.title || agentId),
        status: String(patch.status || 'running'),
      };
      return { subagents: [base, ...st.subagents].slice(0, MAX_SUBAGENTS) };
    }
    return {
      subagents: st.subagents.map((x, i) => (i === at ? { ...x, ...patch } : x)),
    };
  }),
  clearSubagents: () => set({ subagents: [] }),
  setCompactBusy: (busy) => set({ compactBusy: busy }),
}));
