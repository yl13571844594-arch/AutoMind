// 各类消息气泡：普通消息 / 流式 / 打字 / 执行过程 / 协同 / 循环 / 续跑按钮 / 欢迎页。
import { App } from 'antd';
import { memo } from 'react';
import { isSafeUrl, renderMarkdown } from '../../lib/markdown';
import { MODE_LABELS, useApp } from '../../store/app';
import type { ChatItem, LoopIter, MaStep, PlanRow, TraceItem } from '../../store/chat';
import { TEMPLATES } from '../../lib/templates';
import { useUi } from '../../store/ui';

function Avatar({ role, icon }: { role: 'user' | 'agent'; icon?: string }) {
  return <div className="avatar">{icon || (role === 'user' ? '我' : 'AM')}</div>;
}

export const MsgBubble = memo(function MsgBubble({ item }: { item: Extract<ChatItem, { kind: 'msg' }> }) {
  const { message } = App.useApp();
  const copy = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    const codeBtn = target.closest('.copy-code');
    if (codeBtn) {
      const pre = codeBtn.closest('.code-block')?.querySelector('pre');
      if (pre) navigator.clipboard?.writeText(pre.textContent || '').then(() => message.success('已复制代码'));
      return;
    }
    const msgBtn = target.closest('.copy-msg');
    if (msgBtn) {
      const bubble = msgBtn.closest('.bubble');
      if (bubble) navigator.clipboard?.writeText((bubble as HTMLElement).innerText).then(() => message.success('已复制'));
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
  return (
    <div className={`msg ${item.role === 'user' ? 'user' : 'agent'}`}>
      <Avatar role={item.role} />
      <div className="col">
        <div className="bubble" onClick={copy}>
          {item.role === 'agent' && <button className="copy-msg" title="复制此条">⧉</button>}
          {item.images && item.images.length > 0 && (
            <div className="mm-thumbs">
              {item.images.filter(isSafeUrl).map((u, i) => <img key={i} src={u} alt="img" />)}
            </div>
          )}
          <span dangerouslySetInnerHTML={{ __html: renderMarkdown(item.md) }} />
        </div>
        <div className="time">{item.meta || ''}</div>
      </div>
    </div>
  );
});

export function StreamBubble({ item }: { item: Extract<ChatItem, { kind: 'stream' }> }) {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble">
          <span dangerouslySetInnerHTML={{ __html: renderMarkdown(item.buf) }} />
          <span className="cursor">▍</span>
        </div>
      </div>
    </div>
  );
}

export function TypingBubble() {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble"><div className="typing-dots"><span /><span /><span /></div></div>
      </div>
    </div>
  );
}

function Traces({ traces }: { traces: TraceItem[] }) {
  return (
    <div className="exec-trace">
      {traces.map((t, i) => (
        <div key={i} className={`trace-item trace-${t.kind || 'info'}`}>
          <div className="trace-label" dangerouslySetInnerHTML={{ __html: t.label }} />
          <div className="trace-body" dangerouslySetInnerHTML={{ __html: t.body }} />
        </div>
      ))}
    </div>
  );
}

const PLAN_ICON: Record<PlanRow['state'], string> = { pending: '○', run: '◐', ok: '✓', fail: '✗' };

export function ExecBubble({ item }: { item: Extract<ChatItem, { kind: 'exec' }> }) {
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
          <Traces traces={item.traces} />
          {!item.done && <div className="typing-dots" style={{ marginTop: 6 }}><span /><span /><span /></div>}
        </div>
      </div>
    </div>
  );
}

const MA_ROLES: Record<string, string> = {
  planner: '🧭 规划', researcher: '🔎 研究', coder: '💻 编程', writer: '✍️ 写作', reviewer: '🧐 审阅',
};

export function MultiBubble({ item }: { item: Extract<ChatItem, { kind: 'multi' }> }) {
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
}

const LOOP_STOP: Record<string, string> = {
  completed: '✅ 已完成', no_progress: '⏹ 连续无进展，已停止', converged: '🔄 输出已收敛，已停止',
  idle: '💤 连续多轮未执行操作，已停止', max_iterations: '⛔ 达到最大轮数',
};

export function LoopBubble({ item }: { item: Extract<ChatItem, { kind: 'loop' }> }) {
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
          <Traces traces={item.traces} />
          {item.done && item.stopReason && LOOP_STOP[item.stopReason] && (
            <div style={{ marginTop: 8, fontWeight: 600 }}>{LOOP_STOP[item.stopReason]}</div>
          )}
          {!item.done && <div className="typing-dots" style={{ marginTop: 6 }}><span /><span /><span /></div>}
        </div>
      </div>
    </div>
  );
}

export function ResumeBubble({ item, onResume }: {
  item: Extract<ChatItem, { kind: 'resume' }>; onResume: () => void;
}) {
  return (
    <div className="msg agent">
      <Avatar role="agent" />
      <div className="col">
        <div className="bubble">
          <button className="hb-preview" onClick={onResume}>▶ 继续此任务</button>
          <span className="hint-text" style={{ marginLeft: 8 }}>
            从{item.why}处继续，不重做已完成的部分（已产出的文件仍保留）
          </span>
        </div>
      </div>
    </div>
  );
}

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
