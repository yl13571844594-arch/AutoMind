// v1.7.7：输入框加号菜单的 overrides 协议 + 断点续跑的前端侧。
//
// overrides 只作用于**这一条消息**（发完即清）：
//   mode        —— 用某个模式发这条，不改默认模式
//   expert      —— 这条用某个专家；'' = 这条不用专家；undefined = 跟随全局
//   attachments —— 文本附件（图片仍走 images 通道）
//   mcp / skills —— v1.11.0 引用 MCP 连接器 / 技能（与专家同构，本条生效）
//   plan / goal  —— v1.11.0 计划模式 / 长期目标：来自输入框的常驻开关（store/refs），
//                   发送时并进来，不随发送清空
//   model        —— v1.11.15 输入框模型选择（常驻，见 store/modelChoice）：
//                   发送时并入 overrides.model_mode/provider/model，不随发送清空
// 服务端在 task_start 里回一份逐项回执，用户消息上的 chip 据此变绿/变红。
import { apiGet, apiPost } from '../api/client';
import { MODE_LABELS, type Mode } from '../store/app';
import type { MsgChip } from '../store/chat';

export interface Attachment { name: string; text: string; size: number }
/** v1.11.2 输入框 @ 引用：文件 / 目录 / 对话。token 是插进输入框里的那段文字，
 *  发送时用它判断"用户是不是又把这个引用删掉了"。 */
export interface Mention {
  type: 'file' | 'dir' | 'conversation';
  path?: string;
  sid?: string;
  suffix?: string;
  title?: string;
  token: string;
}
/** v1.11.15 本条消息的模型选择（常驻；default 表示不带 model_mode 字段）。 */
export interface PendingModel {
  mode: 'manual' | 'auto';
  provider?: string;
  model?: string;
}
export interface PendingOverrides {
  mode?: Mode;
  expert?: { id: string; name: string };   // id '' = 本条不用专家
  attachments: Attachment[];
  mcp?: string[];                          // 引用的 MCP 连接器（服务器名）
  skills?: string[];                       // 引用的技能（技能名）
  plan?: boolean;                          // 计划模式（常驻，发送时并入）
  goal?: { id: string; title: string };    // 长期目标（常驻，发送时并入）
  mentions?: Mention[];                    // v1.11.2 @文件 / @目录 / @对话
  tools?: string[];                        // v1.11.2 / 指令点名的工具（含插件 / MCP 单个工具）
  model?: PendingModel;                    // v1.11.15 模型选择（常驻，发送时并入）
}

export const EMPTY_OVERRIDES: PendingOverrides = { attachments: [] };

/** 本条（一次性）设置是否非空 —— 不含常驻的模型选择 / 计划模式 / 目标。 */
export function hasOverrides(o: PendingOverrides): boolean {
  return !!(o.mode || o.expert || o.attachments.length || o.mcp?.length || o.skills?.length
    || o.mentions?.length || o.tools?.length);
}

/** 在列表里切换一项（引用菜单的勾选 / 取消）。 */
export function toggleIn(list: string[] | undefined, v: string): string[] {
  const l = list || [];
  return l.includes(v) ? l.filter((x) => x !== v) : [...l, v];
}

/** 发给服务端的 overrides 字段（没有就不带）。 */
export function toWire(o: PendingOverrides): Record<string, any> | undefined {
  if (!hasOverrides(o) && !o.plan && !o.goal && !o.model) return undefined;
  const w: Record<string, any> = {};
  if (o.mode) w.mode = o.mode;
  if (o.expert) w.expert = o.expert.id;
  if (o.attachments.length) w.attachments = o.attachments.map((a) => ({ name: a.name, text: a.text }));
  if (o.mcp?.length) w.mcp = o.mcp;
  if (o.skills?.length) w.skills = o.skills;
  if (o.tools?.length) w.tools = o.tools;
  if (o.mentions?.length) {
    w.mentions = o.mentions.map((m) => (m.type === 'conversation'
      ? { type: m.type, sid: m.sid, title: m.title, transcript: m.suffix !== undefined ? transcriptFor(m.suffix) : undefined }
      : { type: m.type, path: m.path }));
  }
  if (o.plan) w.plan_review = true;
  if (o.goal) w.goal = o.goal.id;
  if (o.model) {
    w.model_mode = o.model.mode;
    if (o.model.mode === 'manual') {
      w.provider = o.model.provider;
      w.model = o.model.model;
    }
  }
  return w;
}

