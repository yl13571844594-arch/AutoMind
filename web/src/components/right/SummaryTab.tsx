// 🧭 对话摘要（v1.11.5）：把"这段对话做到哪了"收拢到一屏 ——
// 当前目标 / 工作进度 / 任务规划 / 对话产物 / 用到的技能与工具 / AI 要点摘要。
//
// 除「AI 要点摘要」外全部从本地已有的状态推出来（消息、执行轨迹、计划、文件改动），
// 不额外调用模型：打开这个标签不花一个 token。要点摘要是显式点按钮才生成，
// 结果按会话缓存，对话有新进展时提示"可刷新"，而不是每次切过来都重算。
import { Button, Progress, Tooltip } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost, errText } from '../../api/client';
import { renderMarkdown } from '../../lib/markdown';
import { useAsync } from '../../lib/useAsync';
import { chatSid, MODE_LABELS, useApp } from '../../store/app';
import { useChat, type ChatItem, type PlanRow } from '../../store/chat';
import { useConv } from '../../store/conv';
import { usePanel } from '../../store/panel';
import { useUi } from '../../store/ui';
import { SUBAGENT_LABEL, TODO_ICON, subagentTone } from './panelLabels';

function Section({ title, extra, children }: {
  title: string; extra?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="rp-section">
      <h4>{title}<span style={{ marginLeft: 'auto' }}>{extra}</span></h4>
      {children}
    </div>
  );
}

/** 轨迹 body 是已转义的 HTML：取出纯文本再解析参数 */
function htmlText(html: string): string {
  const el = document.createElement('textarea');
  el.innerHTML = html.replace(/<[^>]+>/g, ' ');
  return el.value;
}

const FILE_RE = /`([^`\s]{1,160}\.(?:py|md|html?|js|jsx|ts|tsx|css|json|ya?ml|toml|csv|tsv|xlsx?|docx?|pptx?|pdf|png|jpe?g|svg|txt|log|sh|ps1|bat|sql|go|rs|java|c|cpp|h))`/gi;
const fmtDur = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`;
};
const STATE_ICON: Record<string, string> = { pending: '○', run: '◐', ok: '✓', fail: '✗' };

interface Derived {
  asks: string[];
  answers: number;
  plan: PlanRow[];
  planSource: string;
  multi: { role: string; subtask: string; state: string }[];
  loopIters: number;
  loopMax: number;
  tools: { name: string; ok: number; fail: number }[];
  skills: string[];
  experts: string[];
  mcp: string[];
  mentioned: string[];
  workspaces: { added: number; modified: number; deleted: number; applied: boolean; path: string; files: string[] }[];
  workflows: { name: string; runId: string }[];
  shots: number;
  images: number;
  errors: number;
  transcript: { role: string; content: string }[];
}

