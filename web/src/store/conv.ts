// 会话目录（v1.7.6）：工作区内多会话 + 归档 + 上下文延续。
//
// 会话的"内容"仍按原有方式存放（本地 localStorage 按会话键隔离，服务端按 sid
// 隔离历史与 Agent 克隆）；这里只管"目录"：有哪些会话、叫什么、是否归档、
// 当前打开的是哪个。默认会话（后缀为空）就是升级前那唯一一条，内容原样保留。
import { message } from 'antd';
import { create } from 'zustand';
import { apiDelete, apiGet, apiPatch, apiPost, errText, SID } from '../api/client';
import { useApp } from './app';
import { dropConversationStorage, readConversationMessages, useChat, type ChatItem } from './chat';
import { usePanel } from './panel';
import { setTranscriptProvider } from '../lib/overrides';

export interface Conversation {
  sid: string;
  suffix: string;
  title: string;
  pinned?: boolean;
  archived: boolean;
  created: number;
  updated: number;
  turns: number;
  summary?: string;
  running?: boolean;
}

interface ConvState {
  items: Conversation[];
  loaded: boolean;
  loading: boolean;
  error: string;
  load: () => Promise<void>;
  create: (opts?: { continueFrom?: Conversation; title?: string }) => Promise<void>;
  switchTo: (c: Conversation | string) => boolean;
  rename: (c: Conversation, title: string) => Promise<void>;
  togglePin: (c: Conversation) => Promise<void>;
  archive: (c: Conversation) => Promise<void>;
  unarchive: (c: Conversation) => Promise<void>;
  remove: (c: Conversation) => Promise<void>;
}

const scope = () => ({ owner: SID, workspace: useApp.getState().wsSuffix || '' });

/** 把某会话的本地消息压成 [{role, content}]（归档摘要用；过程轨迹不带）。 */
function transcriptOf(suffix: string): { role: string; content: string }[] {
  const byMode = readConversationMessages(suffix);
  const out: { role: string; content: string }[] = [];
  for (const items of Object.values(byMode)) {
    for (const i of (items || []) as ChatItem[]) {
      if (i.kind === 'msg' && i.md && i.md.trim()) {
        out.push({ role: i.role === 'user' ? 'user' : 'assistant', content: i.md.slice(0, 1500) });
      }
    }
  }
  return out.slice(-60);
}

// v1.11.2：@对话 引用时，把那个对话的记录随消息带给服务端
setTranscriptProvider(transcriptOf);

function resetPanels() {
  usePanel.getState().setPlan(null);
  usePanel.getState().setStats({ steps: 0, backtracks: 0, tokens: 0, duration_ms: 0 });
  // v1.11.10：缓存命中 / 上下文是"本次任务"的量，切到别的会话后不能还挂着上一段对话的数字
  usePanel.getState().resetUsage();
  // v1.11.11：提问弹窗与子智能体属于"刚才那个会话/任务"；待办由 ws.ts 在切会话时
  // 重新拉取（bindSession → loadTodos），这里先清掉旧清单，避免新会话闪一眼别人的待办
  usePanel.getState().clearQuestion();
  usePanel.getState().clearSubagents();
  usePanel.getState().clearTodos();
  usePanel.getState().setCompactBusy(false);
}

