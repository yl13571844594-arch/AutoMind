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
export interface MaStep { role: string; subtask: string; state: 'pending' | 'run' | 'ok'; output?: string }
export interface LoopIter { iter: number; max: number; action?: string; obs?: string; done?: boolean }

export type ChatItem =
  | { kind: 'msg'; id: string; role: 'user' | 'agent'; md: string; images?: string[]; meta?: string }
  | { kind: 'welcome'; id: string }
  | { kind: 'stream'; id: string; buf: string }
  | { kind: 'typing'; id: string }
  | { kind: 'exec'; id: string; traces: TraceItem[]; plan: PlanRow[]; done: boolean }
  | { kind: 'multi'; id: string; steps: MaStep[]; done: boolean }
  | { kind: 'loop'; id: string; iters: LoopIter[]; stopReason?: string; done: boolean; traces: TraceItem[] }
  | { kind: 'resume'; id: string; why: string }
  // 任务失败/中断。**自带任务快照**（task/taskMode）而不是只指向易失的 lastTask：
  // 这样重启应用后历史里的失败卡片仍然能续跑，不会变成一个点了报
  // "没有可继续的任务"的死按钮。
  | {
      kind: 'error'; id: string; why: '出错' | '中断'; error: string;
      task?: string; taskMode?: Mode; at?: string;
    };

let seq = 0;
export const uid = () => 'i' + (++seq) + '_' + Date.now().toString(36);

const PER_MODE = 300 * 1024;
const TOTAL = 1200 * 1024;

function storageKey(): string {
  const sid = localStorage.getItem('automind_sid') || 'default';
  const suffix = localStorage.getItem('automind_ws_suffix') || '';
  return 'automind_msgs_' + sid + suffix;
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
