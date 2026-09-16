// WebSocket 管理器 — 流式对话、执行过程实时展示、审批请求、团队活动。
// 事件驱动地更新 Zustand store；断线指数退避重连（封顶 30s）。
import { message } from 'antd';
import { chatSid, MODE_LABELS, useApp, type Mode } from './store/app';
import {
  uid, useChat, type ChatItem, type LoopIter, type MaStep, type PlanRow, type TraceItem,
} from './store/chat';
import { useObserve } from './store/observe';
import { usePanel } from './store/panel';
import { usePrefs } from './store/prefs';
import { esc, renderMarkdown } from './lib/markdown';
import { fmtDuration, notifyTask } from './lib/notify';

let ws: WebSocket | null = null;
let retry = 0;
let timer: ReturnType<typeof setTimeout> | null = null;

// 进行中面板的 id（按任务模式记录）
const live: { stream?: string; exec?: string; multi?: string; loop?: string } = {};
let streamBuf = '';
let streamFlushTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * 任务终态时发系统通知 —— 解决"跑 5 分钟的任务切走干别的，回来才发现早完了"。
 * 只在窗口不可见时发（用户正看着界面时，界面本身已经把结果摆在眼前了）。
 */
function notifyDone(kind: 'ok' | 'fail' | 'stop', mode: Mode, data: any): void {
  if (!usePrefs.getState().notifyOnDone) return;
  const label = MODE_LABELS[mode] || '任务';
  const dur = fmtDuration(data?.duration_ms || 0);
  if (kind === 'ok') {
    const bits = [dur && `耗时 ${dur}`, data?.steps ? `${data.steps} 个步骤` : ''].filter(Boolean);
    notifyTask({
      title: `✅ ${label}任务已完成`,
      body: bits.length ? bits.join(' · ') : '点击回到 AutoMind 查看结果',
    });
  } else if (kind === 'fail') {
    notifyTask({ title: `❌ ${label}任务失败`, body: String(data?.error || '点击回到 AutoMind 查看详情') });
  } else {
    notifyTask({ title: `⏹ ${label}任务已中断`, body: '点击回到 AutoMind 查看详情' });
  }
}

function app() { return useApp.getState(); }
function chat() { return useChat.getState(); }
function panel() { return usePanel.getState(); }
function taskMode(): Mode { return chat().taskMode || app().mode; }