export const useConv = create<ConvState>((set, get) => ({
  items: [],
  loaded: false,
  loading: false,
  error: '',

  load: async () => {
    set({ loading: true });
    try {
      const { owner, workspace } = scope();
      const d = await apiGet(`/conversations?owner=${encodeURIComponent(owner)}&workspace=${encodeURIComponent(workspace)}`);
      const items: Conversation[] = d.conversations || [];
      set({ items, loaded: true, loading: false, error: '' });
      // 本地记着的会话在服务端已不存在（被另一个标签页删了）→ 退回默认会话
      const cur = useApp.getState().convSuffix;
      if (cur && !items.some((c) => c.suffix === cur)) get().switchTo('');
    } catch (e) {
      set({ loading: false, error: errText(e) });
    }
  },

  create: async (opts) => {
    if (useApp.getState().running) { message.warning('任务执行中，请等它结束或先停止，再新建会话'); return; }
    // 当前就是一个还没用过的新会话 → 直接用它，不再堆出一串空的「新会话」
    if (!opts?.continueFrom && !opts?.title) {
      const cur = get().items.find((c) => c.suffix === useApp.getState().convSuffix);
      const hasMsgs = Object.values(useChat.getState().messages)
        .some((list) => (list || []).some((i) => i.kind === 'msg'));
      if (cur && cur.suffix && !cur.turns && !hasMsgs) {
        useApp.getState().setView('chat');
        message.info('当前已是一个空白的新会话，直接输入即可', 2);
        return;
      }
    }
    try {
      const body: any = { ...scope(), title: opts?.title || '' };
      if (opts?.continueFrom) {
        body.continue_from = opts.continueFrom.sid;
        body.transcript = transcriptOf(opts.continueFrom.suffix);
      }
      const d = await apiPost('/conversations', body);
      const c: Conversation = d.conversation;
      set((s) => ({ items: [c, ...s.items] }));
      get().switchTo(c);
      if (opts?.continueFrom) {
        message.success(d.conversation?.seeded
          ? '已新建延续会话：上一段的要点会在第一轮自动带给 Agent'
          : '已新建延续会话（源会话内容较少，未生成要点）');
      }
    } catch (e) {
      message.error(`新建会话失败：${errText(e)}`);
    }
  },

  switchTo: (c) => {
    const suffix = typeof c === 'string' ? c : c.suffix;
    const app = useApp.getState();
    if (suffix === app.convSuffix) { app.setView('chat'); return true; }
    if (app.running) {
      message.warning('当前会话有任务在执行，请等它结束或先停止，再切换会话');
      return false;
    }
    useChat.getState().persist();
    useChat.getState().persistSide();
    app.setConversation(suffix);
    useChat.getState().reload();
    resetPanels();
    app.setView('chat');
    return true;
  },

  rename: async (c, title) => {
    const t = title.trim();
    if (!t || t === c.title) return;
    try {
      const d = await apiPatch(`/conversations/${encodeURIComponent(c.sid)}`, { owner: SID, title: t });
      set((s) => ({ items: s.items.map((x) => (x.sid === c.sid ? { ...x, ...d.conversation } : x)) }));
    } catch (e) { message.error(`重命名失败：${errText(e)}`); }
  },

  togglePin: async (c) => {
    try {
      await apiPatch(`/conversations/${encodeURIComponent(c.sid)}`, { owner: SID, pinned: !c.pinned });
      await get().load();
    } catch (e) { message.error(`操作失败：${errText(e)}`); }
  },

  archive: async (c) => {
    if (c.running || (useApp.getState().running && c.suffix === useApp.getState().convSuffix)) {
      message.warning('该会话有任务正在执行，请先停止再归档'); return;
    }
    const hide = message.loading('正在整理会话要点并写入项目记忆…', 0);
    try {
      const d = await apiPost(`/conversations/${encodeURIComponent(c.sid)}/archive`,
        { owner: SID, transcript: transcriptOf(c.suffix) }, 90_000);
      hide();
      message.success(d.memorized
        ? `已归档「${c.title}」，要点已写入本工作区的长期记忆`
        : `已归档「${c.title}」`);
      await get().load();
      // 归档的正是当前会话 → 开一个新会话接着用，免得在"已归档"的会话里继续写
      // （它的要点已进项目记忆，新会话第一轮会自动带上）
      if (c.suffix === useApp.getState().convSuffix) await get().create();
    } catch (e) {
      hide();
      message.error(`归档失败：${errText(e)}`);
    }
  },

  unarchive: async (c) => {
    try {
      await apiPatch(`/conversations/${encodeURIComponent(c.sid)}`, { owner: SID, archived: false });
      await get().load();
      message.success(`已恢复「${c.title}」`);
    } catch (e) { message.error(`恢复失败：${errText(e)}`); }
  },

  remove: async (c) => {
    if (!c.suffix) { message.info('默认会话不能删除；可以用顶栏「🧹 清空」清掉内容'); return; }
    if (c.running) { message.warning('该会话有任务正在执行，请先停止再删除'); return; }
    try {
      await apiDelete(`/conversations/${encodeURIComponent(c.sid)}?owner=${encodeURIComponent(SID)}`);
      dropConversationStorage(c.suffix);
      if (c.suffix === useApp.getState().convSuffix) get().switchTo('');
      set((s) => ({ items: s.items.filter((x) => x.sid !== c.sid) }));
      message.info(`已删除「${c.title}」`);
    } catch (e) { message.error(`删除失败：${errText(e)}`); }
  },
}));

/** "3 分钟前 / 昨天 / 9月12日" —— 会话列表的活动时间。 */
export function relTime(ts: number): string {
  if (!ts) return '';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 172800) return '昨天';
  const t = new Date(ts * 1000);
  return `${t.getMonth() + 1}月${t.getDate()}日`;
}
