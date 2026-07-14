// WebSocket 管理器 — 流式对话、执行过程实时展示、审批请求、团队活动。
// 事件驱动地更新 Zustand store；断线指数退避重连（封顶 30s）。
import { message } from 'antd';
import { chatSid, MODE_LABELS, useApp, type Mode } from './store/app';
import {
  uid, useChat, type ChatItem, type LoopIter, type MaStep, type PlanRow, type TraceItem,
} from './store/chat';
import { usePanel } from './store/panel';
import { esc, renderMarkdown } from './lib/markdown';

let ws: WebSocket | null = null;
let retry = 0;
let timer: ReturnType<typeof setTimeout> | null = null;

// 进行中面板的 id（按任务模式记录）
const live: { stream?: string; exec?: string; multi?: string; loop?: string } = {};
let streamBuf = '';
let streamFlushTimer: ReturnType<typeof setTimeout> | null = null;

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
  ws.onopen = () => { retry = 0; useApp.setState({ wsState: 'connected' }); };
  ws.onclose = () => { useApp.setState({ wsState: 'disconnected' }); scheduleReconnect(); };
  ws.onerror = () => { try { ws?.close(); } catch { /* ignore */ } };
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch { /* ignore */ } };
}

function scheduleReconnect() {
  if (timer) return;
  const base = Math.min(30000, 1000 * 2 ** retry++);
  timer = setTimeout(() => { timer = null; connectWS(); }, base * (0.5 + Math.random() * 0.5));
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
export function sendApproval(approvalId: string, approved: boolean) {
  if (wsReady()) ws!.send(JSON.stringify({ action: 'approval_response', approval_id: approvalId, approved }));
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

function offerResume(mode: Mode, why: string) {
  const last = chat().lastTask;
  if (!last || !last.text) return;
  if (app().view === 'chat' && app().mode === mode) {
    chat().append(mode, { kind: 'resume', id: uid(), why });
  } else {
    message.info(`任务已${why}。回到${MODE_LABELS[mode]}模式可点「继续此任务」续跑`);
  }
}

function setRunning(on: boolean) {
  app().setRunning(on);
  if (!on) { chat().setTaskMode(null); chat().persist(); }
}

// ── 事件分发 ───────────────────────────────────────────
function handle(data: any) {
  const mode = taskMode();
  switch (data.type) {
    case 'task_start':
      removeTyping(mode);
      chat().setTaskMode(mode);
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
      break;
    case 'ma_step_start':
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
      });
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

    case 'plan_created': {
      const rows: PlanRow[] = (data.steps || []).map((s: any, i: number) => ({
        text: `${i + 1}. ${s.description}${s.tool ? ` [${s.tool}]` : ''}`, state: 'pending',
      }));
      if (live.exec) chat().update(mode, live.exec, (i: any) => ({ ...i, plan: rows }));
      else if (live.loop) execTrace(mode, `📋 已生成计划（${rows.length} 步）`,
        rows.map((r) => `<div>${esc(r.text)}</div>`).join(''), 'plan');
      break;
    }
    case 'plan_step_start':
      if (live.exec) chat().update(mode, live.exec, (i: any) => ({
        ...i, plan: i.plan.map((r: PlanRow, k: number) => (k === data.index ? { ...r, state: 'run' } : r)),
      }));
      break;
    case 'plan_step_end':
      if (live.exec) chat().update(mode, live.exec, (i: any) => ({
        ...i,
        plan: i.plan.map((r: PlanRow, k: number) => (k === data.index
          ? { ...r, state: data.success ? 'ok' : 'fail', error: data.error } : r)),
      }));
      break;
    case 'plan_backtrack':
      execTrace(mode, '↺ 回溯', esc(data.reason), 'warn');
      break;
    case 'step_thought':
      execTrace(mode, '🧠 思考' + (data.iter ? ` · 第${data.iter}轮` : ''), renderMarkdown(data.text || ''), 'think');
      break;
    case 'step_action': {
      const args = Object.keys(data.args || {}).length
        ? `<div class="trace-args">${esc(JSON.stringify(data.args))}</div>` : '';
      const out = data.output
        ? `<div class="trace-out ${data.success ? '' : 'fail'}">${data.success ? '→ ' : '✗ '}${esc(String(data.output).slice(0, 400))}</div>` : '';
      execTrace(mode, (data.success ? '🛠 ' : '⚠ ') + '调用 ' + esc(data.tool), args + out, data.success ? 'action' : 'warn');
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
      break;

    case 'task_error':
      removeTyping(mode);
      finalizeStream(mode, null);
      finalizeAll(mode);
      chat().append(mode, { kind: 'msg', id: uid(), role: 'agent', md: '❌ **错误**: ' + (data.error || '') });
      offerResume(mode, '出错');
      panel().bumpRefresh();
      setRunning(false);
      break;

    case 'task_cancelled':
      finalizeStream(mode, null);
      removeTyping(mode);
      finalizeAll(mode);
      chat().append(mode, { kind: 'msg', id: uid(), role: 'agent', md: '⏹ 任务已中断' });
      offerResume(mode, '中断');
      panel().bumpRefresh();
      setRunning(false);
      break;
  }
}
