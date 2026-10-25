// 对话工作台：消息流（各类气泡）+ 输入区（附件/语音/发送/停止）。
// 首次进入对话模式时从服务端恢复历史；其它模式恢复本地持久化内容。
import { App, type MenuProps } from 'antd';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import {
  describeResumable, dismissResumable, EMPTY_OVERRIDES, fetchResumable, hasOverrides,
  isTextFile, MAX_ATTACH_BYTES, MODE_ICON, pendingChips, stripResumePrefix,
  TEXT_RESUME_PREFIX, toggleIn, toWire, RESUMABLE_ICON,
  type PendingOverrides, type ResumableInfo,
  liveMentions, mentionKey, mentionLabel, type Mention,
} from '../../lib/overrides';
import { buildCommands } from '../../lib/commands';
import { wireModelOverrides } from '../../lib/modelChoice';
import { useConv } from '../../store/conv';
import { useRefs } from '../../store/refs';
import { useModelChoice } from '../../store/modelChoice';
import { chatSid, MODE_FEATURE, MODE_LABELS, useApp, workspaceSid, type Mode } from '../../store/app';
import { uid, useChat, type ChatItem } from '../../store/chat';
import { usePanel } from '../../store/panel';
import { useUi } from '../../store/ui';
import { sendCompact, sendInterject, sendRun, sendStop, wsReady } from '../../ws';
import {
  DeliverableBubble, ErrorBubble, ExecBubble, LoopBubble, MsgBubble, MultiBubble, ResumeBubble,
  StreamBubble, TypingBubble, WelcomeBubble, WorkflowBubble, WorkspaceBubble,
} from './Bubbles';
import { TEMPLATES } from '../../lib/templates';
import TaskProgress from './TaskProgress';
import SafeDropdown from '../ui/SafeDropdown';
import ModelPicker from './ModelPicker';
import ComposerSuggest, {
  detectTrigger, type SuggestHandle, type SuggestItem, type Trigger,
} from './ComposerSuggest';

// zustand v5 的 getSnapshot 需返回稳定引用：空列表复用同一常量，
// 否则每次渲染生成新数组会触发 React #185（无限重渲染）。
const EMPTY_ITEMS: ChatItem[] = [];

//: 默认只渲染最近这么多条；更早的折起来，点一下再放出一批。
//  聊到 100+ 条时，全量渲染意味着每来一个流式分片浏览器都要把上百个气泡
//  （每个都含已渲染的 Markdown DOM）重新排版一遍 —— 滚动和打字都会卡。
const WINDOW_STEP = 40;

