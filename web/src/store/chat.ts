// 会话内容状态（Zustand）：按模式独立的消息列表 + 流式/执行面板中间态。
// 结构化消息（而非旧版的 innerHTML 快照）持久化到 localStorage，
// 按 会话ID+工作区 隔离；单模式 300KB / 总量 1.2MB 截断（保留最新）。
import { create } from 'zustand';
import type { Mode } from './app';

export interface TraceItem {
  label: string; body: string; kind: string;   // body 为已渲染安全 HTML
}
// goalId：后端事件用 goal_id 标识步骤（不是下标），进度更新必须按它匹配
export interface PlanRow { text: string; goalId?: string; state: 'pending' | 'run' | 'ok' | 'fail'; error?: string }

// 多智能体审查结论（父 Agent._review_result 的形状）。
// issues 是**字符串**（问题清单拼成一段话），不是数组 —— 别按数组渲染。
export interface MaReview {
  approved?: boolean;
  available?: boolean;     // false = 审查设施没跑成，不能当成"审查通过"
  issues?: string;
  reason?: string;
  error?: string;
}

// 收尾验证里"项目测试"那一段（_run_project_tests 的形状）。
export interface MaTestResult {
  passed?: boolean;
  unavailable?: boolean;   // 测试设施跑不起来（≠ 没有测试、≠ 测试失败）
  detail?: string;
}

// 收尾验证里"语义验收"那一段（_loop_verify 的形状）。
export interface MaVerdict {
  done?: boolean;
  available?: boolean;     // false = 验收不可用，不能当成"验收未过"
  reason?: string;
  source?: string;
}

// 工程协同的收尾验证摘要：tests / llm 两段证据各自可缺（没有就如实不显示）。
// reason/rounds 是编排器对整轮验证的补充说明（第几轮、为什么要修）。
export interface MaVerification {
  done?: boolean;
  tests?: MaTestResult | null;
  llm?: MaVerdict | null;
  reason?: string;
  rounds?: number;
}

// 编排器聚合指标（engineering 返回的 metrics）。字段可能随后端增删，
// 类型放宽 —— 前端只读自己要展示的那几个数字，不因多一个键报错。
export type MaMetrics = Record<string, number | string | boolean | null>;

// v1.11.22 工程协同：角色步骤从"纯文本流水线的一行"升级成 DAG 里的节点。
// 新增字段**全部可选**：旧文本流水线只发 role/subtask/output，旧记录（localStorage
// 里已落盘的 multi 卡片）没有它们，渲染时按原布局降级，不能凭空显示"依赖/预算/回炉"。
export interface MaStep {
  role: string; subtask: string;
  // skip：依赖失败被跳过（不跑）；fail：跑了但失败 —— 两者在界面上必须能区分
  state: 'pending' | 'run' | 'ok' | 'skip' | 'fail';
  output?: string;
  id?: string;                          // DAG 节点 id（新编排器用它定位步骤）
  label?: string;                       // 后端给的中文标签（缺省用角色名映射）
  dependsOn?: (string | number)[];      // 依赖的节点 id
  writePaths?: string[];                // 该角色的写作用域（共享工作区内相对路径）
  tokens?: number;                      // 本角色实际 token
  steps?: number;                       // 本角色 ReAct 步数
  llmCalls?: number;                    // 本角色 LLM 调用次数
  rework?: number;                      // 审查不通过后的回炉次数
  review?: MaReview | null;             // 只读 reviewer 的结论
  error?: string;                       // 失败/跳过原因（悬停可见）
  stopReason?: string;                  // 角色自己的停止原因（max_iterations 等）
}
export interface LoopIter { iter: number; max: number; action?: string; obs?: string; done?: boolean }

// 中途插入的补充在各阶段的状态。injected 只是"这是一条补充"，与是否生效无关。
// promoted：当时没有任务在跑，它被服务端提升成了一次新任务 —— 也算有了定论，
// 否则任务结束时会被"未确认兜底"误判成"没纳入"，那是对用户撒谎。
export type InjectState = 'received' | 'applied' | 'promoted' | 'dropped' | 'rejected';

// v1.7.7 输入框 overrides 的回执 chip：发送时是 pending，task_start 带回服务端
// 回执后变成 ok / fail（fail 带原因）。挂在用户那条消息上并随消息落盘 ——
// "这条是用哪个专家/模式/附件跑的"事后回看时还得查得到。
export interface MsgChip {
  key: string; label: string; state: 'pending' | 'ok' | 'fail'; note?: string;
}

