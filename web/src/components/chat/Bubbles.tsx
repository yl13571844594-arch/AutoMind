// 各类消息气泡：普通消息 / 流式 / 打字 / 执行过程 / 协同 / 循环 / 续跑按钮 / 欢迎页。
import { App } from 'antd';
import { memo, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { copyText } from '../../lib/clipboard';
import { esc, isSafeUrl, renderMarkdown, splitStream } from '../../lib/markdown';
import { MODE_LABELS, useApp, type Mode } from '../../store/app';
import { useChat, type ChatItem, type LoopIter, type MaStep, type PlanRow, type TraceItem } from '../../store/chat';
import { TEMPLATES } from '../../lib/templates';
import { useUi } from '../../store/ui';
import { usePanel } from '../../store/panel';
import WorkflowRunCard from '../workflow/WorkflowRunCard';

function Avatar({ role, icon }: { role: 'user' | 'agent'; icon?: string }) {
  return <div className="avatar">{icon || (role === 'user' ? '我' : 'AM')}</div>;
}

/**
 * 插入补充的状态徽标。
 *
 * 前提是"它和自己的普通提问长得一样"：都是用户气泡，事后翻记录时分不清哪句是
 * 执行中途后补的、哪句是本轮原本的要求，而这恰恰决定了这轮回答该不该包含它。
 * 三种定论态各有颜色，未定论态写清楚"还在等模型读"，不含糊成一句"补充"。
 */
function injectFlag(item: Extract<ChatItem, { kind: 'msg' }>): { cls: string; text: string } | null {
  if (!item.injected) return null;
  if (item.applied) return { cls: 'badge-builtin', text: '✓ 已纳入本轮' };
  // 被提升成新任务：它不是"没纳入本轮"，也不该顶着"已纳入本轮" —— 这两句话
  // 都不对，用户需要知道的是"你那句话变成了一次新任务"。
  if (item.promoted) return { cls: 'badge-plugin', text: '↗ 已作为新任务开始' };
  if (item.rejected) return { cls: 'badge-dangerous', text: '✕ 未被接受' };
  if (item.dropped) return { cls: 'badge-custom', text: '⚠ 未纳入本轮' };
  return { cls: 'badge-mcp', text: '⤴ 补充 · 待本轮读取' };
}

export const MsgBubble = memo(function MsgBubble({ item, onDelete, onResend }: {
  item: Extract<ChatItem, { kind: 'msg' }>;
  onDelete?: (id: string) => void;
  onResend?: (id: string, text: string) => void;
}) {
  const { message, modal } = App.useApp();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(item.md);
  const copy = async (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    const codeBtn = target.closest('.copy-code');
    if (codeBtn) {
      const pre = codeBtn.closest('.code-block')?.querySelector('pre');
      if (pre) {
        const ok = await copyText(pre.textContent || '');
        ok ? message.success('已复制代码') : message.error('复制失败');
      }
      return;
    }
    const msgBtn = target.closest('.copy-msg');
    if (msgBtn) {
      // 复制 Markdown 原文而不是气泡的 innerText：后者会把复制按钮自己的
      // "⧉" 字形一并带上，也会丢掉代码块的原始格式。
      const ok = await copyText(item.md);
      ok ? message.success('已复制') : message.error('复制失败');
      return;
    }
    const hb = target.closest('.hb-preview') as HTMLElement | null;
    if (hb) {
      try {
        const html = decodeURIComponent(hb.getAttribute('data-hblk') || '');
        useUi.getState().openPreview({ html, label: '内联 HTML' });
      } catch { /* ignore */ }
    }
  };
  const del = () => modal.confirm({
    title: '删除这条消息？',
    content: '仅从本地会话记录中移除，不影响已产生的文件改动。',
    okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: () => onDelete?.(item.id),
  });

  const submitEdit = () => {
    const t = text.trim();
    if (!t) { message.error('内容不能为空'); return; }
    setEditing(false);
    onResend?.(item.id, t);
  };

  const flag = injectFlag(item);

  return (
    <div className={`msg ${item.role === 'user' ? 'user' : 'agent'}`}>
      <Avatar role={item.role} />
      {/* 编辑态放宽气泡宽度：默认 82% 的气泡装不下输入框+两个按钮，会挤到换行 */}
      <div className={`col${editing ? ' editing' : ''}`}>
        <div className="bubble" onClick={copy}>
          {/* 悬停浮出的操作条。自己发出去的提问同样要能复制/编辑/删除 ——
              早期只给 agent 消息挂了复制按钮，发错问题只能靠"复制+重发"。 */}
          {!editing && (
            <div className="msg-acts">
              <button className="copy-msg" title="复制此条">⧉</button>
              {item.role === 'user' && onResend && (
                <button
                  className="msg-act"
                  title="编辑并重新发送（会移除这条之后的消息）"
                  onClick={() => { setText(item.md); setEditing(true); }}
                >✎</button>
              )}
              {onDelete && <button className="msg-act danger" title="删除此条" onClick={del}>🗑</button>}
            </div>
          )}
          {item.images && item.images.length > 0 && (
            <div className="mm-thumbs">
              {item.images.filter(isSafeUrl).map((u, i) => <img key={i} src={u} alt="img" />)}
            </div>
          )}
          {/* v1.7.7：本条消息携带的 overrides（模式/专家/附件）及服务端回执 */}
          {item.chips && item.chips.length > 0 && (
            <div className="ov-chips">
              {item.chips.map((c) => (
                <span key={c.key} className={`ov-chip ${c.state}`}
                  title={c.state === 'fail' ? `未生效：${c.note || ''}`
                    : c.state === 'pending' ? '等待服务端确认…' : '已生效'}>
                  {c.label}
                  <i>{c.state === 'ok' ? '✓' : c.state === 'fail' ? '✕' : '…'}</i>
                </span>
              ))}
            </div>
          )}
          {/* 徽标放在正文上方：它是这条消息的"结论"，读到正文之前就该看见 */}
          {flag && (
            <div className="inj-flag" title={item.injectNote || ''}>
              <span className={`badge ${flag.cls}`}>{flag.text}</span>
              {item.injectNote && <span className="hint-text">{item.injectNote}</span>}
            </div>
          )}
          {editing ? (
            <div className="msg-edit">
              <textarea
                value={text}
                autoFocus
                rows={Math.min(10, text.split('\n').length + 1)}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitEdit(); }
                  if (e.key === 'Escape') setEditing(false);
                }}
              />
              <div className="msg-edit-acts">
                <span className="hint-text">Enter 发送 · Esc 取消</span>
                <span style={{ flex: 1 }} />
                <button className="err-btn" onClick={() => setEditing(false)}>取消</button>
                <button className="err-btn primary" onClick={submitEdit}>重新发送</button>
              </div>
            </div>
          ) : (
            // md 作用域：标题/列表/表格/引用等块级排版样式统一由 .md 前缀限定，
            // 不会波及气泡里的 chips、操作条等非 Markdown 内容
            <span className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(item.md) }} />
          )}
        </div>
        <div className="time">{item.meta || ''}</div>
      </div>
    </div>
  );
});