function derive(items: ChatItem[]): Derived {
  const d: Derived = {
    asks: [], answers: 0, plan: [], planSource: '', multi: [], loopIters: 0, loopMax: 0,
    tools: [], skills: [], experts: [], mcp: [], mentioned: [], workspaces: [], workflows: [],
    shots: 0, images: 0, errors: 0, transcript: [],
  };
  const tools = new Map<string, { ok: number; fail: number }>();
  const skills = new Set<string>();
  const experts = new Set<string>();
  const mcp = new Set<string>();
  const mentioned = new Set<string>();
  const bump = (name: string, ok: boolean) => {
    const t = tools.get(name) || { ok: 0, fail: 0 };
    if (ok) t.ok++; else t.fail++;
    tools.set(name, t);
  };
  for (const it of items) {
    if (it.kind === 'msg') {
      if (it.role === 'user') {
        if (it.md.trim()) d.asks.push(it.md.trim());
        d.images += (it.images || []).length;
        for (const c of it.chips || []) {
          if (c.state === 'fail') continue;
          if (c.key.startsWith('skill:')) skills.add(c.key.slice(6));
          else if (c.key === 'expert' && !c.label.includes('不使用')) experts.add(c.label.replace(/^🎓\s*/, ''));
          else if (c.key.startsWith('mcp:')) mcp.add(c.key.slice(4));
        }
      } else {
        d.answers++;
        for (const m of it.md.matchAll(FILE_RE)) mentioned.add(m[1]);
      }
      if (it.md.trim()) d.transcript.push({ role: it.role === 'user' ? 'user' : 'assistant', content: it.md.slice(0, 1500) });
    } else if (it.kind === 'exec' || it.kind === 'loop') {
      if (it.kind === 'exec' && it.plan.length) { d.plan = it.plan; d.planSource = '分层计划'; }
      if (it.kind === 'loop') { d.loopIters = it.iters.length; d.loopMax = it.iters[it.iters.length - 1]?.max || 0; }
      for (const t of it.traces) {
        if (t.kind === 'shot') { d.shots++; continue; }
        const call = /调用\s+([^\s<]+)/.exec(t.label);
        if (call && (t.kind === 'action' || t.kind === 'warn')) {
          const name = htmlText(call[1]).trim();
          bump(name, t.kind === 'action');
          if (name === 'skill_invoke') {
            const m = /"name"\s*:\s*"([^"]+)"/.exec(htmlText(t.body));
            if (m) skills.add(m[1]);
          } else if (name.startsWith('mcp_') || name.includes('__')) {
            mcp.add(name.split('__')[0].replace(/^mcp_/, ''));
          }
        }
        // 「⛔ 工具失败」只是对同一次调用的补充说明，计数已在 warn 里算过
      }
    } else if (it.kind === 'multi') {
      d.multi = it.steps;
    } else if (it.kind === 'workspace') {
      d.workspaces.push({
        added: it.added.length, modified: it.modified.length, deleted: it.deleted.length,
        applied: !!(it.applied && it.applied.length), path: it.path,
        files: [...it.added, ...it.modified].slice(0, 6),
      });
    } else if (it.kind === 'workflow') {
      d.workflows.push({ name: it.name, runId: it.runId });
    } else if (it.kind === 'error') {
      d.errors++;
    }
  }
  d.tools = [...tools.entries()].map(([name, v]) => ({ name, ...v }))
    .sort((a, b) => (b.ok + b.fail) - (a.ok + a.fail));
  d.skills = [...skills]; d.experts = [...experts]; d.mcp = [...mcp];
  d.mentioned = [...mentioned].slice(0, 12);
  d.transcript = d.transcript.slice(-60);
  return d;
}

interface CachedSummary { summary: string; by: string; turns: number; at: number }
const sumKey = (mode: string) => `automind_sum_${chatSid()}_${mode}`;
function readCached(mode: string): CachedSummary | null {
  try { return JSON.parse(localStorage.getItem(sumKey(mode)) || 'null'); } catch { return null; }
}