/** 发送时挂在用户消息上的 chip（pending）。 */
export function pendingChips(o: PendingOverrides): MsgChip[] {
  const chips: MsgChip[] = [];
  if (o.mode) chips.push({ key: 'mode', label: `${MODE_ICON[o.mode]} ${MODE_LABELS[o.mode]}模式`, state: 'pending' });
  if (o.expert) chips.push({ key: 'expert', label: o.expert.id ? `🎓 ${o.expert.name}` : '🎓 不使用专家', state: 'pending' });
  for (const a of o.attachments) chips.push({ key: 'att:' + a.name, label: `📄 ${a.name}`, state: 'pending' });
  for (const m of o.mcp || []) chips.push({ key: 'mcp:' + m, label: `🔌 ${m}`, state: 'pending' });
  for (const s of o.skills || []) chips.push({ key: 'skill:' + s, label: `🧩 ${s}`, state: 'pending' });
  for (const t of o.tools || []) chips.push({ key: 'tool:' + t, label: `🔧 ${t}`, state: 'pending' });
  for (const m of o.mentions || []) chips.push({ key: 'mention:' + mentionKey(m), label: mentionLabel(m), state: 'pending' });
  if (o.plan) chips.push({ key: 'plan', label: '📋 计划模式', state: 'pending' });
  if (o.goal) chips.push({ key: 'goal', label: `🎯 ${o.goal.title}`, state: 'pending' });
  if (o.model) {
    chips.push({
      key: 'model',
      label: o.model.mode === 'auto' ? '🧠 自动路由'
        : `🧠 ${o.model.provider}/${o.model.model}`,
      state: 'pending',
    });
  }
  return chips;
}

/** 用服务端回执落定 chip 状态；回执里没提到的一律视为未生效（不假装成功）。 */
export function settleChips(chips: MsgChip[], receipt: any): MsgChip[] {
  const r = receipt || {};
  const att = r.attachments || {};
  const applied: string[] = att.applied || [];
  const dropped: { name: string; reason: string }[] = att.dropped || [];
  return chips.map((c) => {
    if (c.key === 'mode' && r.mode) {
      return { ...c, state: r.mode.applied ? 'ok' : 'fail', note: r.mode.reason };
    }
    if (c.key === 'expert' && r.expert) {
      // 服务端回的名字带专家自己的图标（如 "⚖ 法务顾问"），以它为准
      return { ...c, state: r.expert.applied ? 'ok' : 'fail', note: r.expert.reason,
        label: r.expert.applied && r.expert.name ? r.expert.name : c.label };
    }
    if (c.key === 'model' && r.model) {
      // v1.11.15：服务端回执说明本条消息实际用的模型。生效时以回执的 label 为准
      // （自动路由会带上档位，如 "🧠 自动路由 · 强力"），未生效时显示原因 ——
      // 用户手选了模型却没生效，必须让他看见，而不是悄悄用默认模型跑完。
      const m = r.model as any;
      if (m.applied) {
        const label = m.label || (m.mode === 'auto'
          ? (m.tier ? `🧠 自动路由 · ${m.tier}` : '🧠 自动路由')
          : `🧠 ${m.provider || ''}/${m.model || ''}`);
        const note = m.note
          || (m.tier && m.score != null ? `档位 ${m.tier} · 复杂度 ${m.score}` : undefined);
        return { ...c, state: 'ok', label, note };
      }
      return { ...c, state: 'fail', note: m.reason || '服务端未确认（可能是旧版服务端）' };
    }
    if (c.key === 'plan' && r.plan) {
      return { ...c, state: r.plan.applied ? 'ok' : 'fail', note: r.plan.reason || r.plan.note };
    }
    if (c.key === 'goal' && r.goal) {
      return { ...c, state: r.goal.applied ? 'ok' : 'fail', note: r.goal.reason
        || (r.goal.applied ? `已推进 ${r.goal.runs ?? 0} 轮，本轮结束后记入目标` : undefined) };
    }
    if (c.key.startsWith('mcp:') || c.key.startsWith('skill:')) {
      const isMcp = c.key.startsWith('mcp:');
      const name = c.key.slice(isMcp ? 4 : 6);
      const sec = (isMcp ? r.mcp : r.skills) || {};
      const ok = (sec.applied || []).find((x: any) => x.name === name);
      if (ok) {
        return { ...c, state: 'ok', note: isMcp ? `${ok.tools} 个工具已下发给模型；${sec.note || ''}`
          : (ok.kind === 'markdown' ? '技能指令已注入' : '可经 skill_invoke 调用') };
      }
      const d = (sec.dropped || []).find((x: any) => x.name === name);
      return { ...c, state: 'fail', note: d?.reason || '服务端未确认（可能是旧版服务端）' };
    }
    if (c.key.startsWith('tool:')) {
      const name = c.key.slice(5);
      const sec = r.tools || {};
      const ok = (sec.applied || []).find((x: any) => x.name === name);
      if (ok) return { ...c, state: 'ok', note: ok.tool !== name ? `已解析为 ${ok.tool}，本条必定下发` : '本条必定下发给模型' };
      const d = (sec.dropped || []).find((x: any) => x.name === name);
      return { ...c, state: 'fail', note: d?.reason || '服务端未确认（可能是旧版服务端）' };
    }
    if (c.key.startsWith('mention:')) {
      const name = c.key.slice(8).replace(/^(file|dir|conversation):/, '');
      const sec = r.mentions || {};
      const ok = (sec.applied || []).find((x: any) => x.name === name);
      if (ok) return { ...c, state: 'ok', note: '内容已读取并随消息交给模型' };
      const d = (sec.dropped || []).find((x: any) => x.name === name);
      return { ...c, state: 'fail', note: d?.reason || '服务端未确认（可能是旧版服务端）' };
    }
    if (c.key.startsWith('att:')) {
      const name = c.key.slice(4);
      if (applied.includes(name)) return { ...c, state: 'ok' };
      const d = dropped.find((x) => x.name === name);
      return { ...c, state: 'fail', note: d?.reason || '服务端未确认收到' };
    }
    if (c.state === 'pending') return { ...c, state: 'fail', note: '服务端未确认（可能是旧版服务端）' };
    return c;
  });
}