export default function ChatPanel() {
  const { message } = App.useApp();
  const mode = useApp((s) => s.mode);
  const running = useApp((s) => s.running);
  const items = useChat((s) => s.messages[mode] ?? EMPTY_ITEMS);
  const pendingImages = useChat((s) => s.pendingImages);
  const modalOpen = useUi((s) => s.modal !== null || s.preview !== null);
  const wsSuffix = useApp((s) => s.wsSuffix);
  const convSuffix = useApp((s) => s.convSuffix);
  // 面板刷新信号（任务完成/压缩等）：断点横幅跟着对一次账，保证预算耗尽后能出现
  const refreshTick = usePanel((s) => s.refreshTick);
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [recognizing, setRecognizing] = useState(false);
  // v1.7.7 本条消息的 overrides（模式 / 专家 / 附件），发送后清空
  const [ov, setOv] = useState<PendingOverrides>(EMPTY_OVERRIDES);
  const [experts, setExperts] = useState<{ id: string; name: string; icon: string }[] | null>(null);
  // v1.11.0 引用入口：MCP 连接器 / 技能（与专家同构，打开菜单时拉一次）
  const [mcps, setMcps] = useState<{ name: string; connected: boolean; tools: number }[] | null>(null);
  const [skills, setSkills] = useState<{ name: string; emoji: string; type: string; description: string }[] | null>(null);
  // v1.11.0 常驻引用：计划模式 / 长期目标（按工作区持久化，不随发送清空）
  const planMode = useRefs((s) => s.planMode);
  const stickyGoal = useRefs((s) => s.goal);
  const goals = useRefs((s) => s.goals);
  // 本会话最近一次任务没跑完 → 顶部提示「从断点续跑」
  const [resumable, setResumable] = useState<ResumableInfo | null>(null);
  const recRef = useRef<any>(null);
  // v1.11.2 输入框 / 指令与 @ 引用
  const [trigger, setTrigger] = useState<Trigger | null>(null);
  const [toolList, setToolList] = useState<{ name: string; description: string; source: string }[] | null>(null);
  const sugRef = useRef<SuggestHandle>(null);
  const convItems = useConv((s) => s.items);
  const saveTimer = useRef<number | undefined>(undefined);
  // 只在挂载时读一次草稿：textarea 是非受控的，若改成订阅 inputDraft，
  // 每次按键都会重渲整个消息列表，长会话下打字会明显卡顿。
  const draft0 = useRef(useChat.getState().inputDraft).current;

  // 纯视觉：输入框为空且没有附件/图片时把发送键调暗（点击行为不变）。
  // 文本框是非受控的（见 draft0 的说明），这里直接写 data 属性而不是走 state，
  // 避免每个按键都重渲整个消息列表。
  const markEmpty = () => {
    const el = composerRef.current;
    if (!el) return;
    const st = useChat.getState();
    const empty = !(taRef.current?.value || '').trim() && !st.pendingImages.length;
    el.dataset.empty = empty && !hasOverrides(ov) ? '1' : '0';
  };

  // 草稿保存：输入停顿 400ms 落一次盘，避免每个按键都写 localStorage。
  const saveDraft = (immediate = false) => {
    window.clearTimeout(saveTimer.current);
    const flush = () => useChat.getState().setInputDraft(taRef.current?.value || '');
    if (immediate) flush();
    else saveTimer.current = window.setTimeout(flush, 400);
  };

  // 关窗/切标签/组件卸载时立刻补存一次 —— 否则最后 400ms 内敲的字会丢，
  // 而"最后敲的那几个字"恰恰是用户最在意的部分。
  useEffect(() => {
    const flush = () => saveDraft(true);
    window.addEventListener('beforeunload', flush);
    document.addEventListener('visibilitychange', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
      document.removeEventListener('visibilitychange', flush);
      flush();
      window.clearTimeout(saveTimer.current);
    };
  }, []);

  // 切工作区时把输入框换成该工作区自己的草稿。
  // textarea 是非受控的，`defaultValue` 只在挂载时生效，而 v1.4.0 起 ChatPanel
  // 常驻挂载不再重建 —— 不手动同步的话，切过去看到的还是上一个工作区的草稿，
  // 接着一打字又会被存到新工作区名下。
  // 先撤掉待触发的防抖保存：它读的是旧文本，而落盘的 key 已经是新工作区了。
  useEffect(() => { useRefs.getState().reloadScope(); }, [wsSuffix]);

  useEffect(() => {
    window.clearTimeout(saveTimer.current);
    if (taRef.current) {
      taRef.current.value = useChat.getState().inputDraft;
      taRef.current.style.height = 'auto';
    }
    markEmpty();
  }, [wsSuffix, convSuffix]);

  useEffect(() => { markEmpty(); }, [pendingImages, ov]);

  // 打开即聚焦，省掉"先点一下输入框"这一步；任务跑完后也自动交还焦点。
  // 有弹窗时不抢焦点（否则弹窗里的输入框会被顶掉）。
  useEffect(() => {
    if (modalOpen || running) return;
    const t = window.setTimeout(() => taRef.current?.focus({ preventScroll: true }), 60);
    return () => window.clearTimeout(t);
  }, [mode, running, modalOpen]);

  // 对话模式且本地无记录 → 从服务端恢复历史
  useEffect(() => {
    const st = useChat.getState();
    if (mode === 'chat' && (st.messages.chat || []).length === 0) {
      apiGet(`/chat/history?session_id=${encodeURIComponent(chatSid())}`).then((h) => {
        const msgs = (h.messages || []).filter((m: any) => m.role === 'user' || m.role === 'assistant');
        if (msgs.length) {
          st.setMessages('chat', msgs.map((m: any): ChatItem => ({
            kind: 'msg', id: uid(), role: m.role === 'user' ? 'user' : 'agent',
            md: typeof m.content === 'string' ? m.content
              : (Array.isArray(m.content) ? m.content.filter((p: any) => p.type === 'text').map((p: any) => p.text).join('') : ''),
          })));
        }
      }).catch((e) => {
        // 历史拉不回来时**必须说**：静默失败会让人以为"聊天记录没了"，
        // 而实际只是服务没起来 / 令牌过期，刷新一下就有。
        message.error(`会话历史加载失败：${e?.friendly || e?.message || e}（本地内容不受影响）`);
      });
    }
  }, [mode, wsSuffix, convSuffix]);

  // 自动滚动到底
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  // override 用于续跑/重试这类"不是从输入框来"的发送。
  // 模式一律从 store 现取：续跑可能刚切过模式，闭包里的 mode 已经是旧值。
  const send = async (override?: string) => {
    if (useApp.getState().running) return;
    const st = useChat.getState();
    const cur = useApp.getState().mode;
    const text = (override ?? taRef.current?.value ?? '').trim();
    const images = st.pendingImages.slice();
    // 输入框直接发送时带上 overrides；续跑/重试等"不是从输入框来"的发送不带。
    // 计划模式 / 长期目标是常驻状态：任何一条新消息都带上（直到用户退出 / 摘掉）
    const sticky = useRefs.getState();
    // v1.11.15 模型选择同样是常驻的（store/modelChoice）：输入框发送、续跑、重试
    // 都带上；选"默认"时 wire 为 undefined，服务端保持原有的模式模型 + 默认路由行为。
    const modelWire = wireModelOverrides(useModelChoice.getState().choice);
    const modelPend: PendingOverrides['model'] = modelWire
      ? (modelWire.model_mode === 'manual'
        ? { mode: 'manual', provider: modelWire.provider, model: modelWire.model }
        : { mode: 'auto' })
      : undefined;
    const pend: PendingOverrides = {
      ...(override === undefined ? ov : EMPTY_OVERRIDES),
      ...(sticky.planMode ? { plan: true } : {}),
      ...(sticky.goal ? { goal: sticky.goal } : {}),
      ...(modelPend ? { model: modelPend } : {}),
    };
    // 输入框里已经删掉的 @ 引用不再发送（删字 = 不要它了）
    if (pend.mentions?.length) pend.mentions = liveMentions(text, pend.mentions);
    if (!text && !images.length && !pend.attachments.length) return;
    setTrigger(null);
    if (!wsReady()) { message.error('未连接到服务器，请稍候重试'); return; }
    if (override === undefined && taRef.current) {
      taRef.current.value = '';
      taRef.current.style.height = 'auto';
    }
    window.clearTimeout(saveTimer.current);   // 别让待触发的保存把草稿又写回来
    st.setInputDraft('');
    st.setPendingImages([]);
    const msgId = uid();
    const chips = pendingChips(pend);
    const body = text || (pend.attachments.length ? '请阅读附件内容' : '');
    st.append(cur, { kind: 'msg', id: msgId, role: 'user', md: body, images,
      ...(chips.length ? { chips } : {}) });
    st.setTaskMode(cur);
    // lastTask 只存**原始**文本：此前存的是已套了续跑前缀的文本，再续跑一次就套两层
    st.setLastTask({ text: stripResumePrefix(body), mode: pend.mode || cur });
    useApp.getState().setRunning(true);
    st.append(cur, { kind: 'typing', id: uid() });
    const wire = toWire(pend);
    sendRun(body, images, { ref: msgId, ...(wire ? { overrides: wire } : {}) });
    if (hasOverrides(pend)) setOv(EMPTY_OVERRIDES);
    setResumable(null);
  };

  // 断点续跑（v1.7.7；v1.11.16 支持 plan/react/multi/loop 四种形态）：服务端从
  // 检查点接着跑 —— 计划只跑剩余目标、ReAct 带着已完成动作、multi 跳过已完成
  // role、loop 从下一迭代继续。任务原文由服务端取自断点，这里只负责展示与发起。
  const resumeFromCheckpoint = async (r: ResumableInfo) => {
    if (useApp.getState().running) { message.error('任务正在执行中'); return; }
    if (!wsReady()) { message.error('未连接到服务器，请稍候重试'); return; }
    // v1.11.16：断点可能属于另一种模式（multi/loop/…）。先切到那个模式，
    // 否则多智能体/循环的进度面板会画在当前模式的会话视图里，看起来像丢了。
    const target = (r.interaction || r.run_mode || '') as Mode;
    if (target && MODE_LABELS[target] && target !== useApp.getState().mode) {
      await useApp.getState().setMode(target);
    }
    const st = useChat.getState();
    const cur = useApp.getState().mode;
    const msgId = uid();
    st.append(cur, {
      kind: 'msg', id: msgId, role: 'user', md: `↻ 从断点续跑：${r.task}`,
      chips: [{ key: 'resume', label: `↻ ${describeResumable(r)}`, state: 'ok' }],
    });
    st.setTaskMode(cur);
    st.setLastTask({ text: stripResumePrefix(r.task), mode: (r.interaction as Mode) || cur });
    useApp.getState().setRunning(true);
    st.append(cur, { kind: 'typing', id: uid() });
    // v1.11.15 续跑也按当前模型选择跑（服务端会保留续跑的模型覆盖）
    const modelWire = wireModelOverrides(useModelChoice.getState().choice);
    sendRun(r.task, [], {
      ref: msgId, resume_checkpoint: r.id,
      ...(r.interaction ? { interaction: r.interaction } : {}),
      ...(modelWire ? { overrides: modelWire } : {}),
    });
    setResumable(null);
  };

  /**
   * 执行中把这句话"插"进正在跑的那一轮（服务端 interject）。
   *
   * 与 send() 的三点关键差别：不清 pendingImages、不动 lastTask、不置 running ——
   * 这一轮的执行状态是既有的，插一句话不该把它当成新一轮从头开始。极端情况下
   * 服务端发现当时并没有任务在跑，会把它提升成一次新任务（interjection_promoted
   * → task_start），届时由 task_start 自己把 running 置上，这里不用预判。
   */
  const interject = (fromButton = false) => {
    const text = (taRef.current?.value ?? '').trim();
    if (!text) {
      // 点按钮却什么都没发生最让人困惑；键盘回车敲在空框里则不必每次都弹提示
      if (fromButton) message.info('先输入要补充的内容');
      return;
    }
    if (!wsReady()) { message.error('未连接到服务器，请稍候重试'); return; }
    const st = useChat.getState();
    const cur = useApp.getState().mode;
    const id = uid();
    // 先把气泡落下来：用户按下这一下之后必须马上看得见自己说了什么；同时这个 id
    // 就是后续 interjection_applied/dropped 认领气泡的凭据（见 ws.ts 的登记表）。
    st.append(cur, { kind: 'msg', id, role: 'user', md: text, injected: true });
    if (taRef.current) {
      taRef.current.value = '';
      taRef.current.style.height = 'auto';
    }
    window.clearTimeout(saveTimer.current);   // 别让待触发的保存把草稿又写回来
    st.setInputDraft('');
    if (st.pendingImages.length) {
      // 补充只发文字。悄悄把图一起发出去，用户会以为图片还留在待发区。
      message.info('补充只发送文字，已附加的图片会留给下一条正式消息');
    }
    sendInterject(text, id);
  };

  // 从失败卡片重跑/续跑：任务原文取自卡片自身，重启应用后依然有效。
  //
  // wrap=true 并非"断点续传"——后端没有把执行进度回传给前端，这里能做的只是在
  // 原任务前加一段提示，让模型自己先去核对现状。措辞用"尽量/若已完成则跳过"
  // 而不是"不要重做"，因为能不能真跳过取决于模型的核对结果，不该承诺死。
  //
  // v1.7.7：「继续此任务」优先走**真正的断点续跑**（服务端检查点）；只有找不到
  // 可用断点（对话模式、断点已被新任务取代、服务端重装过）时才退回文本提示。
  // 退回时先剥掉已有前缀 —— 此前连续续跑会把前缀嵌套成两层、三层。
  const runAgain = async (text: string, m: Mode, wrap: boolean) => {
    const raw = stripResumePrefix(text || '');
    if (!raw) { message.error('没有可继续的任务'); return; }
    if (useApp.getState().running) { message.error('任务正在执行中'); return; }
    if (wrap) {
      const r = await fetchResumable(chatSid());
      if (r && stripResumePrefix(r.task).trim() === raw.trim()) {
        if (m !== useApp.getState().mode) await useApp.getState().setMode(m);
        await resumeFromCheckpoint(r);
        return;
      }
      message.info('没有找到这次任务的断点记录，将让 Agent 先核对现状再接着做', 3);
    }
    if (m !== useApp.getState().mode) await useApp.getState().setMode(m);
    await send(wrap ? TEXT_RESUME_PREFIX + raw : raw);
  };

  // 进入会话 / 任务结束 / 面板刷新时查一次：本会话最近一次任务若没跑完，提示可续跑。
  // v1.11.16：去掉"对话模式不查"的旧限制 —— 断点可能属于 work/multi/loop，
  // 用户切到对话模式后照样要能看到「继续」入口；四种 kind 的文案见 describeResumable。
  // refreshTick 覆盖"任务因预算耗尽而结束"：task_complete 会 bumpRefresh，横幅必然对账。
  useEffect(() => {
    if (running) { setResumable(null); return; }
    let alive = true;
    fetchResumable(chatSid()).then((r) => { if (alive) setResumable(r); });
    return () => { alive = false; };
  }, [running, wsSuffix, convSuffix, refreshTick]);

  // 编辑后重发：先砍掉这条及其之后的消息（基于旧问题的回答已经失效，
  // 留着会让上下文自相矛盾），再把改好的内容作为一条新消息发出去。
  const resendEdited = async (id: string, text: string) => {
    if (useApp.getState().running) { message.error('任务正在执行中，请先停止'); return; }
    useChat.getState().truncateFrom(mode, id);
    await send(text);
  };

  const resume = async (m: Mode) => {
    const last = useChat.getState().lastTask;
    if (!last || !last.text) { message.error('没有可继续的任务'); return; }
    await runAgain(last.text, last.mode, true);
  };

  // 📎 附件：图片走多模态 images 通道；文本/代码文件读成文本随 overrides 发送；
  // 其余类型（PDF/Word…）明确告知去知识库，而不是静默忽略
  const pickFiles = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    const skipped: string[] = [];
    files.forEach((f) => {
      if (f.type.startsWith('image/')) {
        if (f.size > 8 * 1024 * 1024) { message.error(`图片 ${f.name} 超过 8MB`); return; }
        const reader = new FileReader();
        reader.onload = (ev) => {
          const st = useChat.getState();
          st.setPendingImages([...st.pendingImages, ev.target?.result as string]);
        };
        reader.readAsDataURL(f);
        return;
      }
      if (!isTextFile(f)) { skipped.push(f.name); return; }
      if (f.size > MAX_ATTACH_BYTES) { message.error(`${f.name} 超过 1MB，请改用知识库上传`); return; }
      f.text().then((text) => setOv((o) => ({
        ...o,
        attachments: [...o.attachments.filter((a) => a.name !== f.name), { name: f.name, text, size: f.size }]
          .slice(0, 10),
      })));
    });
    if (skipped.length) {
      message.warning(`${skipped.join('、')} 不是文本文件，无法直接附加；PDF / Word 等请上传到「📚 知识库」`, 5);
    }
    e.target.value = '';
  };

  const loadExperts = async () => {
    if (experts) return;
    try {
      const d = await apiGet(`/experts?session_id=${encodeURIComponent(workspaceSid())}`);
      setExperts((d.installed || []).map((x: any) => ({ id: x.id, name: x.name, icon: x.icon || '🎓' })));
    } catch { setExperts([]); }
  };

  const loadRefs = async () => {
    if (mcps === null) {
      apiGet('/mcp').then((d) => setMcps((d.servers || []).map((s: any) => ({
        name: s.name, connected: !!s.connected && (s.tools || []).length > 0, tools: (s.tools || []).length,
      })))).catch(() => setMcps([]));
    }
    if (skills === null) {
      apiGet('/skills').then((d) => setSkills((Array.isArray(d) ? d : []).map((s: any) => ({
        name: s.name, emoji: s.emoji || '🧩', type: s.type || 'python', description: s.description || '',
      })))).catch(() => setSkills([]));
    }
    if (useRefs.getState().goals === null) useRefs.getState().loadGoals().catch(() => { /* 菜单里显示为空 */ });
  };

  // ── v1.11.2 / 指令与 @ 引用 ─────────────────────────────
  const loadTools = () => {
    if (toolList !== null) return;
    apiGet('/tools').then((d) => setToolList((Array.isArray(d) ? d : d.tools || [])
      .filter((t: any) => t.enabled !== false)
      .map((t: any) => ({ name: t.name, description: t.description || '', source: t.source || 'builtin' }))))
      .catch(() => setToolList([]));
  };

  /** 按光标前的文字重新判断要不要弹指令 / 引用面板。 */
  const updateTrigger = () => {
    const el = taRef.current;
    if (!el) return;
    const t = detectTrigger(el.value, el.selectionStart ?? el.value.length);
    setTrigger((old) => (old?.kind === t?.kind && old?.query === t?.query && old?.start === t?.start ? old : t));
    if (t?.kind === 'slash') { loadRefs(); loadExperts(); loadTools(); }
  };

  /** 把 [start, end) 换成 text，光标停在插入内容之后。 */
  const replaceRange = (start: number, end: number, text: string) => {
    const el = taRef.current;
    if (!el) return;
    el.value = el.value.slice(0, start) + text + el.value.slice(end);
    const pos = start + text.length;
    el.focus();
    el.setSelectionRange(pos, pos);
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 180) + 'px';
    saveDraft();
    markEmpty();
  };

  const commandItems = buildCommands({
    featureOn: useApp.getState().featureOn, modeFeature: MODE_FEATURE, currentMode: mode, planMode,
    skills, mcps, experts, tools: toolList,
    picked: { skills: ov.skills || [], mcp: ov.mcp || [], tools: ov.tools || [] },
  });
  const curConv = useApp.getState().convSuffix;
  const mentionConvs: SuggestItem[] = convItems
    .filter((c) => c.suffix !== curConv && (c.turns || c.summary))
    .map((c) => ({
      key: 'conv:' + c.sid, group: '对话', icon: c.archived ? '🗄' : '💬', title: c.title,
      desc: c.summary ? c.summary.slice(0, 80) : `${c.turns || 0} 轮`,
      hint: c.archived ? '已归档' : '', keywords: 'conversation 对话 会话',
      data: { type: 'conversation', sid: c.sid, suffix: c.suffix, title: c.title },
    }));

  const runAction = (action: string, rest: string) => {
    const ui = useUi.getState();
    switch (action) {
      case 'new': window.dispatchEvent(new CustomEvent('automind:new-session')); break;
      case 'clear': window.dispatchEvent(new CustomEvent('automind:clear')); break;
      // v1.11.11 /compact：压缩当前会话上下文。结果由 ws.ts 的 compact_result 统一提示，
      // 这里只负责发起（busy 时 sendCompact 自己会提示"进行中"，不会重复推送）。
      case 'compact': sendCompact(); break;
      case 'plan': togglePlan(); break;
      case 'goal': ui.openModal('goals'); break;
      case 'schedule': ui.openSchedule(rest); break;
      case 'workflow': ui.openModal('runWorkflow'); break;
      case 'templates': ui.openModal('templates'); break;
      case 'browser': {
        const p = usePanel.getState();
        p.setRightTab('browser');
        if (p.rightCollapsed) p.toggleRight();
        break;
      }
      case 'theme': useApp.getState().toggleTheme(); break;
      default: ui.openModal(action as any);
    }
  };

  const onPick = (it: SuggestItem, t: Trigger) => {
    const d = it.data || {};
    setTrigger(null);
    if (t.kind === 'mention') {
      const m: Mention = d.type === 'conversation'
        ? { type: 'conversation', sid: d.sid, suffix: d.suffix, title: d.title, token: `@${d.title}` }
        : { type: d.type, path: d.path, token: `@${d.path}` };
      replaceRange(t.start, t.end, m.token + ' ');
      setOv((o) => ({
        ...o,
        mentions: [...(o.mentions || []).filter((x) => mentionKey(x) !== mentionKey(m)), m],
      }));
      return;
    }
    if (d.kind === 'prompt') { replaceRange(t.start, t.end, d.text); return; }
    if (d.kind === 'action') {
      // /schedule 后面写的内容带进定时任务；其它动作把指令文字删掉
      const el = taRef.current;
      const rest = el ? (el.value.slice(0, t.start) + el.value.slice(t.end)).trim() : '';
      replaceRange(t.start, t.end, '');
      runAction(d.action, rest);
      return;
    }
    replaceRange(t.start, t.end, '');
    if (d.kind === 'mode') { setOv((o) => ({ ...o, mode: d.mode === mode ? undefined : d.mode })); return; }
    if (d.ref === 'skill') setOv((o) => ({ ...o, skills: toggleIn(o.skills, d.name) }));
    else if (d.ref === 'mcp') setOv((o) => ({ ...o, mcp: toggleIn(o.mcp, d.name) }));
    else if (d.ref === 'tool') setOv((o) => ({ ...o, tools: toggleIn(o.tools, d.name) }));
    else if (d.ref === 'expert') setOv((o) => ({ ...o, expert: { id: d.id, name: d.name } }));
  };

  /** 从 ＋ 菜单进入：在光标处补一个触发符，面板随即弹出。 */
  const startTrigger = (ch: '/' | '@') => {
    const el = taRef.current;
    if (!el) return;
    const pos = el.selectionStart ?? el.value.length;
    const before = el.value.slice(0, pos);
    const needSpace = ch === '@' ? before && !/\s$/.test(before) : before && !/\n$/.test(before);
    replaceRange(pos, pos, (needSpace ? (ch === '/' ? '\n' : ' ') : '') + ch);
    window.setTimeout(updateTrigger, 0);
  };

  const openGoals = () => useUi.getState().openModal('goals');
  const openSchedule = () => useUi.getState().openSchedule(taRef.current?.value || '');
  const togglePlan = () => {
    const on = !useRefs.getState().planMode;
    useRefs.getState().setPlanMode(on);
    message.info(on ? '已进入计划模式：每条消息先生成计划，你批准后才执行（输入框上的 ✕ 退出）'
      : '已退出计划模式：消息将直接执行', 3);
  };

  const featureOn = useApp((s) => s.featureOn);
  const openGoals_ = goals || [];
  // v1.11.2：＋ 菜单改为分组 + 双行条目（图标 · 标题 · 一句说明 · 快捷提示），
  // 此前是一列长短不一的单行文字，选项一多就找不到东西。
  const pm = (icon: string, title: React.ReactNode, desc?: React.ReactNode, kbd?: string, on?: boolean) => (
    <span className={'pm-item' + (on ? ' on' : '')}>
      <span className="pm-ico">{icon}</span>
      <span className="pm-text">
        <span className="pm-title">{title}</span>
        {desc && <span className="pm-desc">{desc}</span>}
      </span>
      {kbd && <kbd className="pm-kbd">{kbd}</kbd>}
      {on && <span className="pm-check">✓</span>}
    </span>
  );
  const sub = (icon: string, title: string, desc?: string, on?: boolean) => (
    <span className={'pm-sub' + (on ? ' on' : '')}>
      <span className="pm-ico">{icon}</span>
      <span className="pm-text">
        <span className="pm-title">{title}</span>
        {desc && <span className="pm-desc">{desc}</span>}
      </span>
      {on && <span className="pm-check">✓</span>}
    </span>
  );
  const countTag = (n?: number) => (n ? <span className="pm-count">{n}</span> : null);
  const plusMenu: MenuProps['items'] = [
    {
      key: 'g:input', type: 'group', label: '输入',
      children: [
        { key: 'attach', label: pm('📎', '添加附件', '图片 / 文本 / 代码文件'), onClick: () => fileRef.current?.click() },
        { key: 'mention', label: pm('＠', '引用文件或对话', '项目里的文件、目录或其它对话', '@'), onClick: () => startTrigger('@') },
        { key: 'slash', label: pm('⌘', '调用指令', '技能 · 工具 · 插件 · MCP · 操作', '/'), onClick: () => startTrigger('/') },
        {
          key: 'voice', label: pm(recognizing ? '⏹' : '🎤', recognizing ? '停止语音输入' : '语音输入', '浏览器语音转文字'),
          onClick: () => toggleVoice(),
        },
      ],
    },
    {
      key: 'g:ref', type: 'group', label: '本条消息',
      children: [
        {
          key: 'mode', popupClassName: 'plus-menu-pop',
          label: pm('🧭', <>本条模式{ov.mode && <span className="pm-count">{MODE_LABELS[ov.mode]}</span>}</>, '只对这一条生效'),
          children: (['chat', 'work', 'coding', 'multi', 'loop'] as Mode[]).map((m) => {
            const locked = !featureOn(MODE_FEATURE[m]);
            return {
              key: 'mode:' + m, disabled: locked,
              label: sub(MODE_ICON[m], `${MODE_LABELS[m]}${locked ? ' 🔒' : ''}`,
                m === mode ? '当前模式' : undefined, ov.mode === m),
              onClick: () => setOv((o) => ({ ...o, mode: m === mode ? undefined : m })),
            };
          }),
        },
        {
          key: 'expert', popupClassName: 'plus-menu-pop', onTitleClick: loadExperts,
          label: pm('🎓', <>专家{ov.expert && <span className="pm-count">{ov.expert.name || '不使用'}</span>}</>, '让某个领域专家来回答'),
          children: experts === null
            ? [{ key: 'exp:loading', label: sub('⏳', '加载中…'), disabled: true }]
            : [
              { key: 'exp:none', label: sub('🚫', '本条不使用专家', undefined, ov.expert?.id === ''), onClick: () => setOv((o) => ({ ...o, expert: { id: '', name: '' } })) },
              ...experts.map((x) => ({
                key: 'exp:' + x.id, label: sub(x.icon, x.name, undefined, ov.expert?.id === x.id),
                onClick: () => setOv((o) => ({ ...o, expert: { id: x.id, name: x.name } })),
              })),
              ...(experts.length ? [] : [{ key: 'exp:empty', label: sub('📭', '还没有安装专家', '去「专家市场」安装'), disabled: true }]),
            ],
        },
        {
          key: 'mcp', popupClassName: 'plus-menu-pop', onTitleClick: loadRefs,
          label: pm('🔌', <>MCP 连接器{countTag(ov.mcp?.length)}</>, '它的工具必定下发给模型'),
          children: mcps === null
            ? [{ key: 'mcp:loading', label: sub('⏳', '加载中…'), disabled: true }]
            : [
              ...mcps.map((s) => ({
                key: 'mcp:' + s.name, disabled: !s.connected,
                label: sub('🔌', s.name, s.connected ? `${s.tools} 个工具` : '未连接', ov.mcp?.includes(s.name)),
                onClick: () => setOv((o) => ({ ...o, mcp: toggleIn(o.mcp, s.name) })),
              })),
              ...(mcps.length ? [] : [{ key: 'mcp:empty', label: sub('📭', '还没有配置 MCP 连接器'), disabled: true }]),
              { type: 'divider' as const },
              { key: 'mcp:manage', label: sub('⚙', '管理连接器…'), onClick: () => useApp.getState().setView('tools') },
            ],
        },
        {
          key: 'skill', popupClassName: 'plus-menu-pop', onTitleClick: loadRefs,
          label: pm('🧩', <>技能{countTag(ov.skills?.length)}</>, '注入技能指令或调用可执行技能'),
          children: skills === null
            ? [{ key: 'skill:loading', label: sub('⏳', '加载中…'), disabled: true }]
            : [
              ...skills.map((s) => ({
                key: 'skill:' + s.name,
                label: sub(s.emoji, s.name, s.type === 'markdown' ? '指令型' : '可执行', ov.skills?.includes(s.name)),
                onClick: () => setOv((o) => ({ ...o, skills: toggleIn(o.skills, s.name) })),
              })),
              ...(skills.length ? [] : [{ key: 'skill:empty', label: sub('📭', '还没有安装技能'), disabled: true }]),
              { type: 'divider' as const },
              { key: 'skill:manage', label: sub('⚙', '管理技能…'), onClick: () => useApp.getState().setView('tools') },
            ],
        },
      ],
    },
    {
      key: 'g:work', type: 'group', label: '工作方式',
      children: [
        {
          key: 'plan',
          label: pm('📋', planMode ? '退出计划模式' : '计划模式', '先出计划，你批准后才执行', undefined, planMode),
          onClick: togglePlan,
        },
        {
          key: 'goal', popupClassName: 'plus-menu-pop', onTitleClick: loadRefs,
          label: pm('🎯', <>目标交付{stickyGoal && <span className="pm-count">{stickyGoal.title.slice(0, 8)}</span>}</>, '围绕长期目标持续推进'),
          children: [
            ...openGoals_.filter((g) => g.status === 'active' || g.status === 'paused').map((g) => ({
              key: 'goal:' + g.id,
              label: sub('🎯', g.title, g.status === 'paused' ? '已暂停' : undefined, stickyGoal?.id === g.id),
              onClick: () => useRefs.getState().setGoal(stickyGoal?.id === g.id ? null : { id: g.id, title: g.title }),
            })),
            ...(goals === null ? [{ key: 'goal:loading', label: sub('⏳', '加载中…'), disabled: true }] : []),
            ...(stickyGoal ? [{ key: 'goal:none', label: sub('✕', '不再围绕目标推进'), onClick: () => useRefs.getState().setGoal(null) }] : []),
            { type: 'divider' as const },
            { key: 'goal:manage', label: sub('＋', '新建 / 查看全部目标…'), onClick: openGoals },
          ],
        },
        {
          key: 'schedule',
          label: pm('⏰', `设为定时任务${featureOn('scheduler') ? '' : ' 🔒'}`, '按固定频率自动执行这段内容'),
          onClick: () => openSchedule(),
        },
        // v1.8.0：在对话里一步发起工作流，运行卡片出现在当前会话
        { key: 'workflow', label: pm('▶', '运行工作流', '在当前会话里发起一次工作流'), onClick: () => useUi.getState().openModal('runWorkflow') },
      ],
    },
    ...(hasOverrides(ov) ? [{ type: 'divider' as const },
      { key: 'clear', danger: true, label: pm('✕', '清除本条的全部设置'), onClick: () => setOv(EMPTY_OVERRIDES) }] : []),
  ];

  const toggleVoice = () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) { message.error('当前浏览器不支持语音识别，请使用 Chrome 或 Edge'); return; }
    if (recognizing) { recRef.current?.stop(); return; }
    const rec = new SR();
    rec.lang = 'zh-CN';
    rec.interimResults = true;
    const base = taRef.current?.value || '';
    rec.onstart = () => { setRecognizing(true); message.info('正在聆听...'); };
    rec.onerror = (ev: any) => message.error('语音识别失败: ' + ev.error);
    rec.onend = () => setRecognizing(false);
    rec.onresult = (ev: any) => {
      let txt = '';
      for (let i = 0; i < ev.results.length; i++) txt += ev.results[i][0].transcript;
      if (taRef.current) taRef.current.value = (base ? base + ' ' : '') + txt;
      saveDraft();   // 语音听写出来的内容同样算草稿，别在切走时丢掉
    };
    recRef.current = rec;
    rec.start();
  };

  const useTemplate = async (i: number) => {
    const t = TEMPLATES[i];
    if (!t) return;
    if (t.mode !== mode) await useApp.getState().setMode(t.mode as Mode);
    if (taRef.current) { taRef.current.value = t.prompt; taRef.current.focus(); saveDraft(true); }
    message.info('模板已填入，补充细节后按 Enter 发送');
  };

  const showWelcome = items.length === 0;
  // 可见窗口：切模式/切工作区时收回默认值，避免带着上一个会话展开的量过来
  const [windowSize, setWindowSize] = useState(WINDOW_STEP);
  useEffect(() => { setWindowSize(WINDOW_STEP); }, [mode, wsSuffix, convSuffix]);
  const hidden = Math.max(0, items.length - windowSize);
  const shown = hidden ? items.slice(hidden) : items;

  // 展开更早的消息时保持视线不动：新内容加在**上方**，若不补偿滚动位置，
  // 用户正看着的那条会被顶到屏幕外，读到一半的地方就找不回来了。
  const keepAnchor = useRef<number | null>(null);
  const loadOlder = () => {
    const el = listRef.current;
    keepAnchor.current = el ? el.scrollHeight - el.scrollTop : null;
    setWindowSize((n) => n + WINDOW_STEP * 2);
  };
  useLayoutEffect(() => {
    const el = listRef.current;
    if (el && keepAnchor.current != null) {
      el.scrollTop = el.scrollHeight - keepAnchor.current;
      keepAnchor.current = null;
    }
  }, [windowSize]);

  return (
    // minHeight: 0 不能省 —— flex 子项的自动最小尺寸是 auto，即"不小于内容高度"。
    // 少了它，消息一多本容器就被内容撑到上万像素、超出父容器，.messages 拿不到
    // 受限高度因而永远不触发 overflow-y:auto，输入框被顶到视口外彻底看不见。
    // （minWidth: 0 是同一条规则的横向版本，早已加上；纵向这条此前漏了。）
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0 }}>
      <div className="messages" ref={listRef} onClick={(e) => {
        // 执行轨迹里的网页截图是 innerHTML 渲染的，点击放大走事件委托
        const t = e.target as HTMLElement;
        if (t.tagName === 'IMG' && t.classList.contains('trace-shot')) {
          useUi.getState().openPreview({
            label: '网页截图',
            html: '<body style="margin:0;background:#111;display:flex;justify-content:center">'
              + `<img src="${(t as HTMLImageElement).src}" style="max-width:100%;height:auto" alt=""></body>`,
          });
        }
      }}>
        {showWelcome && (
          <WelcomeBubble onTemplate={useTemplate} onAllTemplates={() => useUi.getState().openModal('templates')} />
        )}
        {hidden > 0 && (
          <div className="msg-older">
            <button onClick={loadOlder}>
              ↑ 载入更早的消息（还有 {hidden} 条）
            </button>
          </div>
        )}
        {shown.map((item) => {
          switch (item.kind) {
            case 'msg': return (
              <MsgBubble
                key={item.id}
                item={item}
                onDelete={(id) => useChat.getState().remove(mode, id)}
                onResend={resendEdited}
              />
            );
            case 'stream': return <StreamBubble key={item.id} item={item} />;
            case 'typing': return <TypingBubble key={item.id} />;
            case 'exec': return <ExecBubble key={item.id} item={item} />;
            case 'multi': return <MultiBubble key={item.id} item={item} />;
            case 'loop': return <LoopBubble key={item.id} item={item} />;
            case 'resume': return <ResumeBubble key={item.id} item={item} onResume={() => resume(mode)} />;
            case 'workspace': return <WorkspaceBubble key={item.id} item={item} />;
            case 'workflow': return <WorkflowBubble key={item.id} item={item} />;
            case 'deliverable': return <DeliverableBubble key={item.id} item={item} />;
            case 'error': return (
              <ErrorBubble
                key={item.id}
                item={item}
                onResume={(it) => runAgain(it.task || '', it.taskMode || mode, true)}
                onRetry={(it) => runAgain(it.task || '', it.taskMode || mode, false)}
              />
            );
            default: return null;
          }
        })}
      </div>

      {pendingImages.length > 0 && (
        <div style={{ display: 'flex', gap: 8, padding: '6px 18px', flexWrap: 'wrap' }}>
          {pendingImages.map((u, i) => (
            <div key={i} style={{ position: 'relative' }}>
              <img src={u} style={{ height: 56, borderRadius: 8, border: '1px solid var(--border)' }} />
              <button
                onClick={() => {
                  const st = useChat.getState();
                  st.setPendingImages(st.pendingImages.filter((_, k) => k !== i));
                }}
                style={{
                  position: 'absolute', top: -6, right: -6, width: 18, height: 18, borderRadius: '50%',
                  border: 'none', background: 'var(--red)', color: '#fff', cursor: 'pointer', fontSize: 10,
                }}
              >✕</button>
            </div>
          ))}
        </div>
      )}

      <TaskProgress />

      {resumable && !running && (
        <div className="resume-banner">
          <span title={resumable.task}>
            {RESUMABLE_ICON[resumable.kind] || '⏸'} 本会话上一个任务没跑完：
            <b>{resumable.task.slice(0, 60)}{resumable.task.length > 60 ? '…' : ''}</b>
            <span className="hint-text">（{describeResumable(resumable)}）</span>
            {resumable.budget_exhausted && (
              <span className="hint-text"> · Token 预算已用尽，续跑会重新计一份</span>
            )}
          </span>
          <span style={{ flex: 1 }} />
          <button className="err-btn primary" onClick={() => resumeFromCheckpoint(resumable)}>
            ↻ {resumable.kind === 'multi' ? '继续协同'
              : resumable.kind === 'loop' ? '继续循环'
                : resumable.kind === 'plan' ? '继续计划' : '从断点续跑'}
          </button>
          <button className="err-btn" onClick={() => { dismissResumable(resumable.id, chatSid()); setResumable(null); }}>忽略</button>
        </div>
      )}

      {hasOverrides(ov) && (
        <div className="ov-pending">
          <span className="hint-text">本条：</span>
          {ov.mode && (
            <span className="ov-chip pending">{MODE_ICON[ov.mode]} {MODE_LABELS[ov.mode]}模式
              <button onClick={() => setOv((o) => ({ ...o, mode: undefined }))}>✕</button></span>
          )}
          {ov.expert && (
            <span className="ov-chip pending">🎓 {ov.expert.id ? ov.expert.name : '不使用专家'}
              <button onClick={() => setOv((o) => ({ ...o, expert: undefined }))}>✕</button></span>
          )}
          {ov.attachments.map((a) => (
            <span key={a.name} className="ov-chip pending" title={`${(a.size / 1024).toFixed(1)} KB`}>📄 {a.name}
              <button onClick={() => setOv((o) => ({ ...o, attachments: o.attachments.filter((x) => x.name !== a.name) }))}>✕</button></span>
          ))}
          {(ov.mcp || []).map((m) => (
            <span key={'mcp:' + m} className="ov-chip pending" title="本条引用的 MCP 连接器：它的工具会下发给模型">🔌 {m}
              <button onClick={() => setOv((o) => ({ ...o, mcp: toggleIn(o.mcp, m) }))}>✕</button></span>
          ))}
          {(ov.mentions || []).map((m) => (
            <span key={'mention:' + mentionKey(m)} className="ov-chip pending mention"
              title={m.type === 'conversation' ? '引用的对话：其记录随这条消息交给模型' : `引用的${m.type === 'dir' ? '目录' : '文件'}：${m.path}`}>
              {mentionLabel(m)}
              <button onClick={() => setOv((o) => ({ ...o, mentions: (o.mentions || []).filter((x) => x !== m) }))}>✕</button></span>
          ))}
          {(ov.tools || []).map((t) => (
            <span key={'tool:' + t} className="ov-chip pending" title="本条指定的工具：必定下发给模型">🔧 {t}
              <button onClick={() => setOv((o) => ({ ...o, tools: toggleIn(o.tools, t) }))}>✕</button></span>
          ))}
          {(ov.skills || []).map((s) => (
            <span key={'skill:' + s} className="ov-chip pending" title="本条引用的技能：指令注入 / 可经 skill_invoke 调用">🧩 {s}
              <button onClick={() => setOv((o) => ({ ...o, skills: toggleIn(o.skills, s) }))}>✕</button></span>
          ))}
        </div>
      )}

      <div className="input-bar">
       {/* v1.7.8 输入卡片：上为文本区、下为工具栏，整张卡片共用一个聚焦光圈。
           保留 input-inner 类名：HistoryView 靠 `.input-inner textarea` 回填任务文本。 */}
       <div ref={composerRef} data-empty={draft0.trim() ? '0' : '1'}
         className={'input-inner composer' + (running ? ' running' : '') + (recognizing ? ' listening' : '')}
         onClick={(e) => { if (e.target === e.currentTarget) taRef.current?.focus(); }}>
        <input ref={fileRef} type="file" multiple style={{ display: 'none' }} onChange={pickFiles} />
        <ComposerSuggest ref={sugRef} trigger={trigger} commands={commandItems}
          mentionStatic={mentionConvs} onPick={onPick} onClose={() => setTrigger(null)} />
        {/* 执行中不再锁死输入框：此前是 disabled={running}，用户看到 AI 跑偏了只能
            先停止再重发，还会丢掉已经跑出来的中间结果。现在随时能补一句话。 */}
        <textarea
          ref={taRef}
          className="composer-input"
          defaultValue={draft0}
          rows={1}
          placeholder={running
            ? '任务执行中 —— 把补充要求写在这里，Enter 或点 ⤴ 插入本轮（Shift+Enter 换行）'
            : ({
              chat: '输入消息，/ 调用指令，@ 引用文件或对话，Enter 发送…',
              work: '描述你想完成的任务（/ 指令 · @ 引用文件或对话），AutoMind 会自主规划并执行…',
              coding: '描述编程需求，@ 引用相关文件，/ 调用指令（/fix /test /review）…',
              multi: '描述一个较复杂的任务，多个智能体将分工协作完成...',
              loop: '描述一个需要反复迭代直到达标的目标，系统将自主循环修正...',
            } as Record<Mode, string>)[mode]}
          onKeyDown={(e) => {
            // 指令 / 引用面板开着时，↑↓ Enter Tab Esc 先交给面板
            if (sugRef.current?.onKey(e)) { e.preventDefault(); return; }
            if (e.key !== 'Enter' || e.shiftKey) return;
            e.preventDefault();
            // 执行中回车一律当"插入"：用户按回车就是想现在把这句话送出去。
            // 此前这里被 send() 开头的 running 检查静默吞掉，敲了回车像没反应。
            // Ctrl/Cmd+Enter 不分状态都走插入，交给服务端判断该排队还是开新任务。
            if (e.ctrlKey || e.metaKey || useApp.getState().running) interject();
            else send();
            markEmpty();
          }}
          onInput={(e) => {
            const el = e.currentTarget;
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 180) + 'px';
            saveDraft();
            markEmpty();
            updateTrigger();
          }}
          onKeyUp={(e) => {
            if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) updateTrigger();
          }}
          onClick={updateTrigger}
          onBlur={() => { saveDraft(true); setTrigger(null); }}
        />
        <div className="composer-bar">
          {/* v1.7.7 加号菜单：附件 / 语音 / 本条模式 / 本条专家，都挂在输入框旁 */}
          <SafeDropdown menu={{ items: plusMenu }} placement="topLeft" overlayClassName="plus-menu"
            onOpenChange={(o) => { if (o) { loadExperts(); loadRefs(); } }}>
            <button className={'plus-btn' + (hasOverrides(ov) ? ' active' : '')}
              title="附件 · 语音 · 本条模式 · 专家 / MCP / 技能引用 · 计划模式 · 目标" aria-label="更多输入选项">＋</button>
          </SafeDropdown>
          {/* v1.11.15 本条消息使用的模型（默认 / 已配置模型 / 专业·企业版自动路由） */}
          <ModelPicker />
          {recognizing && (
            <button className="composer-pill listening" title="停止语音输入" onClick={toggleVoice}>
              <span className="rec-dot" />聆听中 · 点击停止
            </button>
          )}
          {/* 当前发送将使用的模式（本条覆盖时标注），纯展示 */}
          <span className={'composer-mode' + (ov.mode ? ' override' : '')}
            title={ov.mode ? '本条消息使用的模式（可在 ＋ 菜单里改回）' : '当前模式（顶部切换）'}>
            {MODE_ICON[ov.mode || mode]} {MODE_LABELS[ov.mode || mode]}{ov.mode ? ' · 本条' : ''}
          </span>
          {/* v1.11.0 常驻引用：计划模式 / 长期目标 —— 点名字开关 / 查看，✕ 退出 */}
          <button className={'composer-sticky' + (planMode ? ' on' : '')} onClick={togglePlan}
            title={planMode ? '计划模式已开启：每条消息先生成计划，你批准后才执行。点击退出'
              : '计划模式：先生成计划、你批准后才执行。点击进入'}>
            📋 计划{planMode ? '模式 ✕' : ''}
          </button>
          <button className={'composer-sticky' + (stickyGoal ? ' on goal' : '')} onClick={openGoals}
            title={stickyGoal ? `正在围绕目标「${stickyGoal.title}」推进：点击查看进度` : '目标交付模式：设置 / 查看长期目标'}>
            🎯 {stickyGoal ? stickyGoal.title.slice(0, 14) + (stickyGoal.title.length > 14 ? '…' : '') : '目标'}
            {stickyGoal && (
              <span className="x" role="button" aria-label="摘掉目标" title="不再围绕这个目标推进"
                onClick={(e) => { e.stopPropagation(); useRefs.getState().setGoal(null); }}>✕</span>
            )}
          </button>
          {/* v1.11.1 定时任务入口：带上输入框里正在写的内容 */}
          <button className="composer-sticky" onClick={openSchedule}
            title={featureOn('scheduler')
              ? '设为定时任务：把输入框里的内容按固定频率自动执行'
              : '定时任务（专业版）：点击了解并升级'}>
            ⏰ 定时{featureOn('scheduler') ? '' : ' 🔒'}
          </button>
          <span style={{ flex: 1 }} />
          <span className="composer-hint">
            {running ? <><kbd>Enter</kbd> 插入补充</> : <><kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行</>}
          </span>
          {/* 不能直接 onClick={send}：React 会把 MouseEvent 当成 override 参数传进去 */}
          {/* 插入与停止并排且长得不一样：两者后果天差地别，样式相同会点错 ——
              停止会丢掉这一轮的中间结果，插入不会取消任何东西。 */}
          {running && (
            <button
              onClick={() => { interject(true); markEmpty(); }}
              title="插入补充：把这句话并进正在执行的任务，不会中断它（Enter 或 Ctrl+Enter；Shift+Enter 换行）"
              className="send-btn inject"
            >⤴ 插入</button>
          )}
          {!running ? (
            <button onClick={() => { send(); markEmpty(); }} title="发送 (Enter)" className="send-btn"
              aria-label="发送">
              <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true">
                <path d="M12 19V5M5.5 11.5 12 5l6.5 6.5" fill="none" stroke="currentColor"
                  strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          ) : (
            <button onClick={sendStop} title="停止" className="send-btn stop" aria-label="停止">
              <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
                <rect x="4" y="4" width="16" height="16" rx="3" fill="currentColor" />
              </svg>
            </button>
          )}
        </div>
       </div>
      </div>
    </div>
  );
}
export { MODE_LABELS, usePanel };