/**
 * 流式正文：已定稿的部分**增量**解析并累积，尾巴按纯文本直出。
 *
 * 此前是 `renderMarkdown(item.buf)` —— 每 50ms 把整个回答重解析一遍。
 * 一篇 8000 字的回答意味着几百次全量解析，总开销是 O(长度²)，
 * 表现就是"前面很顺，越往后越卡，最后几百字一个一个往外蹦"。
 * 改成累积后总开销回落到 O(长度)：每段只解析一次。
 */
function StreamBody({ buf }: { buf: string }) {
  const acc = useRef({ at: 0, html: '' });
  const [prefix, tail] = splitStream(buf);
  if (prefix.length > acc.current.at) {
    // 只解析新长出来的那一段，接到已有 HTML 后面
    // （切点取在块边界上，所以分段解析与整体解析等价）
    acc.current.html += renderMarkdown(prefix.slice(acc.current.at));
    acc.current.at = prefix.length;
  } else if (prefix.length < acc.current.at) {
    // 缓冲区变短了（重发/回退）：整段重来。正常流式不会走到这里。
    acc.current = { at: prefix.length, html: renderMarkdown(prefix) };
  }
  return (
    <span className="md">
      {acc.current.html && <span dangerouslySetInnerHTML={{ __html: acc.current.html }} />}
      {/* 尾巴只做转义 —— 它每 50ms 都在变，绝不能走 Markdown 解析 */}
      <span className="stream-tail" dangerouslySetInnerHTML={{ __html: esc(tail) }} />
    </span>
  );
}