export type ChatItem =
  // 插入补充（injected）另带一组状态位：已纳入本轮的 / 确认没纳入的 / 被拒的。
  // 拆成独立字段而不是一个枚举，是为了让"有没有效"也能跟着消息一起落盘 ——
  // 刷新后徽标仍停在当时的状态，而不是退回"待读取"让人以为还没生效。
  // injectNote 存服务端给的原因/位置（哪一步读到的、为什么没纳入），挂在徽标上。
  | {
      kind: 'msg'; id: string; role: 'user' | 'agent'; md: string; images?: string[]; meta?: string;
      injected?: boolean; applied?: boolean; promoted?: boolean; dropped?: boolean; rejected?: boolean;
      injectNote?: string;
      chips?: MsgChip[];
    }
  | { kind: 'welcome'; id: string }
  | { kind: 'stream'; id: string; buf: string }
  | { kind: 'typing'; id: string }
  // traceDropped：轨迹是有上限的（见 ws.ts MAX_TRACES）。超出后丢最旧的，
  // 并把丢掉的条数记在这里明说 —— 悄悄少几步会让人误判 Agent 漏做了事。
  | { kind: 'exec'; id: string; traces: TraceItem[]; plan: PlanRow[]; done: boolean; traceDropped?: number }
  // v1.11.22：工程协同卡片额外带 DAG 标记、收尾验证摘要与聚合指标；
  // 旧文本流水线不写这些字段，卡片渲染保持原样。
  | {
      kind: 'multi'; id: string; steps: MaStep[]; done: boolean;
      engineering?: boolean; verification?: MaVerification; metrics?: MaMetrics;
    }
  | { kind: 'loop'; id: string; iters: LoopIter[]; stopReason?: string; done: boolean; traces: TraceItem[]; traceDropped?: number }
  | { kind: 'resume'; id: string; why: string }
  // v1.8.0 工作流运行卡片：只存 run_id，进度从 store/workflow 实时读取
  | { kind: 'workflow'; id: string; runId: string; name: string }
  // v1.11.11 交付卡片（presented_files）：模型用 present 工具声明"这些是交付物"。
  // 只存路径与说明 —— 文件内容可能很大且随时被改，点开时再读，卡片本身保持轻量、可持久化。
  | {
      kind: 'deliverable'; id: string;
      files: { path: string; name?: string; description?: string }[];
      count?: number;
    }
  // v1.7.7 隔离工作副本的产物卡片：改了哪些文件 + 应用到项目 / 导出 / 删除副本
  | {
      kind: 'workspace'; id: string; sid: string; path: string;
      added: string[]; modified: string[]; deleted: string[];
      applied?: string[]; conflicts?: string[]; removed?: boolean;
    }
  // 任务失败/中断。**自带任务快照**（task/taskMode）而不是只指向易失的 lastTask：
  // 这样重启应用后历史里的失败卡片仍然能续跑，不会变成一个点了报
  // "没有可继续的任务"的死按钮。
  // v1.11.21：服务端现在把结构化停因随 task_error / task_complete 下发，
  // 卡片要能回答"为什么停 / 还有没有可续跑断点 / 这一轮花了多少次模型调用"。
  // 全部可选：旧版本落盘的消息没有这些字段，缺失时按原来的布局渲染。
  | {
      kind: 'error'; id: string; why: '出错' | '中断'; error: string;
      task?: string; taskMode?: Mode; at?: string;
      stopReason?: string; resumable?: boolean; budgetExhausted?: boolean; llmCalls?: number;
    };

// 停止原因（后端 stop_reason 的英文枚举）→ 中文短标签。
// 历史页徽标、失败卡片、任务结果 meta 三处共用一份映射，避免各写一套后
// 口径慢慢分叉。未知值原样返回 —— 后端以后新增原因时至少不会显示成空白。
export const STOP_REASON_ZH: Record<string, string> = {
  completed: '已完成',
  max_iterations: '步数上限',
  task_budget: '预算耗尽',
  budget_exhausted: '预算耗尽',
  no_progress: '无进展',
  llm_unavailable: '模型不可达',
  partial: '部分交付',
  interrupted: '已中断',
  cancelled: '已取消',
  // v1.11.22 工程协同：依赖失败的节点被跳过；收尾验证未过是任务级失败原因
  skipped: '已跳过',
  timeout: '超时',
  verification_failed: '验证未通过',
};

/** 停止原因转中文；空值返回空串（缺失时调用方据此不渲染）。 */
export function stopReasonZh(raw?: unknown): string {
  const k = typeof raw === 'string' ? raw.trim() : '';
  if (!k) return '';
  return STOP_REASON_ZH[k] || k;
}

let seq = 0;
export const uid = () => 'i' + (++seq) + '_' + Date.now().toString(36);

const PER_MODE = 300 * 1024;
const TOTAL = 1200 * 1024;

function storageKey(): string {
  const sid = localStorage.getItem('automind_sid') || 'default';
  const suffix = localStorage.getItem('automind_ws_suffix') || '';
  // v1.7.6 多会话：默认会话后缀为空，键与旧版完全一致（升级后历史不丢）
  let conv = '';
  try { conv = localStorage.getItem('automind_conv' + suffix) || ''; } catch { /* ignore */ }
  if (!/^_c[a-z0-9]{1,16}$/.test(conv)) conv = '';
  return 'automind_msgs_' + sid + suffix + conv;
}

