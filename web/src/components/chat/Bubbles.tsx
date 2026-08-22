// 各类消息气泡：普通消息 / 流式 / 打字 / 执行过程 / 协同 / 循环 / 续跑按钮 / 欢迎页。
import { App } from 'antd';
import { memo, useRef, useState } from 'react';
import { copyText } from '../../lib/clipboard';
import { esc, isSafeUrl, renderMarkdown, splitStream } from '../../lib/markdown';
import { MODE_LABELS, useApp } from '../../store/app';
import type { ChatItem, LoopIter, MaStep, PlanRow, TraceItem } from '../../store/chat';
import { TEMPLATES } from '../../lib/templates';
import { useUi } from '../../store/ui';

function Avatar({ role, icon }: { role: 'user' | 'agent'; icon?: string }) {
  return <div className="avatar">{icon || (role === 'user' ? '我' : 'AM')}</div>;
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
            <span dangerouslySetInnerHTML={{ __html: renderMarkdown(item.md) }} />
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
    <>
      {acc.current.html && <span dangerouslySetInnerHTML={{ __html: acc.current.html }} />}
      {/* 尾巴只做转义 —— 它每 50ms 都在变，绝不能走 Markdown 解析 */}
      <span className="stream-tail" dangerouslySetInnerHTML={{ __html: esc(tail) }} />
    </>
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
          <div className="trace-body" dangerouslySetInnerHTML={{ __html: t.body }} />
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
              {s.output && <div className="ma-out" dangerouslySetInnerHTML={{ __html: renderMarkdown(s.output + (s.output.length >= 600 ? ' …' : '')) }} />}
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
              {it.action && <div style={{ fontSize: '.9em', color: 'var(--text2)', marginTop: 4 }}
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
  if (/connect|network|dns|ssl|proxy|econn/.test(e))
    return '网络连不通 —— 检查代理/中转地址是否可达，再重试。';
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

export function WelcomeBubble({ onTemplate, onAllTemplates }: {
  onTemplate: (i: number) => void; onAllTemplates: () => void;
}) {
  const featureOn = useApp((s) => s.featureOn);
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble">
          <b>👋 欢迎使用 AutoMind 通用自动化 Agent</b><br /><br />
          顶部可切换五种模式：<br />
          • 💬 <b>对话</b> — 像聊天一样问答交流（支持图片输入 / 知识库自动检索）<br />
          • ⚙️ <b>工作</b> — 自主规划并执行任务（建项目、跑命令、改文件）<br />
          • 💻 <b>编程</b> — 聚焦代码：阅读、编写、调试、重构、测试<br />
          • 🤝 <b>协同</b> — 多智能体分工协作{featureOn('multi_agent') ? '' : ' 🔒专业版'}<br />
          • 🔁 <b>循环</b> — 自主"行动-观察-修正"闭环{featureOn('loop_engine') ? '' : ' 🔒专业版'}<br /><br />
          <span className="hint-text" style={{ fontSize: '.92em' }}>
            📚 侧边栏「知识库」可上传 PDF/Word/MD/TXT，对话时自动检索引用。<br />
            ⚙ 首次使用请先点击左下角 <b>「⚙ 设置」→「🔑 API Keys」</b> 配置模型。<br />
            支持 OpenAI / Claude / DeepSeek / Kimi / 百炼 / 智谱 / 豆包 / Gemini / Grok / Ollama 及自定义中转代理。
          </span>
          <div style={{ marginTop: 12, borderTop: '1px dashed var(--border)', paddingTop: 10 }}>
            <span className="hint-text">🚀 快速开始（点击模板一键填入）：</span>
            <div className="tpl-chips">
              {TEMPLATES.slice(0, 5).map((t, i) => (
                <button key={i} className="tpl-chip" onClick={() => onTemplate(i)}>{t.icon} {t.title}</button>
              ))}
              <button className="tpl-chip" onClick={onAllTemplates}>📚 全部模板…</button>
            </div>
          </div>
        </div>
        <div className="time">现在</div>
      </div>
    </div>
  );
}
export { MODE_LABELS };
