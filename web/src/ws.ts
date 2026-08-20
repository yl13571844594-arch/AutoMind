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

function execTrace(mode: Mode, label: string, body: string, kind: string) {
  const t: TraceItem = { label, body, kind };
  if (live.loop) {
    chat().update(mode, live.loop, (i: any) => ({ ...i, traces: [...i.traces, t] }));
  } else if (live.exec) {
    chat().update(mode, live.exec, (i: any) => ({ ...i, traces: [...i.traces, t] }));
  }
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

function setRunning(on: boolean) {
  app().setRunning(on);
  // 完成/失败/中断/断线都会走到这里，进度条统一在此收掉，
  // 免得漏了某条终态路径，进度指示永远停在"第 3/7 步"不动。
  if (!on) { chat().setTaskMode(null); chat().persist(); panel().clearProgress(); }
}

// ── 事件分发 ───────────────────────────────────────────
function handle(data: any) {
  const mode = taskMode();
  // 观测中心：同一份事件流就地累积成执行 DAG（实时，无需轮询后端）
  try { useObserve.getState().onEvent(data); } catch { /* 观测失败不影响主流程 */ }
  switch (data.type) {
    case 'task_start':
      removeTyping(mode);
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
      break;

    case 'approval_timeout':
      // 超时此前是静默的：弹窗还挂着，用户以为系统仍在等他点
      panel().setApproval(null);
      // duration 0 = 不自动消失：这一步已按拒绝处理、任务多半也失败了，
      // 一闪而过的提示等于没提示
      message.warning(
        data.message || `工具 ${data.tool} 的审批等待超时，已按「拒绝」处理。`, 0);
      break;

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
  }
}