/** 读取任意会话的本地消息（归档时把过程记录带给服务端生成摘要）。 */
export function readConversationMessages(convSuffix: string): Partial<Record<Mode, ChatItem[]>> {
  const sid = localStorage.getItem('automind_sid') || 'default';
  const suffix = localStorage.getItem('automind_ws_suffix') || '';
  try {
    return JSON.parse(localStorage.getItem('automind_msgs_' + sid + suffix + convSuffix) || '{}') || {};
  } catch { return {}; }
}

/** 删除会话时一并清掉它的本地消息与草稿。 */
export function dropConversationStorage(convSuffix: string): void {
  const sid = localStorage.getItem('automind_sid') || 'default';
  const suffix = localStorage.getItem('automind_ws_suffix') || '';
  const k = 'automind_msgs_' + sid + suffix + convSuffix;
  try { localStorage.removeItem(k); localStorage.removeItem(k + '_side'); } catch { /* ignore */ }
}

function loadPersisted(): Partial<Record<Mode, ChatItem[]>> {
  try {
    return JSON.parse(localStorage.getItem(storageKey()) || '{}') || {};
  } catch { return {}; }
}

// 草稿与"上次任务"另存一个键：它们体积很小，不该参与消息列表那套按体积截断的
// 逻辑（否则消息一多就会把它们一起丢掉）。
function sideKey(): string {
  return storageKey() + '_side';
}

interface SideState {
  draft: string;
  lastTask: { text: string; mode: Mode } | null;
}

function loadSide(): SideState {
  try {
    const raw = JSON.parse(localStorage.getItem(sideKey()) || '{}') || {};
    return {
      draft: typeof raw.draft === 'string' ? raw.draft : '',
      lastTask: raw.lastTask && typeof raw.lastTask.text === 'string' ? raw.lastTask : null,
    };
  } catch { return { draft: '', lastTask: null }; }
}

function persistable(items: ChatItem[]): ChatItem[] {
  // 剥离进行中的流式/打字中间态；执行面板保留（已定格内容可回看）
  return items.filter((i) => i.kind !== 'stream' && i.kind !== 'typing');
}

interface ChatState {
  messages: Partial<Record<Mode, ChatItem[]>>;
  taskMode: Mode | null;             // 当前执行中任务所属模式
  lastTask: { text: string; mode: Mode } | null;
  pendingImages: string[];
  inputDraft: string;

  items: (mode: Mode) => ChatItem[];
  append: (mode: Mode, item: ChatItem) => void;
  update: (mode: Mode, id: string, patch: (item: ChatItem) => ChatItem) => void;
  remove: (mode: Mode, id: string) => void;
  truncateFrom: (mode: Mode, id: string) => void;
  removeKind: (mode: Mode, kinds: string[]) => void;
  clearMode: (mode: Mode) => void;
  setMessages: (mode: Mode, items: ChatItem[]) => void;
  reload: () => void;
  markInjected: (id: string, state: InjectState, note?: string) => void;
  setChips: (id: string, patch: (chips: MsgChip[]) => MsgChip[]) => void;
  setTaskMode: (m: Mode | null) => void;
  setLastTask: (t: { text: string; mode: Mode } | null) => void;
  setPendingImages: (imgs: string[]) => void;
  setInputDraft: (s: string) => void;
  persist: () => void;
  persistSide: () => void;
}

const SIDE0 = loadSide();

