// 会话内容状态（Zustand）：按模式独立的消息列表 + 流式/执行面板中间态。
// 结构化消息（而非旧版的 innerHTML 快照）持久化到 localStorage，
// 按 会话ID+工作区 隔离；单模式 300KB / 总量 1.2MB 截断（保留最新）。
import { create } from 'zustand';
import type { Mode } from './app';

export interface TraceItem {
  label: string; body: string; kind: string;   // body 为已渲染安全 HTML
}
export interface PlanRow { text: string; state: 'pending' | 'run' | 'ok' | 'fail'; error?: string }
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
  | { kind: 'resume'; id: string; why: string };

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
  removeKind: (mode: Mode, kinds: string[]) => void;
  clearMode: (mode: Mode) => void;
  setMessages: (mode: Mode, items: ChatItem[]) => void;
  reload: () => void;
  setTaskMode: (m: Mode | null) => void;
  setLastTask: (t: { text: string; mode: Mode } | null) => void;
  setPendingImages: (imgs: string[]) => void;
  setInputDraft: (s: string) => void;
  persist: () => void;
}

export const useChat = create<ChatState>((set, get) => ({
  messages: loadPersisted(),
  taskMode: null,
  lastTask: null,
  pendingImages: [],
  inputDraft: '',

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

  remove: (mode, id) => {
    set((s) => ({
      messages: { ...s.messages, [mode]: (s.messages[mode] || []).filter((i) => i.id !== id) },
    }));
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

  reload: () => set({ messages: loadPersisted() }),

  setTaskMode: (m) => set({ taskMode: m }),
  setLastTask: (t) => set({ lastTask: t }),
  setPendingImages: (imgs) => set({ pendingImages: imgs }),
  setInputDraft: (v) => set({ inputDraft: v }),

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
}));