/** 引用的稳定键（chip key 用）：与服务端回执里的 name 对得上 */
export function mentionKey(m: Mention): string {
  return `${m.type}:${m.type === 'conversation' ? (m.title || m.sid || '') : (m.path || '')}`;
}
export function mentionLabel(m: Mention): string {
  if (m.type === 'conversation') return `💬 ${m.title || '对话'}`;
  return `${m.type === 'dir' ? '📁' : '📄'} ${(m.path || '').split('/').pop() || m.path}`;
}
/** 发送前：输入框里已经删掉的 @ 引用一并丢掉（用户删字 = 不要它了） */
export function liveMentions(text: string, ms: Mention[] | undefined): Mention[] {
  return (ms || []).filter((m) => text.includes(m.token));
}

// 对话记录由会话目录提供（store/conv 注入，避免 lib 反向依赖 store 造成循环引用）
let transcriptFor: (suffix: string) => { role: string; content: string }[] = () => [];
export function setTranscriptProvider(fn: typeof transcriptFor): void { transcriptFor = fn; }

export const MODE_ICON: Record<Mode, string> = {
  chat: '💬', work: '⚙️', coding: '💻', multi: '🤝', loop: '🔁',
};

// 可作为文本附件读取的扩展名（其余非图片文件提示走知识库）
const TEXT_EXT = /\.(txt|md|markdown|py|js|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|conf|csv|tsv|log|xml|html?|css|scss|less|sql|sh|bat|ps1|java|kt|go|rs|c|cc|cpp|h|hpp|cs|php|rb|swift|vue|svelte|env|gitignore|dockerfile)$/i;
export const MAX_ATTACH_BYTES = 1024 * 1024;

export function isTextFile(f: File): boolean {
  return f.type.startsWith('text/') || TEXT_EXT.test(f.name) || f.type === 'application/json';
}

// ── 断点续跑 ────────────────────────────────────────────

/**
 * 可续跑摘要（GET /api/checkpoints/resumable）。
 *
 * v1.11.16：kind 从两种扩到四种（plan / react / multi / loop），进度统一放在
 * ``progress`` 里（键随 kind 而定）；plan/react 的旧扁平字段（done/total/
 * remaining/artifacts）仍然保留，旧版 UI 不需要改也能显示。
 */