export default function SummaryTab() {
  const mode = useApp((s) => s.mode);
  const running = useApp((s) => s.running);
  const convSuffix = useApp((s) => s.convSuffix);
  const convs = useConv((s) => s.items);
  const messages = useChat((s) => s.messages);
  const progress = usePanel((s) => s.progress);
  const stats = usePanel((s) => s.stats);
  const usage = usePanel((s) => s.usage);
  const planTree = usePanel((s) => s.plan);
  const tick = usePanel((s) => s.refreshTick);
  const openInCode = usePanel((s) => s.openInCode);
  // v1.11.11：待办 / 子智能体（与本标签同源的 store；完整清单在「📊 面板」标签）
  const todos = usePanel((s) => s.todos);
  const todoCounts = usePanel((s) => s.todoCounts);
  const subagents = usePanel((s) => s.subagents);
  const featureOn = useApp((s) => s.featureOn);

  const items = messages[mode] || [];
  const d = useMemo(() => derive(items), [items]);
  const conv = convs.find((c) => (c.suffix || '') === (convSuffix || ''));

  const changesSt = useAsync<any[]>(() => apiGet('/changes').then((r) => r.changes || []), [tick]);
  const changes = useMemo(() => {
    const seen = new Set<string>();
    return (changesSt.data || []).filter((c) => !seen.has(c.path) && seen.add(c.path)).slice(0, 10);
  }, [changesSt.data]);

  // 进度条上的"已耗时"每秒走一下（只在有任务时）
  const [, setNow] = useState(0);
  useEffect(() => {
    if (!progress) return;
    const t = setInterval(() => setNow((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [progress]);

  const [cached, setCached] = useState<CachedSummary | null>(() => readCached(mode));
  const [summing, setSumming] = useState(false);
  const [sumErr, setSumErr] = useState('');
  useEffect(() => { setCached(readCached(mode)); setSumErr(''); }, [mode, convSuffix]);

  const summarize = async () => {
    if (!d.transcript.length) return;
    setSumming(true); setSumErr('');
    try {
      const r = await apiPost('/conversation-summary', { transcript: d.transcript }, 60000);
      const v: CachedSummary = { summary: String(r.summary || ''), by: String(r.by || ''), turns: d.asks.length, at: Date.now() };
      setCached(v);
      try { localStorage.setItem(sumKey(mode), JSON.stringify(v)); } catch { /* 配额：只是不缓存 */ }
    } catch (e) { setSumErr(errText(e)); } finally { setSumming(false); }
  };

  const openArtifact = (path: string) => {
    if (/\.html?$/i.test(path)) {
      useUi.getState().openPreview({ url: `/api/preview/file?path=${encodeURIComponent(path)}`, label: path });
    } else openInCode(path);
  };

  // 进度：进行中的任务优先；否则看最近一次计划的完成情况
  const planDone = d.plan.filter((r) => r.state === 'ok').length;
  const planFail = d.plan.filter((r) => r.state === 'fail').length;
  const pct = progress && progress.total > 0
    ? Math.min(100, Math.round(((Math.max(progress.cur, 1) - 1) / progress.total) * 100))
    : d.plan.length ? Math.round((planDone / d.plan.length) * 100) : 0;

  const lastAsk = d.asks[d.asks.length - 1] || '';
  const empty = !items.some((i) => i.kind === 'msg');
  const stale = cached && cached.turns < d.asks.length;
  // v1.11.10：头部只放"已经拿到"的两个过程指标 —— 没收到 usage_update 就不占位，
  // 免得任务还没开始就挂一行「缓存 —%」。cache_rate 为 null 同样跳过（模型没给缓存统计）。
  const usageBits: string[] = !usage.active ? [] : [
    usage.cache_rate != null ? `⚡ 缓存 ${Math.round(usage.cache_rate * 10) / 10}%` : '',
    usage.context_pct > 0 ? `🧠 上下文 ${Math.round(usage.context_pct)}%` : '',
  ].filter(Boolean);

  return (
    <div style={{ flex: 1, overflowY: 'auto' }}>
      <div className="sum-head">
        <div className="sum-title" title={conv?.title || ''}>{conv?.title || '当前对话'}</div>
        <div className="sum-meta">
          <span className={'sum-dot' + (running ? ' on' : '')} />{running ? '执行中' : '空闲'}
          <span>· {MODE_LABELS[mode] || mode}模式</span>
          <span>· {d.asks.length} 轮提问</span>
          {stats.tokens > 0 && <span>· 本次 {stats.tokens.toLocaleString()} tk</span>}
          {usageBits.length > 0 && <span>· {usageBits.join(' · ')}</span>}
        </div>
      </div>

      {empty ? (
        <div className="rp-section"><em className="hint-text">开始对话后，这里会汇总目标、进度、规划、产物与用到的技能。</em></div>
      ) : (<>
        <Section title="🎯 当前目标">
          <div className="sum-goal" title={lastAsk}>{lastAsk.slice(0, 220)}{lastAsk.length > 220 ? '…' : ''}</div>
          {d.asks.length > 1 && (
            <details className="sum-asks">
              <summary className="hint-text">此前的提问（{d.asks.length - 1}）</summary>
              {d.asks.slice(0, -1).slice(-8).reverse().map((a, i) => (
                <div key={i} className="sum-ask" title={a}>· {a.slice(0, 80)}</div>
              ))}
            </details>
          )}
        </Section>

        <Section title="📈 工作进度">
          {progress ? (
            <>
              <div className="sum-line"><b>{progress.phase}</b>
                {progress.total > 0 && <span className="hint-text">第 {Math.max(progress.cur, 1)} / {progress.total} 步</span>}
                <span className="hint-text" style={{ marginLeft: 'auto' }}>已用 {fmtDur(Date.now() - progress.startedAt)}</span>
              </div>
              {progress.total > 0 && <Progress percent={pct} size="small" status="active" showInfo={false} />}
              {progress.label && <div className="hint-text sum-ell" title={progress.label}>{progress.label}</div>}
            </>
          ) : d.plan.length ? (
            <>
              <div className="sum-line">
                <span>最近一次计划：完成 <b>{planDone}</b> / {d.plan.length}</span>
                {planFail > 0 && <span style={{ color: 'var(--red)' }}>失败 {planFail}</span>}
              </div>
              <Progress percent={pct} size="small" showInfo={false}
                status={planFail ? 'exception' : pct === 100 ? 'success' : 'normal'} />
            </>
          ) : (
            <div className="sum-line">
              <span>{d.answers} 次回复</span>
              {d.loopIters > 0 && <span>· Loop {d.loopIters}{d.loopMax ? `/${d.loopMax}` : ''} 轮</span>}
              {d.errors > 0 && <span style={{ color: 'var(--red)' }}>· {d.errors} 次失败/中断</span>}
              <span className="hint-text" style={{ marginLeft: 'auto' }}>暂无进行中的任务</span>
            </div>
          )}
        </Section>

        <Section title="📋 待办" extra={
          todos.length > 0 && todoCounts ? (
            <span className="hint-text">
              待办 {todoCounts.pending} · 进行中 {todoCounts.in_progress} · 完成 {todoCounts.completed}
            </span>
          ) : null
        }>
          {todos.length > 0 ? (
            <div className="todo-list">
              {todos.slice(0, 8).map((t) => (
                <div key={t.id} className={`todo-row ${t.status}`}>
                  <span className="todo-ico">{TODO_ICON[t.status] || '○'}</span>
                  <span className="todo-text">{t.content}</span>
                </div>
              ))}
              {todos.length > 8 && (
                <div className="hint-text">还有 {todos.length - 8} 条，完整清单见「📊 面板」标签</div>
              )}
            </div>
          ) : (
            <em className="hint-text">暂无待办。多步任务中模型会维护一份待办清单。</em>
          )}
        </Section>

        {/* 子智能体是专业版 multi_agent 功能：未授权时整块不显示，别给一个永远空的面板 */}
        {featureOn('multi_agent') && (
          <Section title="🤖 子智能体" extra={
            subagents.length > 0 ? <span className="hint-text">{subagents.length} 个</span> : null
          }>
            {subagents.length > 0 ? subagents.slice(0, 5).map((a) => {
              const tone = subagentTone(a.status);
              const tail = a.output_tail || a.output || '';
              return (
                <div key={a.agent_id} className={`sa-card ${tone}`}>
                  <div className="sa-head">
                    <b className="sa-title" title={a.title || a.agent_id}>{a.title || a.agent_id}</b>
                    <span className="hint-text sa-meta">
                      {SUBAGENT_LABEL[a.status] || a.status}
                      {a.steps ? ` · ${a.steps} 步` : ''}
                    </span>
                  </div>
                  {a.error
                    ? <div className="sa-err" title={a.error}>{a.error}</div>
                    : tail ? <div className="sa-tail" title={tail}>{tail}</div> : null}
                </div>
              );
            }) : (
              <em className="hint-text">协同模式或模型主动委派时，子智能体会列在这里。</em>
            )}
          </Section>
        )}

        <Section title="📋 任务规划" extra={d.planSource && <span className="hint-text">{d.planSource}</span>}>
          {d.plan.length ? (
            <div className="sum-plan">
              {d.plan.map((r, i) => (
                <div key={r.goalId || i} className={'sum-step ' + r.state} title={r.error || r.text}>
                  <span className="sum-step-ico">{STATE_ICON[r.state]}</span>{r.text}
                </div>
              ))}
            </div>
          ) : d.multi.length ? (
            <div className="sum-plan">
              {d.multi.map((s, i) => (
                <div key={i} className={'sum-step ' + (s.state === 'ok' ? 'ok' : s.state === 'run' ? 'run' : 'pending')}>
                  <span className="sum-step-ico">{STATE_ICON[s.state === 'ok' ? 'ok' : s.state === 'run' ? 'run' : 'pending']}</span>
                  <b>{s.role}</b>：{s.subtask}
                </div>
              ))}
            </div>
          ) : planTree?.root_goal ? (
            <div className="sum-step">{planTree.root_goal.description}</div>
          ) : (
            <em className="hint-text">对话 / 编程模式没有预先生成的计划；在「工作」模式或开启 📋 计划模式后会列出分步规划。</em>
          )}
        </Section>

        <Section title="📦 对话产物" extra={<a style={{ cursor: 'pointer' }} onClick={changesSt.reload}>⟳</a>}>
          {changes.length > 0 && <div className="sum-sub">文件改动</div>}
          {changes.map((c) => (
            <div key={c.path} className="sum-file" title={c.path} onClick={() => openArtifact(c.path)}>
              {c.created ? '✨' : '✏️'} <span className="sum-ell">{c.path.split(/[\\/]/).pop()}</span>
              <span className="hint-text">{c.time || ''}</span>
            </div>
          ))}
          {d.workspaces.map((w, i) => (
            <div key={i} className="sum-card">
              🗂 隔离副本：新增 {w.added} · 修改 {w.modified}{w.deleted ? ` · 删除 ${w.deleted}` : ''}
              {w.applied && <span style={{ color: 'var(--green)' }}> · 已应用</span>}
              {w.files.length > 0 && <div className="hint-text sum-ell">{w.files.join('、')}</div>}
            </div>
          ))}
          {d.workflows.length > 0 && <div className="sum-sub">工作流运行</div>}
          {d.workflows.slice(-5).map((w) => (
            <div key={w.runId} className="sum-card">🔀 {w.name || '工作流'} <span className="hint-text mono">{w.runId.slice(0, 8)}</span></div>
          ))}
          {d.mentioned.length > 0 && <div className="sum-sub">回复中提到的文件</div>}
          {d.mentioned.map((p) => (
            <div key={p} className="sum-file" title={`打开 ${p}`} onClick={() => openArtifact(p)}>📄 <span className="sum-ell">{p}</span></div>
          ))}
          {(d.shots > 0 || d.images > 0) && (
            <div className="hint-text" style={{ marginTop: 4 }}>
              {d.shots > 0 && `🖼 网页截图 ${d.shots} 张　`}{d.images > 0 && `📎 你发送的图片 ${d.images} 张`}
            </div>
          )}
          {!changes.length && !d.workspaces.length && !d.workflows.length && !d.mentioned.length && !d.shots && (
            <em className="hint-text">{changesSt.loading && !changesSt.loaded ? '正在读取…' : '还没有产物：Agent 写的文件、隔离副本、工作流运行会列在这里'}</em>
          )}
        </Section>

        <Section title="🧩 技能与工具">
          {(d.skills.length > 0 || d.experts.length > 0 || d.mcp.length > 0) && (
            <div className="sum-tags">
              {d.experts.map((x) => <span key={'e' + x} className="sum-tag exp">🎓 {x}</span>)}
              {d.skills.map((x) => <span key={'s' + x} className="sum-tag skill">🧩 {x}</span>)}
              {d.mcp.map((x) => <span key={'m' + x} className="sum-tag mcp">🔌 {x}</span>)}
            </div>
          )}
          {d.tools.length ? (
            <div className="sum-tools">
              {d.tools.slice(0, 12).map((t) => (
                <Tooltip key={t.name} title={`成功 ${t.ok} 次${t.fail ? `，失败 ${t.fail} 次` : ''}`}>
                  <span className={'sum-tool' + (t.fail && !t.ok ? ' bad' : '')}>
                    🔧 {t.name}<b>×{t.ok + t.fail}</b>{t.fail > 0 && <i>✗{t.fail}</i>}
                  </span>
                </Tooltip>
              ))}
              {d.tools.length > 12 && <span className="hint-text">等 {d.tools.length} 个工具</span>}
            </div>
          ) : !d.skills.length && !d.experts.length && !d.mcp.length && (
            <em className="hint-text">本对话还没有调用工具或技能</em>
          )}
        </Section>

        <Section title="📝 要点摘要" extra={
          <Button size="small" type={cached ? 'default' : 'primary'} loading={summing}
            disabled={!d.transcript.length} onClick={summarize}>
            {cached ? (stale ? '有新进展，刷新' : '重新生成') : '生成摘要'}
          </Button>}>
          {sumErr && <div className="hint-text" style={{ color: 'var(--red)' }}>⚠️ 生成失败：{sumErr}</div>}
          {cached?.summary ? (
            <>
              <div className="sum-md md" dangerouslySetInnerHTML={{ __html: renderMarkdown(cached.summary) }} />
              <div className="hint-text" style={{ marginTop: 4 }}>
                {cached.by === 'llm' ? '由模型生成' : '抽取式摘要（模型不可用时的兜底）'} · 基于 {cached.turns} 轮提问 · {new Date(cached.at).toLocaleTimeString()}
              </div>
            </>
          ) : !sumErr && (
            <em className="hint-text">按「目标 / 已完成 / 关键结论 / 下一步」整理当前对话（会调用一次模型；未配置模型时用抽取式摘要）。</em>
          )}
        </Section>
      </>)}
    </div>
  );
}