export function connectWS() {
  if (timer) { clearTimeout(timer); timer = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws`);
  } catch { scheduleReconnect(); return; }
  ws.onopen = () => {
    const wasDown = useApp.getState().wsAttempt > 0;
    retry = 0;
    useApp.setState({ wsState: 'connected', wsAttempt: 0, wsNextRetryAt: 0 });
    // 断过再连上要说一声：用户刚才看到的是"正在重连"，得有个收尾
    if (wasDown) message.success('已重新连接到服务器');
  };
  ws.onclose = () => { scheduleReconnect(); };
  ws.onerror = () => { try { ws?.close(); } catch { /* ignore */ } };
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch { /* ignore */ } };
}

function scheduleReconnect() {
  if (timer) return;
  const base = Math.min(30000, 1000 * 2 ** retry++);
  const delay = base * (0.5 + Math.random() * 0.5);

  // 在途的补充一并收尾（详见该函数注释）。它只改气泡状态，不参与 running 的
  // 判定 —— 绝不能因为"还有补充没等到回音"就拖着界面不解锁。
  settleInterjectsOnDisconnect();

  // 任务跑到一半断线：后端把这次执行绑在这条 socket 上，事件再也送不回来了。
  // 若不复位 running，输入框会一直是 disabled，用户既看不到结果也没法重发 ——
  // 表现为"卡在执行中不动"。故落一张失败卡片（带重跑入口）并解锁界面。
  if (app().running) {
    const mode = taskMode();
    removeTyping(mode);
    finalizeStream(mode, null);
    finalizeAll(mode);
    appendFailure(mode, '中断', '与服务器的连接已断开，本次执行的结果无法送回。'
      + '连接恢复后可用下方按钮重跑。');
    setRunning(false);
  }

  // 把"第几次重连"和"下次何时重试"发布出去，界面据此显示倒计时
  useApp.setState({
    wsState: 'reconnecting', wsAttempt: retry, wsNextRetryAt: Date.now() + delay,
  });
  timer = setTimeout(() => { timer = null; connectWS(); }, delay);
}

/** 用户点「立即重连」——跳过剩余退避时间马上试一次。 */
export function reconnectNow() {
  if (timer) { clearTimeout(timer); timer = null; }
  retry = 0;
  try { ws?.close(); } catch { /* ignore */ }
  useApp.setState({ wsNextRetryAt: Date.now() });
  connectWS();
}

export function wsReady(): boolean { return !!ws && ws.readyState === WebSocket.OPEN; }

export function sendRun(task: string, images: string[]) {
  ws!.send(JSON.stringify({
    action: 'run', task, interaction: app().mode, images, session_id: chatSid(),
  }));
}
export function sendStop() {
  if (wsReady()) { ws!.send(JSON.stringify({ action: 'stop' })); message.info('正在中断任务...'); }
}
/**
 * 回传审批结果。
 *
 * `args` 非空即「修改后批准」（ApprovalAction.MODIFY）：服务端会用这份参数
 * 替换本次工具调用的实参，而不是拿模型原来给的那份去执行。
 */
export function sendApproval(
  approvalId: string, approved: boolean, args?: Record<string, any>,
) {
  if (!wsReady()) return;
  ws!.send(JSON.stringify({
    action: 'approval_response', approval_id: approvalId, approved,
    ...(args ? { arguments: args, comment: '用户修改参数后批准' } : {}),
  }));
}

/**
 * 中途插入的补充问题 —— 客户端侧的"事件 → 气泡"对应表。
 *
 * 主路径是 ref（请求里带上、服务端原样回显，见 sendInterject），这张表是**回落**：
 * 老式回执不带 ref 时按 seq/文本找，另外还记着这条补充是发在哪个模式的（promoted
 * 之后要把"上次任务"归到那个模式下）。表放在 ws.ts 而不是 store，是因为它纯粹是
 * 传输层的易失状态 —— 写进 localStorage 只会在重启后留下一堆对不上号的 seq。
 */
interface SentInterject { id: string; text: string; mode: Mode; seq?: number }
const sentInterjects: SentInterject[] = [];

// 一次执行里同时在途的补充不会有几条，留点余量即可；服务端一直不回（比如它在
// 回事件之前就崩了）时，靠这个上限避免登记项无限堆积。
const MAX_SENT_INTERJECTS = 20;

/**
 * 发送一条中途补充。
 *
 * `itemId` 由调用方（ChatPanel）先生成：气泡是"发出去就立刻显示"的乐观渲染，
 * 而服务端事件只带 seq/text。把 id 在发送时就交进来，收到确认后才找得回那个
 * 已经画在屏幕上的气泡。（也可以让服务端回 itemId，但协议已冻结，只能在本地记账。）
 *
 * `ref` 直接复用这个气泡 id：协议要求客户端给一个"稳定唯一串"并原样回显，而
 * 气泡 id 本来就满足（发送时生成、跨会话唯一、随消息一起落盘）。这样回执一到
 * 就能一步定位 —— 连发两条一模一样的补充也各归各的，不必靠"最新一条未确认"
 * 这种猜测。
 *
 * 刻意不带图片：interject 的语义是"补一句话"。协议虽留了 images 字段，这里仍传
 * 空数组 —— 待发区的图片是用户为下一条正式消息准备的，悄悄一起发出去会让他以为
 * 图片还留着（ChatPanel 里会把这件事告诉用户）。
 */
export function sendInterject(text: string, itemId: string): void {
  if (!wsReady()) return;
  sentInterjects.push({ id: itemId, text, mode: app().mode });
  if (sentInterjects.length > MAX_SENT_INTERJECTS) sentInterjects.shift();
  ws!.send(JSON.stringify({
    action: 'interject', text, session_id: chatSid(), interaction: app().mode, images: [],
    ref: itemId,
  }));
}

/** 回执里的 ref（服务端原样回显；也接受 client_ref 这种写法）。ref 就是气泡 id。 */
function refOf(data: any): string {
  const r = data?.ref ?? data?.client_ref;
  return typeof r === 'string' ? r : '';
}

/** 首先按 ref、其次按 seq、最后按文本找回登记项。seq 要等服务端确认时才知道。 */
function findSentInterject(data: any): SentInterject | null {
  const ref = refOf(data);
  if (ref) {
    const byRef = sentInterjects.find((e) => e.id === ref);
    if (byRef) return byRef;
  }
  const seq = Number(data?.seq);
  if (Number.isFinite(seq)) {
    const bySeq = sentInterjects.find((e) => e.seq === seq);
    if (bySeq) return bySeq;
  }
  const text = String(data?.text ?? '');
  return sentInterjects.find((e) => e.text === text) || null;
}

/**
 * 还没定论的插入补充（既没确认纳入、也没说没纳入、也没被提升成新任务）——
 * 三处判定共用，避免以后加了一档状态却漏掉某一处。写成类型谓词是为了让调用处
 * 直接拿到 msg 变体（要读 id/md），不必再到处断言。
 */
function isUnsettledInjection(
  it: ChatItem,
): it is Extract<ChatItem, { kind: 'msg' }> {
  return it.kind === 'msg' && !!it.injected
    && !it.applied && !it.promoted && !it.dropped && !it.rejected;
}

/**
 * 定位这条插入事件对应的气泡 id。
 *
 * ref 优先（见 sendInterject：它就是气泡 id，唯一且不含歧义）；没有 ref 的旧式
 * 回执才退回登记表，再不行就回到消息列表里按文本找"还没定论"的插入气泡 —— 只认
 * 未定论的，否则上一轮一条同名的补充会被这次事件改成错的状态。刷新页面后登记表
 * 是空的，靠的正是这最后一层。
 */
function interjectBubbleId(data: any): string | null {
  const ref = refOf(data);
  if (ref) return ref;
  const entry = findSentInterject(data);
  if (entry) return entry.id;
  const text = String(data?.text ?? '');
  for (const m of Object.keys(MODE_LABELS) as Mode[]) {
    const items = chat().items(m);
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (!isUnsettledInjection(it)) continue;
      if (text && it.md !== text) continue;
      return it.id;
    }
  }
  return null;
}

/** 事件已定论，把登记项撤掉：seq 很可能按轮次从 1 重新计数，留着会串到下一轮。 */
function forgetSentInterject(data: any): void {
  const entry = findSentInterject(data);
  const at = entry ? sentInterjects.indexOf(entry) : -1;
  if (at >= 0) sentInterjects.splice(at, 1);
}

// 模型是在哪一步读到这条补充的。只报位置、不替服务端断言"是否改变了答案" ——
// turn_end 读到意味着这轮回答可能没受影响，说成"已生效"就是过度承诺。
const INJECT_AT_CN: Record<string, string> = {
  chat_round: '本轮回答', react_step: '执行步骤', plan_step: '计划步骤', turn_end: '本轮收尾',
};

/** 提示语里引用插入原文：太长的截断，免得一条提示占掉半个屏幕。 */
function brief(text: unknown, n = 40): string {
  const s = String(text ?? '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

/**
 * 断线时给还没定论的插入收个尾。
 *
 * 后端把这次执行绑在这条 socket 上，事件再也送不回来了 —— 那些停在"已收下，
 * 排入本轮"的气泡永远不会变成已纳入/未纳入，一直挂着等于骗用户"还在排队"。
 * 这里统一标成未纳入（提示语写明是断线导致无法确认），并清空登记表：seq 可能
 * 按轮次从 1 重数，留着会串到重连之后的新任务上。注意它只动气泡，不碰 running。
 */
function settleInterjectsOnDisconnect(): void {
  if (!sentInterjects.length) return;
  sentInterjects.length = 0;
  for (const m of Object.keys(MODE_LABELS) as Mode[]) {
    for (const it of chat().items(m)) {
      if (!isUnsettledInjection(it)) continue;
      chat().markInjected(it.id, 'dropped', '与服务器的连接已断开，这条补充是否被读到无法确认');
    }
  }
}

// ── 面板/气泡工具 ──────────────────────────────────────
function removeTyping(mode: Mode) { chat().removeKind(mode, ['typing']); }

function startStream(mode: Mode) {
  removeTyping(mode);
  streamBuf = '';
  const id = uid();
  live.stream = id;
  chat().append(mode, { kind: 'stream', id, buf: '' });
}

function flushStream(mode: Mode) {
  if (!live.stream) return;
  const id = live.stream;
  chat().update(mode, id, (i) => ({ ...(i as any), buf: streamBuf }));
}

function scheduleFlush(mode: Mode) {
  if (streamFlushTimer) return;
  streamFlushTimer = setTimeout(() => { streamFlushTimer = null; flushStream(mode); }, 50);
}

function finalizeStream(mode: Mode, data: any | null) {
  if (streamFlushTimer) { clearTimeout(streamFlushTimer); streamFlushTimer = null; }
  const id = live.stream;
  live.stream = undefined;
  if (!id) return;
  if (!streamBuf.trim() && !data) { chat().remove(mode, id); return; }
  const meta: string[] = [];
  if (data?.cached) meta.push('⚡ 缓存命中 · 0 Token');
  else if (data?.tokens) meta.push(`🪙 ${data.tokens}tk (${data.prompt_tokens || 0}↑/${data.completion_tokens || 0}↓ · 估算)`);
  if (data?.duration_ms) meta.push(`${data.duration_ms}ms`);
  chat().update(mode, id, () => ({
    kind: 'msg', id, role: 'agent', md: streamBuf || '(无回复)',
    meta: meta.join(' · ') || new Date().toLocaleTimeString(),
  } as ChatItem));
  chat().persist();
  streamBuf = '';
}

function startExec(mode: Mode) {
  removeTyping(mode);
  const id = uid();
  live.exec = id;
  chat().append(mode, { kind: 'exec', id, traces: [], plan: [], done: false });
}
function startMulti(mode: Mode) {
  removeTyping(mode);
  const id = uid();
  live.multi = id;
  chat().append(mode, { kind: 'multi', id, steps: [], done: false });
}
function startLoop(mode: Mode) {
  removeTyping(mode);
  const id = uid();
  live.loop = id;
  chat().append(mode, { kind: 'loop', id, iters: [], done: false, traces: [] });
}

const MA_ROLE_CN: Record<string, string> = {
  planner: '规划', researcher: '研究', coder: '编程', writer: '写作', reviewer: '审阅',
};

/** 按 goal_id 从进行中的执行面板反查"这是第几步、步骤文案是什么"，供进度条显示。 */
function planStepInfo(goalId: any): { idx: number; text: string } {
  if (!live.exec) return { idx: 0, text: '' };
  const item: any = chat().items(taskMode()).find((i) => i.id === live.exec);
  const rows: PlanRow[] = item?.plan || [];
  const at = rows.findIndex((r) => r.goalId && r.goalId === String(goalId || ''));
  // 计划文案本身带了"1. "序号前缀，进度条另有"第 x / y 步"，去掉以免重复
  return at < 0 ? { idx: 0, text: '' }
    : { idx: at + 1, text: (rows[at].text || '').replace(/^\d+\.\s*/, '') };
}

/**
 * 单个执行面板最多保留的轨迹条数。
 *
 * 此前无上限：一个跑几百步的长任务能攒出上万条轨迹，每条都带已渲染的 HTML
 * （截图那种还是整张 base64 图）。累积到后面，**每来一条新轨迹**都要复制整个
 * 数组并重排上万个 DOM 节点 —— 观测面板直接卡死，滚都滚不动。
 * 保留最近 300 条足够看清"现在在干什么、刚才为什么失败"；更早的执行记录
 * 在「任务历史」与「观测中心」里有完整留存，不靠这里兜底。
 */
const MAX_TRACES = 300;

function pushTrace(item: any, t: TraceItem) {
  const traces = [...item.traces, t];
  const over = traces.length - MAX_TRACES;
  if (over <= 0) return { ...item, traces };
  // 一次多丢一些（丢到 90%），避免此后每条新轨迹都触发一次数组截断
  const drop = over + Math.floor(MAX_TRACES * 0.1);
  return {
    ...item,
    traces: traces.slice(drop),
    traceDropped: (item.traceDropped || 0) + drop,
  };
}

function execTrace(mode: Mode, label: string, body: string, kind: string) {
  const t: TraceItem = { label, body, kind };
  const target = live.loop || live.exec;
  if (target) chat().update(mode, target, (i: any) => pushTrace(i, t));
}

function finalizeAll(mode: Mode, data?: any) {
  if (live.exec) { chat().update(mode, live.exec, (i: any) => ({ ...i, done: true })); live.exec = undefined; }
  if (live.multi) { chat().update(mode, live.multi, (i: any) => ({ ...i, done: true })); live.multi = undefined; }
  if (live.loop) {
    const stop = data?.stop_reason || '';
    chat().update(mode, live.loop, (i: any) => ({ ...i, done: true, stopReason: stop }));
    live.loop = undefined;
  }
}

function appendResult(mode: Mode, data: any) {
  const meta: string[] = [];
  if (data.interaction && data.interaction !== 'chat') {
    if (data.steps) meta.push(`${data.steps}步`);
    if (data.backtracks) meta.push(`${data.backtracks}回溯`);
  }
  if (data.cached) meta.push('⚡ 缓存命中');
  if (data.tokens) meta.push(`🪙 ${data.tokens}tk (${data.prompt_tokens || 0}↑/${data.completion_tokens || 0}↓)`);
  if (data.duration_ms) meta.push(`${data.duration_ms}ms`);
  chat().append(mode, {
    kind: 'msg', id: uid(), role: 'agent', md: data.output || '任务完成',
    meta: meta.join(' · ') || new Date().toLocaleTimeString(),
  });
}

// 失败/中断一律落成一张带恢复入口的卡片（而不是一行红字 + 一个可能不出现的
// 续跑气泡）。任务原文直接写进卡片，重启后依然能续跑。
function appendFailure(mode: Mode, why: '出错' | '中断', error: string) {
  const last = chat().lastTask;
  chat().append(mode, {
    kind: 'error', id: uid(), why, error,
    task: last?.text, taskMode: last?.mode ?? mode,
    at: new Date().toLocaleTimeString(),
  });
  // 用户不在这个模式/视图时看不到卡片，补一句提示指路
  if (!(app().view === 'chat' && app().mode === mode)) {
    message.info(`${MODE_LABELS[mode]}任务已${why}。回到该模式可「继续此任务」或「重新执行」`);
  }
}

/**
 * 任务终态兜底：把还停在"已收下、未确认"的插入补充统一标成未纳入并提示一次。
 *
 * 为什么必须有这一层：用户点「停止」时任务是被取消的，服务端很可能来不及发
 * interjection_dropped。不兜底的话气泡会永远停在"待本轮读取"，用户一直以为自己
 * 那句话生效了 —— 这比明确报错更糟。已 applied/dropped/rejected 的一律不动。
 *
 * 放在 setRunning(false) 里，是因为 chat_done / task_complete / task_cancelled /
 * task_error 四条终态路径都必经此处（断线那条更早就由
 * settleInterjectsOnDisconnect 收尾了，到这里已无可扫之物，不会重复提示）。
 */
function settleInterjectsOnRunEnd(): void {
  let n = 0;
  for (const m of Object.keys(MODE_LABELS) as Mode[]) {
    for (const it of chat().items(m)) {
      if (!isUnsettledInjection(it)) continue;
      chat().markInjected(it.id, 'dropped', '本轮任务已结束，未收到服务端确认，这条补充没能纳入');
      n++;
    }
  }
  if (!n) return;
  // 本轮已结束，登记项一并清掉：seq 可能按轮次从 1 重数，留着会串到下一轮
  sentInterjects.length = 0;
  // 气泡上的徽标用户未必正看着，补一条提示把话说明白
  message.warning(
    `本轮任务已结束，你插入的 ${n} 条补充没能纳入（未收到服务端确认），需要的话请重新发送`, 6);
}

function setRunning(on: boolean) {
  app().setRunning(on);
  // 完成/失败/中断/断线都会走到这里，进度条统一在此收掉，
  // 免得漏了某条终态路径，进度指示永远停在"第 3/7 步"不动。
  if (!on) {
    chat().setTaskMode(null);
    settleInterjectsOnRunEnd();
    chat().persist();
    panel().clearProgress();
  }
}

// ── 事件分发 ───────────────────────────────────────────
function handle(data: any) {
  const mode = taskMode();
  // 观测中心：同一份事件流就地累积成执行 DAG（实时，无需轮询后端）
  try { useObserve.getState().onEvent(data); } catch { /* 观测失败不影响主流程 */ }
  switch (data.type) {
    case 'task_start':
      removeTyping(mode);
      // running 在这里置位，而不是只靠 ChatPanel 的 send()。补充被服务端"提升"
      // 成新任务时（interjection_promoted）界面上没有任何人替它置位，漏了这一步
      // 就会出现"任务在跑、输入框还是发送态、进度条不显示"的错乱渲染。
      setRunning(true);
      chat().setTaskMode(mode);
      panel().startProgress(
        data.interaction === 'multi' ? '协同中'
          : data.interaction === 'loop' ? '迭代中'
            : data.interaction === 'chat' ? '正在回答' : '正在执行',
        data.interaction === 'chat' ? '' : '准备中…');
      if (data.interaction === 'chat') startStream(mode);
      else if (data.interaction === 'multi') startMulti(mode);
      else if (data.interaction === 'loop') startLoop(mode);
      else startExec(mode);
      break;

    case 'ma_plan':
      if (!live.multi) startMulti(mode);
      chat().update(mode, live.multi!, (i: any) => ({
        ...i,
        steps: (data.plan || []).map((s: any): MaStep => ({ role: s.role, subtask: s.subtask, state: 'pending' })),
      }));
      panel().patchProgress({ total: (data.plan || []).length, label: '分工已确定' });
      break;
    case 'ma_step_start':
      panel().patchProgress({
        cur: (data.index ?? 0) + 1,
        label: `${MA_ROLE_CN[data.role] || data.role || ''}：${data.subtask || ''}`.slice(0, 60),
      });
      if (live.multi) chat().update(mode, live.multi, (i: any) => ({
        ...i, steps: i.steps.map((s: MaStep, k: number) => (k === data.index ? { ...s, state: 'run' } : s)),
      }));
      break;
    case 'ma_step_end':
      if (live.multi) chat().update(mode, live.multi, (i: any) => ({
        ...i,
        steps: i.steps.map((s: MaStep, k: number) => (k === data.index
          ? { ...s, state: 'ok', output: (data.output || '').slice(0, 600) } : s)),
      }));
      break;

    case 'loop_iter_start':
      panel().patchProgress({ cur: data.iter || 0, total: data.max || 0, label: '本轮执行中…' });
      if (!live.loop) startLoop(mode);
      chat().update(mode, live.loop!, (i: any) => ({
        ...i, iters: [...i.iters, { iter: data.iter, max: data.max } as LoopIter],
      }));
      break;
    case 'loop_action':
      if (live.loop) chat().update(mode, live.loop, (i: any) => ({
        ...i,
        iters: i.iters.map((it: LoopIter) => (it.iter === data.iter
          ? { ...it, action: (data.output || '').slice(0, 300) } : it)),
      }));
      break;
    case 'loop_observation':
      if (live.loop) chat().update(mode, live.loop, (i: any) => ({
        ...i,
        iters: i.iters.map((it: LoopIter) => (it.iter === data.iter
          ? { ...it, obs: (data.reason || '').slice(0, 160), done: !!data.done } : it)),
      }));
      break;

    case 'approval_request':
      panel().setApproval({
        approval_id: data.approval_id, tool: data.tool, tier: data.tier,
        reason: data.reason || '', params: data.params || {},
        // editable 是未截断的原始参数，供「修改参数」表单回填
        editable: data.editable || {},
        // 后端等待上限：弹窗据此倒计时。不给期限的话，用户会以为可以一直等，
        // 而实际上超时后这一步已按拒绝处理、任务也失败了
        timeoutS: data.timeout_s || 0,
        askedAt: Date.now(),
      });
      // 进度条同步切成"等你批准"：弹窗万一被别的窗口/弹层挡住，
      // 输入框旁边这一行仍然是可见的
      panel().patchProgress({ label: `⏳ 等待你批准：${data.tool || ''}` });
      break;

    case 'approval_stale':
      // 迟到的回答（审批已超时/任务已中断）—— 明说，别让用户以为点空了
      message.info(data.message || '这次审批已经结束，本次点击不再生效。', 5);
      panel().setApproval(null);
      break;

    case 'approval_timeout':
      // 超时此前是静默的：弹窗还挂着，用户以为系统仍在等他点
      panel().setApproval(null);
      // duration 0 = 不自动消失：这一步已按拒绝处理、任务多半也失败了，
      // 一闪而过的提示等于没提示
      message.warning(
        data.message || `工具 ${data.tool} 的审批等待超时，已按「拒绝」处理。`, 0);
      break;

    case 'approval_failed': {
      // 审批通道本身出错（回调抛异常）——这一步已被按拒绝处理，必须让用户知道
      panel().setApproval(null);
      execTrace(taskMode(), `🙋 审批通道异常：${data.tool || ''}`,
        esc(String(data.reason || '')), 'error');
      message.error(
        `${data.tool || '该操作'} 的审批未能完成（${data.reason || '通道异常'}），已按拒绝处理。`,
        6);
      break;
    }

    case 'tool_timeout': {
      // 单步工具超时：这一步被中止了，但任务还在继续（模型会换做法）
      const n = Number(data.timeouts_total || 1);
      execTrace(taskMode(), `⏱ 单步超时：${data.tool || ''}`,
        `${esc(String(data.reason || ''))}<br>超时上限 ${data.timeout_s || 0}s`
        + (n > 1 ? `（本次任务第 ${n} 次）` : ''), 'error');
      break;
    }

    case 'react_no_progress': {
      // ReAct 原地打转：同一个动作被反复执行到被拦截
      const blocked = Number(data.blocked || 0);
      panel().patchProgress({ label: `🔁 无进展：${data.tool || '重复动作'}` });
      execTrace(taskMode(), `🔁 无进展（第 ${blocked} 次拦截）`,
        `${esc(String(data.tool || ''))}<br>${esc(String(data.reason || ''))}`,
        'error');
      break;
    }

    case 'team_activity': {
      panel().pushTeam(data);
      if (data.sid && data.sid !== chatSid()) {
        if (data.kind === 'task_done') {
          message.info(`👥 同事完成了任务「${data.task}」${data.changed_files ? `（涉及 ${data.changed_files} 个文件改动）` : ''}`);
          panel().bumpRefresh();
        } else if (data.kind === 'task_assigned') {
          message.info(`👥 新团队任务：「${data.title}」${data.assignee ? ` → ${data.assignee}` : ''}`);
        }
      }
      break;
    }

    // 每次 LLM 调用结束就更新 token 数（v1.5.1）。
    // 此前流式回答只在整段生成完才刷一次，长回答期间面板上一直是 0，
    // 看起来像"没在计费/卡住了"。后端现在按调用推 usage_update。
    case 'usage_update': {
      const cum = data.cumulative || {};
      if (typeof cum.total_tokens === 'number') {
        panel().setStats({ tokens: cum.total_tokens });
      }
      break;
    }

    // 工具失败单独标红 —— 此前混在 step_action 流水里看不出来（v1.5.1）
    case 'tool_error': {
      const streak = data.streak || 1;
      const tail = data.circuit_open
        ? `（已连续失败 ${streak} 次，停止重试）`
        : streak > 1 ? `（第 ${streak} 次失败）` : '';
      execTrace(taskMode(), `⛔ 工具失败：${data.tool}${tail}`,
        esc(String(data.error || '')), 'error');
      break;
    }

    // 心跳：长调用期间证明"还活着"，刷新进度条上的阶段文案
    case 'heartbeat': {
      const label = data.phase === 'streaming' ? '正在生成回答' : '正在思考';
      panel().patchProgress({ label: `${label}…` });
      break;
    }

    // 任务前自检发现的问题（LLM 未配置 / 目录不可写等）
    case 'preflight_warning': {
      const items: string[] = data.problems || [];
      if (items.length) message.warning(`任务前检查发现问题：${items.join('；')}`, 6);
      break;
    }

    case 'plan_created': {
      const rows: PlanRow[] = (data.steps || []).map((s: any, i: number) => ({
        text: `${i + 1}. ${s.description}${s.tool ? ` [${s.tool}]` : ''}`,
        goalId: String(s.goal_id || ''), state: 'pending',
      }));
      panel().patchProgress({ total: rows.length, label: '计划已生成' });
      if (live.exec) chat().update(mode, live.exec, (i: any) => ({ ...i, plan: rows }));
      else if (live.loop) execTrace(mode, `📋 已生成计划（${rows.length} 步）`,
        rows.map((r) => `<div>${esc(r.text)}</div>`).join(''), 'plan');
      break;
    }
    // 按 goal_id 匹配（后端事件不含 index —— 早期按下标匹配导致进度从不更新）
    case 'plan_step_start': {
      const st = planStepInfo(data.goal_id);
      panel().patchProgress(st.idx > 0
        ? { cur: st.idx, label: st.text }
        : { label: '执行中…' });
      if (live.exec) chat().update(mode, live.exec, (i: any) => ({
        ...i,
        plan: i.plan.map((r: PlanRow) => (r.goalId && r.goalId === String(data.goal_id || '')
          ? { ...r, state: 'run' } : r)),
      }));
      break;
    }
    case 'plan_step_end':
      if (live.exec) chat().update(mode, live.exec, (i: any) => ({
        ...i,
        plan: i.plan.map((r: PlanRow) => (r.goalId && r.goalId === String(data.goal_id || '')
          ? { ...r, state: data.success ? 'ok' : 'fail', error: data.error } : r)),
      }));
      break;
    case 'plan_backtrack':
      execTrace(mode, '↺ 回溯', esc(data.reason), 'warn');
      break;
    case 'step_thought':
      panel().patchProgress({ label: '思考中…' });
      execTrace(mode, '🧠 思考' + (data.iter ? ` · 第${data.iter}轮` : ''), renderMarkdown(data.text || ''), 'think');
      break;
    case 'step_action': {
      panel().patchProgress({ label: (data.tool ? `调用 ${data.tool}` : '执行动作') });
      const args = Object.keys(data.args || {}).length
        ? `<div class="trace-args">${esc(JSON.stringify(data.args))}</div>` : '';
      const out = data.output
        ? `<div class="trace-out ${data.success ? '' : 'fail'}">${data.success ? '→ ' : '✗ '}${esc(String(data.output).slice(0, 400))}</div>` : '';
      execTrace(mode, (data.success ? '🛠 ' : '⚠ ') + '调用 ' + esc(data.tool), args + out, data.success ? 'action' : 'warn');
      break;
    }
    case 'browser_preview': {
      // 浏览器/截图工具返回的网页截图 —— 直接渲染在对话框里，展示网页交互效果
      const b64 = data.screenshot_base64 || '';
      if (b64) {
        execTrace(mode, '🖼 ' + esc(data.tool || '浏览器') + ' · 网页截图',
          `<img class="trace-shot" src="data:image/png;base64,${b64}" alt="网页截图" />`,
          'shot');
      }
      break;
    }

    case 'chat_chunk':
      if (!live.stream) startStream(mode);
      streamBuf += data.delta;
      scheduleFlush(mode);
      break;
    case 'chat_done':
      finalizeStream(mode, data);
      panel().setStats({ steps: 0, backtracks: 0, tokens: data.tokens || 0, duration_ms: data.duration_ms || 0 });
      panel().bumpRefresh();
      setRunning(false);
      notifyDone('ok', mode, data);
      break;

    case 'task_complete':
      removeTyping(mode);
      finalizeAll(mode, data);
      appendResult(mode, data);
      panel().setStats({
        steps: data.steps || 0, backtracks: data.backtracks || 0,
        tokens: data.tokens || 0, duration_ms: data.duration_ms || 0,
      });
      if (data.plan) panel().setPlan(data.plan);
      panel().bumpRefresh();
      chat().setLastTask(null);
      chat().persist();
      setRunning(false);
      notifyDone('ok', mode, data);
      break;

    case 'task_error':
      removeTyping(mode);
      finalizeStream(mode, null);
      finalizeAll(mode);
      appendFailure(mode, '出错', String(data.error || '未知错误'));
      panel().bumpRefresh();
      setRunning(false);
      notifyDone('fail', mode, data);
      break;

    case 'task_cancelled':
      finalizeStream(mode, null);
      removeTyping(mode);
      finalizeAll(mode);
      appendFailure(mode, '中断', String(data.error || '任务被手动停止'));
      panel().bumpRefresh();
      setRunning(false);
      notifyDone('stop', mode, data);
      break;

    // ── 中途插入的补充问题 ───────────────────────────────
    // 这五条构成一条完整的生命周期：received → applied / dropped；没任务在跑时
    // 走 promoted，参数不合法走 rejected。只有 received 是"收下了"，其余四条都
    // 必须让用户看见 —— 沉默的失败会让他以为那句话已经算进本轮回答里了。
    case 'interjection_received': {
      // 到这里才知道 seq，补进登记表（seq 只用于展示"前面还有几条"，定位一律靠 ref）
      const text = String(data.text ?? '');
      const ref = refOf(data);
      const entry = sentInterjects.find((e) => e.id === ref && e.seq === undefined)
        || sentInterjects.find((e) => e.seq === undefined && e.text === text)
        || sentInterjects.find((e) => e.seq === undefined);
      if (entry) entry.seq = Number(data.seq);
      const id = interjectBubbleId(data);
      const pending = Number(data.pending || 0);
      if (id) {
        chat().markInjected(id, 'received', pending > 1
          ? `已收下，前面还有 ${pending - 1} 条补充在排队`
          : '已收下，排入本轮等待模型读取');
      }
      break;
    }

    case 'interjection_applied': {
      // 模型**真的读到了**：徽标从"待读取"转成"已纳入本轮"。
      // 服务端一次可能下发多条（items，每条各带自己的 ref），也可能只给顶层一条。
      const items: any[] = Array.isArray(data.items) && data.items.length ? data.items : [data];
      const labels = new Set<string>();
      for (const it of items) {
        const label = INJECT_AT_CN[String(it?.applied_at ?? it?.at ?? '')]
          || String(it?.applied_at ?? it?.at ?? '本轮');
        labels.add(label);
        const id = interjectBubbleId(it);
        if (id) chat().markInjected(id, 'applied', `模型已在${label}读到这条补充`);
        forgetSentInterject(it);
      }
      // 再补一条短提示是必要的 —— 此刻用户往往正盯着流式回答，未必看得到列表里
      // 那个徽标的变化，而"我那句话到底有没有进去"正是这个功能最要紧的反馈。
      const where = labels.size === 1 ? [...labels][0] : '本轮';
      if (items.length === 1) {
        message.success(`✓ 已纳入本轮（${where}）：${brief(items[0]?.text)}`, 3);
      } else {
        message.success(`✓ ${items.length} 条补充已纳入本轮（${where}）`, 3);
      }
      break;
    }

    case 'interjection_dropped': {
      // 没能纳入本轮（任务先结束了）。这条最不能沉默：用户以为补上了，其实没有，
      // 而本轮回答会按原来的要求在跑。气泡同样要标成未纳入，别只靠一闪而过的提示。
      const reason = String(data.reason || '本轮任务在你补充之前就结束了');
      const id = interjectBubbleId(data);
      if (id) chat().markInjected(id, 'dropped', reason);
      forgetSentInterject(data);
      message.warning(
        `你插入的补充没能纳入本轮（${reason}）：${brief(data.text)}，需要的话请重新发送`, 6);
      break;
    }

    case 'interjection_promoted': {
      // 当时没有任务在跑，服务端已把它当成一次新任务直接开跑（task_start 随之而来）。
      // 顺手把"上次任务"改成这句补充：这一轮若失败/中断，失败卡片上的"原任务"
      // 必须是真正在跑的东西，否则"检查现状后接着做"会去续一个早已完成的旧任务。
      const entry = findSentInterject(data);
      chat().setLastTask({ text: String(data.text ?? entry?.text ?? ''), mode: entry?.mode ?? taskMode() });
      const id = interjectBubbleId(data);
      // 必须落一个"有定论"的状态：它已经是这次任务的正文，若还挂着"待读取"，
      // 任务结束时会被下面的兜底误标成"没纳入"，等于告诉用户那句补充没生效。
      if (id) chat().markInjected(id, 'promoted', '当时没有正在执行的任务，已作为新任务直接开始');
      forgetSentInterject(data);
      message.info('当时没有正在执行的任务，已把它作为新任务直接开始');
      break;
    }

    case 'interjection_rejected': {
      // 被拒（空内容/太长/配额/并发上限）。气泡留着并标成未生效，而不是删掉：
      // 直接扔掉的话，用户刚敲的那段字就再也找不回来了。
      const reason = String(data.reason || '这条补充未被接受');
      const id = interjectBubbleId(data);
      if (id) chat().markInjected(id, 'rejected', reason);
      forgetSentInterject(data);
      message.error(data.text ? `${reason}：${brief(data.text)}` : reason, 6);
      break;
    }
  }
}