export const StreamBubble = memo(function StreamBubble(
  { item }: { item: Extract<ChatItem, { kind: 'stream' }> },
) {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble">
          <StreamBody buf={item.buf} />
          <span className="cursor">▍</span>
        </div>
      </div>
    </div>
  );
});

export const TypingBubble = memo(function TypingBubble() {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble"><div className="typing-dots"><span /><span /><span /></div></div>
      </div>
    </div>
  );
});

const Traces = memo(function Traces(
  { traces, dropped = 0 }: { traces: TraceItem[]; dropped?: number },
) {
  return (
    <div className="exec-trace">
      {dropped > 0 && (
        <div className="trace-clipped">
          ⋯ 更早的 {dropped} 条执行轨迹已折叠（面板只保留最近 300 条以免卡顿）；
          完整过程见「任务历史」与「观测中心」。
        </div>
      )}
      {traces.map((t, i) => (
        <div key={i} className={`trace-item trace-${t.kind || 'info'}`}>
          <div className="trace-label" dangerouslySetInnerHTML={{ __html: t.label }} />
          <div className="trace-body md" dangerouslySetInnerHTML={{ __html: t.body }} />
        </div>
      ))}
    </div>
  );
});

const PLAN_ICON: Record<PlanRow['state'], string> = { pending: '○', run: '◐', ok: '✓', fail: '✗' };

export const ExecBubble = memo(function ExecBubble({ item }: { item: Extract<ChatItem, { kind: 'exec' }> }) {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col" style={{ maxWidth: '92%' }}>
        <div className="bubble">
          <b>⚙️ 执行过程</b>
          {item.plan.length > 0 && (
            <div className={'trace-item trace-plan'} style={{ marginTop: 8 }}>
              <div className="trace-label">📋 已生成计划（{item.plan.length} 步）</div>
              {item.plan.map((r, i) => (
                <div key={i} className={`plan-row ${r.state}`} title={r.error || ''}>
                  {PLAN_ICON[r.state]} {r.text}
                </div>
              ))}
            </div>
          )}
          <Traces traces={item.traces} dropped={item.traceDropped} />
          {!item.done && <div className="typing-dots" style={{ marginTop: 6 }}><span /><span /><span /></div>}
        </div>
      </div>
    </div>
  );
});

const MA_ROLES: Record<string, string> = {
  planner: '🧭 规划', researcher: '🔎 研究', coder: '💻 编程', writer: '✍️ 写作', reviewer: '🧐 审阅',
};