export const useChat = create<ChatState>((set, get) => ({
  messages: loadPersisted(),
  taskMode: null,
  lastTask: SIDE0.lastTask,
  pendingImages: [],
  inputDraft: SIDE0.draft,

  items: (mode) => get().messages[mode] || [],

  append: (mode, item) => {
    set((s) => ({ messages: { ...s.messages, [mode]: [...(s.messages[mode] || []), item] } }));
    get().persist();
  },

  update: (mode, id, patch) => {
    set((s) => ({
      messages: {
        ...s.messages,
        [mode]: (s.messages[mode] || []).map((i) => (i.id === id ? patch(i) : i)),
      },
    }));
  },

  // 用户手动删除的必须落盘，否则刷新一下被删的消息又回来了
  remove: (mode, id) => {
    set((s) => ({
      messages: { ...s.messages, [mode]: (s.messages[mode] || []).filter((i) => i.id !== id) },
    }));
    get().persist();
  },

  // 删掉该条及其之后的全部消息 —— 编辑重发时用：改了问题，后面基于旧问题
  // 产生的回答就都失效了，留着只会让上下文自相矛盾。
  truncateFrom: (mode, id) => {
    set((s) => {
      const items = s.messages[mode] || [];
      const at = items.findIndex((i) => i.id === id);
      return at < 0 ? s : { messages: { ...s.messages, [mode]: items.slice(0, at) } };
    });
    get().persist();
  },

  removeKind: (mode, kinds) => {
    set((s) => ({
      messages: { ...s.messages, [mode]: (s.messages[mode] || []).filter((i) => !kinds.includes(i.kind)) },
    }));
  },

  clearMode: (mode) => {
    set((s) => ({ messages: { ...s.messages, [mode]: [] } }));
    get().persist();
  },

  setMessages: (mode, items) => {
    set((s) => ({ messages: { ...s.messages, [mode]: items } }));
    get().persist();
  },

  // 切会话/工作区后除消息外，草稿与"上次任务"也要跟着换成该会话的
  reload: () => {
    const side = loadSide();
    set({ messages: loadPersisted(), inputDraft: side.draft, lastTask: side.lastTask });
  },

  /**
   * 给一条插入补充落定状态（由服务端事件驱动，见 ws.ts 的 interjection_* 分支）。
   *
   * 按 id 在**所有模式**里找，而不是只查当前模式：补充是发在当时那个模式列表
   * 里的，事件回来时用户可能已经切走（切模式不阻塞执行），只查当前模式会漏标，
   * 气泡就永远停在"待读取"。id 由发送方生成、全局唯一，跨模式扫描不会误伤。
   */
  markInjected: (id, state, note) => {
    const hit = (i: ChatItem): ChatItem => (i.kind !== 'msg' ? i : {
      ...i,
      injected: true,
      applied: state === 'applied',
      // 被提升成新任务的既不是"没纳入"也不是"纳入本轮"，它有自己的一档
      promoted: state === 'promoted',
      // 被拒的也归到"未纳入"这一侧：对用户而言两者一样 —— 这句话没进本轮回答
      dropped: state === 'dropped' || state === 'rejected',
      rejected: state === 'rejected',
      injectNote: note || i.injectNote,
    });
    set((s) => {
      let changed = false;
      const messages: Partial<Record<Mode, ChatItem[]>> = { ...s.messages };
      for (const k of Object.keys(messages) as Mode[]) {
        const list = messages[k];
        if (!list || !list.some((i) => i.id === id)) continue;
        messages[k] = list.map((i) => (i.id === id ? hit(i) : i));
        changed = true;
      }
      return changed ? { messages } : s;
    });
    // 落盘：状态是用户事后回看时的依据，丢了刷新一下就像从没生效过
    get().persist();
  },

  // 按消息 id 在所有模式里找（同 markInjected：回执回来时用户可能已切走模式）
  setChips: (id, patch) => {
    set((s) => {
      const messages: Partial<Record<Mode, ChatItem[]>> = { ...s.messages };
      let changed = false;
      for (const k of Object.keys(messages) as Mode[]) {
        const list = messages[k];
        if (!list || !list.some((i) => i.id === id)) continue;
        messages[k] = list.map((i) => (i.id === id && i.kind === 'msg'
          ? { ...i, chips: patch(i.chips || []) } : i));
        changed = true;
      }
      return changed ? { messages } : s;
    });
    get().persist();
  },

  setTaskMode: (m) => set({ taskMode: m }),
  setLastTask: (t) => { set({ lastTask: t }); get().persistSide(); },
  setPendingImages: (imgs) => set({ pendingImages: imgs }),
  setInputDraft: (v) => { set({ inputDraft: v }); get().persistSide(); },

  persist: () => {
    try {
      const msgs = get().messages;
      const out: Record<string, ChatItem[]> = {};
      for (const k of Object.keys(msgs) as Mode[]) {
        let items = persistable(msgs[k] || []);
        // 单模式截断：从头丢弃直至体积达标
        while (JSON.stringify(items).length > PER_MODE && items.length > 1) items = items.slice(1);
        out[k] = items;
      }
      let payload = JSON.stringify(out);
      // 总量控制：丢弃体积最大的模式
      while (payload.length > TOTAL) {
        const big = Object.keys(out).sort(
          (a, b) => JSON.stringify(out[b]).length - JSON.stringify(out[a]).length)[0];
        if (!big) break;
        delete out[big];
        payload = JSON.stringify(out);
      }
      localStorage.setItem(storageKey(), payload);
    } catch { /* 配额溢出等：忽略 */ }
  },

  persistSide: () => {
    try {
      const { inputDraft, lastTask } = get();
      // 超长草稿（粘了一大段日志）没必要整段留着，截断保底避免撑爆配额
      localStorage.setItem(sideKey(), JSON.stringify({
        draft: inputDraft.slice(0, 200 * 1024), lastTask,
      }));
    } catch { /* 配额溢出等：忽略 */ }
  },
}));