export interface ResumableInfo {
  id: string; kind: 'plan' | 'react' | 'multi' | 'loop'; task: string; interaction: string;
  run_mode?: string;
  /** kind 专属进度：plan={total,done,remaining}，react={completed_actions,artifacts}，
      multi={steps_done,steps_total,last_output}，loop={iterations,last_output,feedback} */
  progress?: {
    total?: number; done?: number; remaining?: number;
    completed_actions?: number; failed_actions?: number; artifacts?: string[];
    steps_done?: number; steps_total?: number;
    iterations?: number; last_output?: string; feedback?: string;
  };
  // ── 兼容旧前端的扁平字段 ──
  done?: number; total?: number; remaining?: number; failed?: number;
  artifacts?: string[];
  steps_done?: number; steps_total?: number; iterations?: number; last_output?: string;
  status?: string; stop_reason?: string; updated?: number; budget_exhausted?: boolean;
}

export async function fetchResumable(sid: string): Promise<ResumableInfo | null> {
  try {
    const d = await apiGet(`/checkpoints/resumable?session_id=${encodeURIComponent(sid)}`);
    return d.resumable || null;
  } catch { return null; }
}

export async function dismissResumable(id: string, sid: string): Promise<void> {
  try { await apiPost(`/checkpoints/${encodeURIComponent(id)}/dismiss`, { session_id: sid }); } catch { /* ignore */ }
}

/** 断点类型的图标（横幅与重跑卡片共用）。 */
export const RESUMABLE_ICON: Record<string, string> = {
  plan: '📋', react: '🔧', multi: '🤝', loop: '🔁',
};

/**
 * 一句话说明"上次做到哪了"。
 *
 * 四种形态的进度单位完全不同（计划是步、ReAct 是动作/产物、协同是角色步骤、
 * 循环是迭代轮数），所以这里按 kind 分别措辞 —— 统一写成"N 步"会让用户
 * 按着错误的预期点「继续」。
 */
export function describeResumable(r: ResumableInfo): string {
  const p = r.progress || {};
  switch (r.kind) {
    case 'plan': {
      const done = r.done ?? p.done ?? 0;
      const total = r.total ?? p.total ?? 0;
      const remaining = r.remaining ?? p.remaining ?? Math.max(0, total - done);
      return `计划已完成 ${done}/${total} 步，本次只执行剩余 ${remaining} 步`;
    }
    case 'multi': {
      const done = r.steps_done ?? p.steps_done ?? r.done ?? 0;
      const total = r.steps_total ?? p.steps_total ?? r.total ?? 0;
      return total
        ? `协同已完成 ${done}/${total} 个角色步骤（跳过不重做），剩余 ${Math.max(0, total - done)} 步`
        : `协同已完成 ${done} 个角色步骤，跳过不重做`;
    }
    case 'loop': {
      const it = r.iterations ?? p.iterations ?? 0;
      return it > 0 ? `已跑完 ${it} 轮，从第 ${it + 1} 轮继续` : '从第 1 轮继续';
    }
    default: {
      const n = r.done ?? p.completed_actions ?? 0;
      const arts = (r.artifacts ?? p.artifacts)?.length ?? 0;
      return n
        ? `已真实完成 ${n} 个动作${arts ? `，产出 ${arts} 个文件` : ''}（不会重做）`
        : '尚无已完成的动作，从当前进度继续';
    }
  }
}

// 旧的"文本续跑"前缀（没有可用断点时的兜底）。v1.7.7 修复：续跑前先剥掉已有前缀，
// 否则连续续跑两次会把前缀嵌套成两层、三层。
export const TEXT_RESUME_PREFIX = '下面这个任务此前执行到一半中断了。请先检查当前的实际进度（已存在的文件、'
  + '已完成的改动），已经做好的部分就不必重做，从尚未完成的地方接着做：\n';

export function stripResumePrefix(text: string): string {
  let t = text;
  while (t.startsWith(TEXT_RESUME_PREFIX)) t = t.slice(TEXT_RESUME_PREFIX.length);
  return t.replace(/^↻ 从断点续跑：/, '');
}