export const MultiBubble = memo(function MultiBubble({ item }: { item: Extract<ChatItem, { kind: 'multi' }> }) {
  return (
    <div className="msg agent">
      <div className="avatar">🤝</div>
      <div className="col" style={{ maxWidth: '88%' }}>
        <div className="bubble">
          <b>🤝 多智能体协同</b>
          {item.steps.length === 0 && !item.done && (
            <div className="typing-dots" style={{ marginTop: 8 }}><span /><span /><span /></div>
          )}
          {item.steps.map((s: MaStep, i) => (
            <div key={i} className="ma-step">
              <span style={{ color: s.state === 'ok' ? 'var(--green)' : s.state === 'run' ? 'var(--yellow)' : 'var(--text3)' }}>
                {s.state === 'ok' ? '✓' : s.state === 'run' ? '◐' : '○'}
              </span>{' '}
              <b>{MA_ROLES[s.role] || s.role}</b>{' '}
              <span style={{ color: 'var(--text2)' }}>{s.subtask}</span>
              {s.output && <div className="ma-out md" dangerouslySetInnerHTML={{ __html: renderMarkdown(s.output + (s.output.length >= 600 ? ' …' : '')) }} />}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
});

const LOOP_STOP: Record<string, string> = {
  completed: '✅ 已完成', no_progress: '⏹ 连续无进展，已停止', converged: '🔄 输出已收敛，已停止',
  idle: '💤 连续多轮未执行操作，已停止', max_iterations: '⛔ 达到最大轮数',
  // v1.11.16：预算耗尽不再假装跑完 —— 明确"断点已保存，可继续"，
  // 否则循环卡片看起来只是安静地停在了某一轮。
  budget_exhausted: '⏸ Token 预算已用尽，已保存断点（可点上方「从断点续跑」继续）',
  task_budget: '⏸ Token 预算已用尽，已保存断点（可点上方「从断点续跑」继续）',
};

export const LoopBubble = memo(function LoopBubble({ item }: { item: Extract<ChatItem, { kind: 'loop' }> }) {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col" style={{ maxWidth: '90%' }}>
        <div className="bubble">
          <b>🔁 循环工程（自主迭代）</b>
          {item.iters.map((it: LoopIter) => (
            <div key={it.iter} className={`loop-card ${it.done ? 'done' : it.obs ? 'retry' : ''}`}>
              <b>第 {it.iter} 轮 / 最多 {it.max}</b>
              {!it.action && !it.done && <span className="cursor"> ▍</span>}
              {it.action && <div className="md" style={{ fontSize: '.9em', color: 'var(--text2)', marginTop: 4 }}
                dangerouslySetInnerHTML={{ __html: '🛠 ' + renderMarkdown(it.action) }} />}
              {it.done === true && <div style={{ color: 'var(--green)', marginTop: 4 }}>✓ 观察：任务已完成</div>}
              {it.done === false && it.obs && <div style={{ color: 'var(--yellow)', marginTop: 4 }}>↻ 观察：{it.obs}</div>}
            </div>
          ))}
          <Traces traces={item.traces} dropped={item.traceDropped} />
          {item.done && item.stopReason && LOOP_STOP[item.stopReason] && (
            <div style={{ marginTop: 8, fontWeight: 600 }}>{LOOP_STOP[item.stopReason]}</div>
          )}
          {!item.done && <div className="typing-dots" style={{ marginTop: 6 }}><span /><span /><span /></div>}
        </div>
      </div>
    </div>
  );
});

/**
 * 隔离工作副本的产物卡片（v1.7.7）。
 *
 * 此前产物"藏"在 .automind/workspaces/<会话> 里，回复里只有一个路径和一句
 * "调用工作区导出"——而导出根本没有入口。现在：列出改了哪些文件，一键应用到
 * 项目（冲突先问，可在「↩️ 文件改动」撤销）、导出，或删除副本释放磁盘。
 */
export const WorkspaceBubble = memo(function WorkspaceBubble({ item }: {
  item: Extract<ChatItem, { kind: 'workspace' }>;
}) {
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState('');
  const files = [...item.added.map((f) => ({ f, t: '新增' })), ...item.modified.map((f) => ({ f, t: '修改' }))];
  const owner = (localStorage.getItem('automind_sid') || '');
  const patch = (p: Partial<Extract<ChatItem, { kind: 'workspace' }>>) => {
    const cm = useChat.getState();
    for (const m of Object.keys(cm.messages) as any[]) {
      if ((cm.messages as any)[m]?.some((i: any) => i.id === item.id)) {
        cm.update(m, item.id, (i: any) => ({ ...i, ...p }));
      }
    }
    cm.persist();
  };

  // v1.11.6：先预览「写到哪个项目根、哪些是覆盖」，确认后按相对路径整份覆盖回去
  const doApply = async () => {
    setBusy('apply');
    try {
      const r = await apiPost(`/session-workspaces/${encodeURIComponent(item.sid)}/apply`, { owner });
      if (r.error) { message.error(r.error); return; }
      const applied = [...new Set([...(item.applied || []), ...(r.applied || [])])];
      patch({ applied, conflicts: r.overwritten_changed || [] });
      const failed: { path: string; error: string }[] = r.failed || [];
      const lines = [`已把 ${(r.applied || []).length} 个文件按相对路径应用到 ${r.project}`];
      if (r.backup_dir) lines.push(`被覆盖的原文件已备份到 ${r.backup_dir}`);
      if (failed.length) {
        modal.warning({
          title: `${failed.length} 个文件未能应用`,
          content: <div style={{ maxHeight: 220, overflow: 'auto' }}>
            {lines.map((l) => <div key={l}>{l}</div>)}
            <ul>{failed.map((f) => <li key={f.path}><span className="mono">{f.path}</span>：{f.error}</li>)}</ul>
          </div>,
        });
      } else {
        message.success(lines.join('；') + '（可在「↩️ 文件改动」撤销）', 6);
      }
    } catch (e: any) {
      message.error(`应用失败：${e?.friendly || e?.message || e}`);
    } finally { setBusy(''); }
  };

  const apply = async () => {
    setBusy('apply');
    let plan: any;
    try {
      plan = await apiGet(`/session-workspaces/${encodeURIComponent(item.sid)}/apply-plan?owner=${encodeURIComponent(owner)}`);
    } catch (e: any) {
      message.error(`读取副本失败：${e?.friendly || e?.message || e}`); setBusy(''); return;
    }
    setBusy('');
    if (!plan?.ok) { message.error(plan?.error || '读取副本失败'); return; }
    const list: { path: string; action: string; changed_in_project: boolean }[] = plan.files || [];
    if (!list.length) { message.info('副本里没有待应用的改动'); patch({ applied: files.map((x) => x.f) }); return; }
    const over = list.filter((f) => f.action === 'overwrite');
    const touched = list.filter((f) => f.changed_in_project);
    modal.confirm({
      title: `应用到项目：新建 ${list.length - over.length} 个，覆盖 ${over.length} 个`,
      width: 560,
      content: <div>
        <div className="hint-text">目标项目根（按副本里的相对路径写入，同目录同名文件直接覆盖）：</div>
        <div className="mono" style={{ wordBreak: 'break-all', marginBottom: 6 }}>{plan.project}</div>
        <div style={{ maxHeight: 220, overflow: 'auto', fontSize: '.86em' }}>
          {list.map((f) => (
            <div key={f.path} className="mono">
              <span className={`ws-tag ${f.action === 'overwrite' ? 'mod' : 'add'}`}>{f.action === 'overwrite' ? '覆盖' : '新建'}</span>
              {f.path}{f.changed_in_project && <span className="ws-bad">项目里也改过</span>}
            </div>
          ))}
        </div>
        {touched.length > 0 && <div style={{ color: 'var(--yellow)', marginTop: 6 }}>
          ⚠ {touched.length} 个文件在任务期间项目里也被改过，覆盖前会自动备份。</div>}
        <div className="hint-text" style={{ marginTop: 6 }}>覆盖前原文件会备份到副本的 _apply_backup 目录，文本文件还可在「↩️ 文件改动」一键撤销。</div>
      </div>,
      okText: '确认覆盖到项目', cancelText: '取消',
      onOk: doApply,
    });
  };

  const exportAll = async () => {
    setBusy('export');
    try {
      const r = await apiPost(`/session-workspaces/${encodeURIComponent(item.sid)}/export`, { owner });
      if (r.ok) message.success(`已导出 ${r.count} 个文件到：${r.dir}`, 6);
      else message.error(r.error || '导出失败');
    } catch (e: any) { message.error(`导出失败：${e?.friendly || e?.message || e}`); } finally { setBusy(''); }
  };

  const removeCopy = () => modal.confirm({
    title: '删除这个会话的隔离副本？',
    content: '副本里未应用到项目的改动将被丢弃。下次在该会话跑任务时会重新创建副本。',
    okText: '删除', okButtonProps: { danger: true },
    onOk: async () => {
      try {
        await apiDelete(`/session-workspaces/${encodeURIComponent(item.sid)}?owner=${encodeURIComponent(owner)}`);
        patch({ removed: true });
        message.info('已删除隔离副本');
      } catch (e: any) { message.error(`删除失败：${e?.friendly || e?.message || e}`); }
    },
  });

  const appliedAll = !!item.applied && files.every(({ f }) => item.applied!.includes(f));
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble ws-card">
          <div className="ws-card-head">
            <b>📁 隔离副本产物</b>
            <span className="hint-text">新增 {item.added.length} · 修改 {item.modified.length}
              {item.deleted.length ? ` · 删除 ${item.deleted.length}（不会同步到项目）` : ''}</span>
          </div>
          <div className="hint-text mono ws-card-path" title={item.path}>{item.path}</div>
          <div className="ws-card-files">
            {(open ? files : files.slice(0, 5)).map(({ f, t }) => (
              <div key={f} className="mono">
                <span className={`ws-tag ${t === '新增' ? 'add' : 'mod'}`}>{t}</span>{f}
                {item.applied?.includes(f) && <span className="ws-ok">✓ 已应用</span>}
                {item.conflicts?.includes(f) && <span className="ws-bad">已覆盖你的改动（有备份）</span>}
              </div>
            ))}
            {files.length > 5 && (
              <a onClick={() => setOpen(!open)}>{open ? '收起' : `展开全部 ${files.length} 个`}</a>
            )}
          </div>
          {item.removed ? <div className="hint-text">副本已删除</div> : (
            <div className="ws-card-acts">
              <button className="err-btn primary" disabled={!!busy || appliedAll} onClick={apply}>
                {appliedAll ? '✓ 已全部应用' : busy === 'apply' ? '应用中…' : '⤵ 应用到项目'}
              </button>
              <button className="err-btn" disabled={!!busy} onClick={exportAll}>📦 导出</button>
              <button className="err-btn" disabled={!!busy} onClick={removeCopy}>🗑 删除副本</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

/**
 * 📦 交付卡片（v1.11.11 presented_files）—— 模型用 present 工具明确声明的交付物。
 *
 * 为什么单独一张卡片而不是塞进回复正文：交付物要能**点开**（预览 / 代码标签 / 复制路径），
 * 纯 Markdown 链接做不到这一点；而且随消息落盘后，翻历史记录时仍能一键打开当时的产物。
 * 只存路径与说明：文件内容随时可能再被改，点开时现读才是最新的。
 */
const OPEN_PREVIEW_RE = /\.(?:html?|svg|png|jpe?g|gif|webp|bmp|ico)$/i;
/** v1.11.14：这些后缀走「📄 文档视图」而不是代码编辑器 / iframe 源码 */
const MD_DOC_RE = /\.(?:md|markdown|mdx)$/i;

export const DeliverableBubble = memo(function DeliverableBubble({ item }: {
  item: Extract<ChatItem, { kind: 'deliverable' }>;
}) {
  const { message } = App.useApp();
  const files = item.files || [];
  const nameOf = (f: { path: string; name?: string }) =>
    f.name || f.path.split(/[\\/]/).pop() || f.path;
  // 打开方式：Markdown 文档交给文档视图（渲染排版，不再是 iframe 里的源码）；
  // HTML/图片类交给预览弹窗（服务端按文件 MIME 返回；SVG 可内嵌渲染），
  // 其它文件切到「代码」标签用编辑器打开。绝对路径与相对路径两端都接受（服务端校验项目边界）。
  const openFile = (path: string) => {
    if (MD_DOC_RE.test(path)) {
      useUi.getState().openDoc({ path, label: path });
    } else if (OPEN_PREVIEW_RE.test(path)) {
      useUi.getState().openPreview({ url: `/api/preview/file?path=${encodeURIComponent(path)}`, label: path });
    } else {
      usePanel.getState().openInCode(path);
    }
  };
  const copyPath = async (path: string) => {
    const ok = await copyText(path);
    ok ? message.success('已复制文件路径') : message.error('复制失败');
  };

  return (
    <div className="msg agent">
      <Avatar role="agent" icon="📦" />
      <div className="col">
        <div className="bubble dlv-card">
          <div className="dlv-head">
            <b>📦 交付文件</b>
            <span className="hint-text">{item.count || files.length} 个 · 点击打开</span>
          </div>
          {files.map((f) => (
            <div key={f.path} className="dlv-row">
              <span className="dlv-main" title={f.path}>
                <a className="dlv-name" onClick={() => openFile(f.path)}>
                  {nameOf(f)}
                  {MD_DOC_RE.test(f.path) && (
                    <span className="dlv-md-tag" title="以文档视图打开（渲染 Markdown 排版）">文档视图</span>
                  )}
                </a>
                {f.description && <span className="dlv-desc">{f.description}</span>}
                <span className="dlv-path mono">{f.path}</span>
              </span>
              <button className="dlv-copy" title="复制文件路径" onClick={() => copyPath(f.path)}>⧉</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
});

/** v1.8.0 工作流运行卡片（对话区）：进度实时来自 store/workflow，与「工作流」视图一致。 */
export const WorkflowBubble = memo(function WorkflowBubble({ item }: {
  item: Extract<ChatItem, { kind: 'workflow' }>;
}) {
  return (
    <div className="msg agent">
      <Avatar role="agent" icon="🔀" />
      <div className="col" style={{ minWidth: 0, flex: 1 }}>
        <WorkflowRunCard runId={item.runId} compact
          onOpen={() => useApp.getState().setView('workflow')} />
      </div>
    </div>
  );
});

export const ResumeBubble = memo(function ResumeBubble({ item, onResume }: {
  item: Extract<ChatItem, { kind: 'resume' }>; onResume: () => void;
}) {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble">
          <button className="hb-preview" onClick={onResume}>▶ 检查现状后接着做</button>
          <span className="hint-text" style={{ marginLeft: 8 }}>
            重发该任务并要求先查看当前进度、尽量跳过已完成的部分（已产出的文件保留）
          </span>
        </div>
      </div>
    </div>
  );
});

// 常见失败给一句"该怎么办"，而不是把原始报错甩给用户就完事
function diagnose(err: string): string | null {
  const e = err.toLowerCase();
  if (/api[_ ]?key|unauthorized|401|invalid.*key/.test(e))
    return '多半是 API Key 未配置或已失效 —— 打开「⚙ 设置 → 🔑 API Keys」检查。';
  if (/quota|insufficient|余额|欠费|429|rate.?limit/.test(e))
    return '模型侧限流或额度不足 —— 稍等片刻再重试，或在设置里换一个模型。';
  if (/timeout|timed out|超时/.test(e))
    return '请求超时 —— 网络或模型侧较慢，可直接重试；长任务建议拆小。';
  if (/connect|network|dns|ssl|proxy|econn|无法连接模型服务|502|503|504|bad gateway/.test(e))
    return '模型服务连不上（已自动重连多次仍失败）—— 检查代理/VPN 与中转地址是否可达；'
      + '可在「⚙ 设置 → 🤖 模型」里点「测试连接」确认。恢复后点「接着做」，已完成的步骤不会重跑。';
  if (/context length|maximum context|too many tokens/.test(e))
    return '上下文超长 —— 新开会话或精简输入后重试。';
  return null;
}

export const ErrorBubble = memo(function ErrorBubble({ item, onResume, onRetry }: {
  item: Extract<ChatItem, { kind: 'error' }>;
  onResume: (item: Extract<ChatItem, { kind: 'error' }>) => void;
  onRetry: (item: Extract<ChatItem, { kind: 'error' }>) => void;
}) {
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const hint = diagnose(item.error || '');
  const long = (item.error || '').length > 160;
  const shown = long && !open ? item.error.slice(0, 160) + ' …' : item.error;

  return (
    <div className="msg agent">
      <div className="avatar err">!</div>
      <div className="col" style={{ maxWidth: '92%' }}>
        <div className="bubble err-card">
          <div className="err-head">
            {item.why === '中断' ? '⏹ 任务已中断' : '❌ 任务失败'}
            {item.at && <span className="err-at">{item.at}</span>}
          </div>
          {item.error && <div className="err-body">{shown}</div>}
          {long && (
            <button className="err-more" onClick={() => setOpen(!open)}>
              {open ? '收起' : '展开完整报错'}
            </button>
          )}
          {hint && <div className="err-hint">💡 {hint}</div>}
          <div className="err-actions">
            {item.task && (
              <>
                {/* 措辞刻意不写"从断点继续"：后端并没有把执行进度传回来，这里能做的
                    只是重发原任务、并额外要求模型先查看现状。说成"断点续传"会让用户
                    以为已完成的步骤一定不会重做 —— 那是做不到的承诺。 */}
                <button
                  className="err-btn primary"
                  title="重发原任务，并要求先检查当前进度、尽量跳过已完成的部分。这是给模型的附加提示，不是断点续传。"
                  onClick={() => onResume(item)}
                >▶ 检查现状后接着做</button>
                <button
                  className="err-btn"
                  title="原样重发这条任务，不附加任何提示（从头开始）"
                  onClick={() => onRetry(item)}
                >↻ 从头重跑</button>
              </>
            )}
            <button
              className="err-btn"
              onClick={async () => {
                const ok = await copyText(
                  `【${item.why}】${item.at || ''}\n${item.error}` +
                  (item.task ? `\n\n原任务：\n${item.task}` : ''));
                ok ? message.success('已复制错误详情') : message.error('复制失败');
              }}
            >⧉ 复制错误</button>
          </div>
          {item.task && (
            <>
              <div className="err-task" title={item.task}>
                原任务：{item.task.length > 90 ? item.task.slice(0, 90) + '…' : item.task}
              </div>
              <div className="err-fineprint">
                两者都会重新执行一次任务；已写出的文件不会被回滚。
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
});

// v1.11.4 初始界面：从"一个写满字的气泡"改为欢迎页 —— 品牌标题、五种模式卡片
// （点一下就切过去）、快速开始模板卡片、未配置模型时的醒目引导、新输入方式提示。
const WELCOME_MODES: { m: Mode; icon: string; desc: string; feature?: string }[] = [
  { m: 'chat', icon: '💬', desc: '像聊天一样问答，支持图片与知识库检索' },
  { m: 'work', icon: '⚙️', desc: '自主规划并执行：建项目、跑命令、改文件' },
  { m: 'coding', icon: '💻', desc: '读写代码、调试、重构、补测试' },
  { m: 'multi', icon: '🤝', desc: '多个智能体分工协作完成复杂任务', feature: 'multi_agent' },
  { m: 'loop', icon: '🔁', desc: '行动 → 观察 → 修正，循环直到达标', feature: 'loop_engine' },
];

export function WelcomeBubble({ onTemplate, onAllTemplates }: {
  onTemplate: (i: number) => void; onAllTemplates: () => void;
}) {
  const featureOn = useApp((s) => s.featureOn);
  const mode = useApp((s) => s.mode);
  const status = useApp((s) => s.status);
  const needSetup = !!status && !status.llm_ready;
  const pickMode = (m: Mode, feature?: string) => {
    if (feature && !featureOn(feature)) { useUi.getState().openModal('license'); return; }
    useApp.getState().setMode(m);
  };
  return (
    <div className="welcome">
      <div className="wl-hero">
        <div className="wl-logo" aria-hidden="true" />
        <h1>你好，我是 <span className="wl-grad">AutoMind</span></h1>
        <p>本地运行的通用自动化 Agent —— 能对话、能干活、能写代码，数据不出你的电脑。</p>
      </div>

      {needSetup && (
        <div className="wl-setup">
          <span className="wl-setup-ico">🔑</span>
          <div className="wl-setup-text">
            <b>还差一步：配置模型</b>
            <span>支持 OpenAI / Claude / DeepSeek / Kimi / 百炼 / 智谱 / 豆包 / Gemini / Ollama 及自定义中转</span>
          </div>
          <button className="wl-btn primary" onClick={() => useUi.getState().openModal('apikeys')}>去配置 API Key</button>
        </div>
      )}

      <div className="wl-sec">选择工作模式</div>
      <div className="wl-modes">
        {WELCOME_MODES.map(({ m, icon, desc, feature }) => {
          const locked = !!feature && !featureOn(feature);
          return (
            <button key={m} className={'wl-mode' + (mode === m ? ' on' : '') + (locked ? ' locked' : '')}
              onClick={() => pickMode(m, feature)} title={locked ? '专业版功能：点击了解升级' : `切换到${MODE_LABELS[m]}模式`}>
              <span className="wl-mode-ico">{icon}</span>
              <span className="wl-mode-name">{MODE_LABELS[m]}{locked && <i>专业版</i>}{mode === m && <i className="cur">当前</i>}</span>
              <span className="wl-mode-desc">{desc}</span>
            </button>
          );
        })}
      </div>

      <div className="wl-sec">快速开始 <span>点一下填入输入框</span></div>
      <div className="wl-tpls">
        {TEMPLATES.slice(0, 5).map((t, i) => (
          <button key={i} className="wl-tpl" onClick={() => onTemplate(i)}>
            <span className="wl-tpl-ico">{t.icon}</span>
            <span className="wl-tpl-text"><b>{t.title}</b><span>{t.desc}</span></span>
          </button>
        ))}
        <button className="wl-tpl more" onClick={onAllTemplates}>
          <span className="wl-tpl-ico">📚</span>
          <span className="wl-tpl-text"><b>全部模板</b><span>浏览更多场景</span></span>
        </button>
      </div>

      <div className="wl-tips">
        <span><kbd>/</kbd> 调用指令、技能与工具</span>
        <span><kbd>@</kbd> 引用文件或对话</span>
        <span><kbd>Shift</kbd>+<kbd>Enter</kbd> 换行</span>
        <span>📚 侧栏「知识库」上传文档后自动检索</span>
      </div>
    </div>
  );
}
export { MODE_LABELS };
