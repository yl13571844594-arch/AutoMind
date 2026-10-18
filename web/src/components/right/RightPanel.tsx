// 右栏：🧭 摘要（v1.11.5：目标 / 进度 / 规划 / 产物 / 技能 / AI 要点摘要）
//      📊 观测面板（实时状态 / Token 用量 / 文件改动 / 计划 / HTML 预览 / 审计）
//      🌐 浏览器（v1.11.1：Agent 操作网页的实时画面）
//      📄 代码标签（文件树 + Monaco 编辑器 + Diff）。
// 可收起成一条细栏，给对话区让出宽度；收起时浏览器有新画面会在图标上亮点提示。
import { App } from 'antd';
import { Tooltip } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet } from '../../api/client';
import { writeAction } from '../../lib/writeAction';
import { useAsync } from '../../lib/useAsync';
import { estCost, fmtCost, setCustomPrice, clearCustomPrice, modelPrice } from '../../lib/pricing';
import { esc } from '../../lib/markdown';
import { useApp, chatSid } from '../../store/app';
import { usePanel, type RightTab } from '../../store/panel';
import { useUi } from '../../store/ui';
import BrowserTab from './BrowserTab';
import CodeTab from './CodeTab';
import { SUBAGENT_LABEL, TODO_ICON, subagentTone } from './panelLabels';
import SummaryTab from './SummaryTab';

/** 右栏专用的一行式失败提示：窄栏放不下完整错误面板，但**必须说出来**。 */
function LoadFail({ st }: { st: { error: Error | null; reload: () => void } }) {
  return (
    <div className="hint-text" style={{ color: 'var(--red)' }}>
      ⚠️ 加载失败：{(st.error as any)?.friendly || st.error?.message || '未知错误'}
      <a style={{ marginLeft: 6, cursor: 'pointer' }} onClick={st.reload}>重试</a>
    </div>
  );
}

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

function planHtml(g: any, indent: string): string {
  const icons: any = { pending: '○', in_progress: '◐', completed: '✓', failed: '✗', blocked: '⊘', reverted: '↺' };
  const cls: any = { pending: 'pending', in_progress: 'running', completed: 'done', failed: 'fail', blocked: 'pending', reverted: 'fail' };
  let html = `<div class="node ${cls[g.status] || ''}">${indent}${icons[g.status] || '?'} ${esc(g.description)}`;
  if (g.action) html += ` [${esc(g.action)}]`;
  html += '</div>';
  (g.children || []).forEach((c: any) => { html += planHtml(c, indent + '  '); });
  return html;
}

const TABS: { key: RightTab; icon: string; label: string; width: number }[] = [
  { key: 'summary', icon: '🧭', label: '摘要', width: 320 },
  { key: 'panel', icon: '📊', label: '面板', width: 300 },
  { key: 'browser', icon: '🌐', label: '浏览器', width: 440 },
  { key: 'code', icon: '📄', label: '代码', width: 560 },
];

// v1.11.11 待办/子智能体的状态图标与中文映射见 panelLabels.ts（摘要标签共用一份）

export default function RightPanel() {
  const tab = usePanel((s) => s.rightTab);
  const collapsed = usePanel((s) => s.rightCollapsed);
  const frameId = usePanel((s) => s.frames[0]?.id || 0);
  const setTab = usePanel((s) => s.setRightTab);
  const toggle = usePanel((s) => s.toggleRight);
  // 收起 / 不在浏览器标签时来了新画面 → 图标亮点，提醒有东西可看
  const seen = useRef(frameId);
  const [unseen, setUnseen] = useState(false);
  useEffect(() => {
    if (frameId === seen.current) return;
    seen.current = frameId;
    setUnseen(collapsed || tab !== 'browser');
  }, [frameId, collapsed, tab]);
  useEffect(() => { if (!collapsed && tab === 'browser') setUnseen(false); }, [collapsed, tab]);

  if (collapsed) {
    return (
      <div className="rp-rail">
        <Tooltip title="展开右栏" placement="left">
          <button className="rp-rail-btn" onClick={toggle} aria-label="展开右栏">«</button>
        </Tooltip>
        {TABS.map((t) => (
          <Tooltip key={t.key} title={t.label} placement="left">
            <button className="rp-rail-btn" aria-label={t.label}
              onClick={() => { setTab(t.key); toggle(); }}>
              {t.icon}{t.key === 'browser' && unseen && <span className="rp-new-dot" />}
            </button>
          </Tooltip>
        ))}
      </div>
    );
  }

  const width = TABS.find((t) => t.key === tab)?.width || 300;
  return (
    <div className="right-panel" style={{ width }}>
      <div className="rp-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key}
            className={'rp-tab' + (tab === t.key ? ' active' : '')} onClick={() => setTab(t.key)}>
            {t.icon} {t.label}
            {t.key === 'browser' && unseen && <span className="rp-new-dot" />}
          </button>
        ))}
        <Tooltip title="收起右栏" placement="left">
          <button className="rp-collapse" onClick={toggle} aria-label="收起右栏">»</button>
        </Tooltip>
      </div>
      {tab === 'code' ? <CodeTab /> : tab === 'browser' ? <BrowserTab />
        : tab === 'summary' ? <SummaryTab /> : <ObservePanel />}
    </div>
  );
}

/** 📊 观测面板（原右栏主体）。 */
function ObservePanel() {
  const { message, modal } = App.useApp();
  const stats = usePanel((s) => s.stats);
  const liveUsage = usePanel((s) => s.usage);
  const plan = usePanel((s) => s.plan);
  const tick = usePanel((s) => s.refreshTick);
  const model = useApp((s) => s.status?.model || '');
  // v1.11.11：待办 / 子智能体（数据由 ws.ts 的事件与 /api/todo 初始化写入 store，
  // 所以面板晚打开也能看到最新状态，不必等一次点击触发拉取）
  const todos = usePanel((s) => s.todos);
  const todoCounts = usePanel((s) => s.todoCounts);
  const subagents = usePanel((s) => s.subagents);
  const featureOn = useApp((s) => s.featureOn);
  // 四块各自三态。此前全是 `.catch(() => {})`：服务一挂，右栏就变成
  // "0 个 Token、没有文件改动、没有审计记录"——一份**看起来正常的假快照**。
  const tokensSt = useAsync<any>(() => apiGet('/tokens'), [tick]);
  const changesSt = useAsync<any[]>(
    () => apiGet('/changes').then((r) => r.changes || []), [tick]);
  const htmlSt = useAsync<any[]>(
    () => apiGet('/files/html').then((r) => (Array.isArray(r) ? r : [])), [tick]);
  const auditSt = useAsync<any>(() => apiGet('/audit?limit=5'), [tick]);
  const tokens = tokensSt.data || {};

  // v1.11.10：缓存命中 / 上下文占用。收到过 usage_update 就用本轮任务的累计；
  // 否则回退 /api/tokens 的会话累计值 —— 任务刚结束、或打开页面还没跑新任务时，
  // 这一块不该是空白（右侧标签也随之从「本次任务」变回「本次运行」）。
  const usage = liveUsage.active ? {
    rate: liveUsage.cache_rate,
    cached: liveUsage.cached_tokens,
    input: liveUsage.input_tokens,
    hitCalls: liveUsage.cache_hit_calls,
    calls: liveUsage.calls,
    used: liveUsage.peak_prompt_tokens,
    max: liveUsage.context_max,
    pct: liveUsage.context_pct,
  } : {
    rate: typeof tokens.cache_rate === 'number' ? tokens.cache_rate : null,
    cached: tokens.cached || 0,
    input: tokens.input || 0,
    hitCalls: tokens.cache_hit_calls || 0,
    calls: tokens.calls || 0,
    used: tokens.context?.used || 0,
    max: tokens.context?.max || 0,
    pct: tokens.context?.pct || 0,
  };
  // cache_rate === null 表示"模型没给缓存统计"，显示「—」；0% 才是"确实没命中"
  const rateTxt = usage.rate == null ? '—' : `${Math.round(usage.rate * 10) / 10}%`;
  const rateCls = usage.rate == null ? '' : usage.rate >= 50 ? ' ok' : usage.rate > 0 ? ' warn' : '';
  // 上下文：≥70% 提醒（再长要压缩了），≥90% 标红（这一轮基本写不下新东西了）
  const ctxCls = usage.pct >= 90 ? ' bad' : usage.pct >= 70 ? ' warn' : '';
  const hasCtx = usage.calls > 0 || usage.used > 0;
  const ctxTxt = hasCtx ? `${Math.round(usage.pct)}%` : '—';
  const ctxWidth = hasCtx ? Math.max(0, Math.min(100, usage.pct)) : 0;
  const changes = changesSt.data || [];
  const htmlFiles = htmlSt.data || [];
  const audit = auditSt.data;

  const refresh = () => {
    tokensSt.reload(); changesSt.reload(); htmlSt.reload(); auditSt.reload();
  };

  const cost = fmtCost(estCost(model, tokens.prompt, tokens.completion));

  const configPricing = () => {
    const [p, c, custom] = modelPrice(model);
    const cur = custom ? '（当前为自定义单价）' : (p != null ? `（当前 ${model} 默认：¥${p}/¥${c}）` : '（当前模型无内置单价）');
    const inp = prompt(`设置 Token 单价用于成本估算 ${cur}\n格式：输入单价/百万tk,输出单价/百万tk（如 2,8）\n留空并确定 = 恢复内置单价表`, custom ? `${p},${c}` : '');
    if (inp === null) return;
    if (!inp.trim()) { clearCustomPrice(); message.info('已恢复内置单价表'); refresh(); return; }
    const m = inp.split(/[,，]/).map((s) => parseFloat(s.trim()));
    if (m.length !== 2 || m.some((v) => isNaN(v) || v < 0)) { message.error('格式错误，示例：2,8'); return; }
    setCustomPrice(m[0], m[1]);
    message.success(`单价已设置：输入¥${m[0]} / 输出¥${m[1]} 每百万token`);
    refresh();
  };

  const rollback = async (path?: string) => {
    modal.confirm({
      title: path ? '撤销该文件的全部改动？' : '回滚全部文件改动？',
      content: path || '新建文件删除、修改文件恢复原内容，此操作不可撤销',
      onOk: writeAction(path ? '撤销文件改动' : '回滚全部改动', async () => {
        const r = await fetch('/api/changes/rollback', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(path ? { path } : { all: true }),
        }).then((x) => x.json());
        if (r.error) { message.error(r.error); return; }
        message.success(path ? '已恢复' : `已回滚 ${r.restored} 个文件`);
        refresh();
      }),
    });
  };

  const dedupChanges = (() => {
    const seen = new Set<string>();
    return changes.filter((c) => !seen.has(c.path) && seen.add(c.path)).slice(0, 8);
  })();

  // 清空待办：先删服务端（否则刷新/重连后又拉回来，界面看起来"清了个寂寞"）
  const clearTodos = async () => {
    try {
      await apiDelete(`/todo?session_id=${encodeURIComponent(chatSid())}`);
      usePanel.getState().clearTodos();
      message.info('待办已清空');
    } catch (e: any) {
      message.error(`清空待办失败：${e?.friendly || e?.message || e}`);
    }
  };

  return (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <Section title="📊 实时状态">
            <div className="stat-grid">
              <div className="stat-item"><div className="label">步骤</div><div className="value">{stats.steps}</div></div>
              <div className="stat-item"><div className="label">回溯</div><div className="value">{stats.backtracks}</div></div>
              <div className="stat-item"><div className="label">本次 Tokens</div><div className="value">{stats.tokens}</div></div>
              <div className="stat-item"><div className="label">耗时</div><div className="value">{stats.duration_ms}ms</div></div>
            </div>
          </Section>

          <Section title="⚡ 缓存与上下文" extra={
            <span className="hint-text">{liveUsage.active ? '本次任务' : '本次运行'}</span>
          }>
            <div className="stat-grid">
              <div className="stat-item">
                <div className="label">缓存命中率</div>
                <div className={'value' + rateCls}>{rateTxt}</div>
              </div>
              <div className="stat-item">
                <div className="label">上下文使用率</div>
                <div className={'value' + ctxCls}>{ctxTxt}</div>
                <div className={'usage-bar' + ctxCls}>
                  <div className="usage-bar-fill" style={{ width: `${ctxWidth}%` }} />
                </div>
              </div>
            </div>
            <div className="hint-text" style={{ marginTop: 6 }}>
              命中 {usage.cached.toLocaleString()} / 输入 {usage.input.toLocaleString()} tk ·
              命中 {usage.hitCalls} / {usage.calls} 次调用
            </div>
            <div className="hint-text">
              上下文 {usage.used.toLocaleString()} / {usage.max.toLocaleString()} tk（取最近一次请求）
            </div>
          </Section>

          <Section title="📋 待办" extra={
            todos.length > 0
              ? <a style={{ fontSize: '.9em', cursor: 'pointer' }} onClick={clearTodos}>清空</a>
              : null
          }>
            {todos.length > 0 ? (
              <>
                {todoCounts && (
                  <div className="hint-text" style={{ marginBottom: 6 }}>
                    待办 {todoCounts.pending} · 进行中 {todoCounts.in_progress} · 完成 {todoCounts.completed}
                  </div>
                )}
                <div className="todo-list">
                  {todos.map((t) => (
                    <div key={t.id} className={`todo-row ${t.status}`}>
                      <span className="todo-ico">{TODO_ICON[t.status] || '○'}</span>
                      <span className="todo-text">{t.content}</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <em className="hint-text">
                暂无待办。多步任务中模型会维护一份待办清单，进度会实时显示在这里。
              </em>
            )}
          </Section>

          {/* 子智能体是专业版 multi_agent 功能：社区版不显示这块，免得给出一个永远空的面板 */}
          {featureOn('multi_agent') && (
            <Section title="🤖 子智能体" extra={
              subagents.length > 0 ? <span className="hint-text">{subagents.length} 个</span> : null
            }>
              {subagents.length > 0 ? subagents.map((a) => {
                const tone = subagentTone(a.status);
                const tail = a.output_tail || a.output || '';
                return (
                  <div key={a.agent_id} className={`sa-card ${tone}`}>
                    <div className="sa-head">
                      <b className="sa-title" title={a.title || a.agent_id}>
                        {tone === 'running' ? '◐' : tone === 'done' ? '✓' : tone === 'failed' ? '✗' : '⊘'}
                        {' '}{a.title || a.agent_id}
                      </b>
                      <span className="hint-text sa-meta">
                        {SUBAGENT_LABEL[a.status] || a.status}
                        {a.steps ? ` · ${a.steps} 步` : ''}
                        {a.tokens ? ` · ${a.tokens.toLocaleString()} tk` : ''}
                      </span>
                    </div>
                    {a.error ? (
                      <div className="sa-err" title={a.error}>{a.error}</div>
                    ) : tail ? (
                      <div className="sa-tail" title={tail}>{tail}</div>
                    ) : null}
                  </div>
                );
              }) : (
                <em className="hint-text">
                  协同模式或模型主动委派时，子智能体会列在这里（标题 · 状态 · 步数 · 输出尾部）。
                </em>
              )}
            </Section>
          )}

          <Section title="🪙 Token 用量统计" extra={
            <a style={{ fontSize: '.9em', cursor: 'pointer' }} onClick={async () => {
              await apiDelete('/tokens'); refresh(); message.info('Token 统计已重置');
            }}>重置</a>
          }>
            {tokensSt.error && !tokensSt.loaded && <LoadFail st={tokensSt} />}
            <div className="stat-grid">
              <div className="stat-item"><div className="label">输入 (prompt)</div><div className="value" style={{ fontSize: '1em' }}>{(tokens.prompt || 0).toLocaleString()}</div></div>
              <div className="stat-item"><div className="label">输出 (completion)</div><div className="value" style={{ fontSize: '1em' }}>{(tokens.completion || 0).toLocaleString()}</div></div>
              <div className="stat-item"><div className="label">累计总量</div><div className="value ok" style={{ fontSize: '1em' }}>{(tokens.total || 0).toLocaleString()}</div></div>
              <div className="stat-item"><div className="label">任务数</div><div className="value" style={{ fontSize: '1em' }}>{tokens.tasks || 0}</div></div>
              <div className="stat-item" style={{ gridColumn: 'span 2' }}>
                <div className="label">💰 预估成本（点击设置单价）</div>
                <div className="value" style={{ fontSize: '1em', cursor: 'pointer', color: cost ? 'var(--yellow)' : 'var(--text3)' }}
                  onClick={configPricing}>{cost || '设置单价'}</div>
              </div>
            </div>
          </Section>

          <Section title="↩️ 文件改动" extra={<a style={{ cursor: 'pointer' }} onClick={refresh}>⟳</a>}>
            {changesSt.error && !changesSt.loaded ? <LoadFail st={changesSt} /> :
             changesSt.loading && !changesSt.loaded ? <em className="hint-text">正在读取…</em> :
             dedupChanges.length === 0 ? (
              <em className="hint-text">Agent 改过的文件会列在这里，可一键撤销</em>
            ) : (
              <>
                {dedupChanges.map((c) => (
                  <div key={c.path} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0', fontSize: '.8em' }}>
                    <span title={c.path} style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {c.created ? '✨' : '✏️'} {c.path.split(/[\\/]/).pop()}
                    </span>
                    <span className="hint-text">{c.time || ''}</span>
                    <a style={{ cursor: 'pointer' }} title="撤销该文件的全部改动" onClick={() => rollback(c.path)}>↩</a>
                  </div>
                ))}
                <div style={{ textAlign: 'right', marginTop: 6 }}>
                  <a style={{ color: 'var(--red)', fontSize: '.78em', cursor: 'pointer' }} onClick={() => rollback()}>↩️ 全部回滚</a>
                </div>
              </>
            )}
          </Section>

          <Section title="📋 执行计划">
            {plan?.root_goal ? (
              <div className="plan-tree" dangerouslySetInnerHTML={{ __html: planHtml(plan.root_goal, '') }} />
            ) : (
              <em className="hint-text">对话模式无计划。切换到「工作」模式后将显示分层计划。</em>
            )}
          </Section>

          <Section title="🌐 HTML 预览" extra={<a style={{ cursor: 'pointer' }} onClick={refresh}>⟳</a>}>
            {htmlSt.error && !htmlSt.loaded ? <LoadFail st={htmlSt} /> :
             htmlSt.loading && !htmlSt.loaded ? <em className="hint-text">正在读取…</em> :
             htmlFiles.length === 0 ? (
              <em className="hint-text">项目中暂无 HTML 文件</em>
            ) : htmlFiles.slice(0, 8).map((f: any) => (
              <div key={f.path} style={{ padding: '4px 0', fontSize: '.8em', cursor: 'pointer', color: 'var(--text2)' }}
                onClick={() => useUi.getState().openPreview({ url: `/api/preview/file?path=${encodeURIComponent(f.path)}`, label: f.path })}>
                🔍 {f.path}
              </div>
            ))}
          </Section>

          <Section title="🛡️ 审计概览">
            {auditSt.error && !auditSt.loaded ? <LoadFail st={auditSt} /> :
             auditSt.loading && !auditSt.loaded ? <em className="hint-text">正在读取…</em> :
             !audit || !audit.entries?.length ? (
              <em className="hint-text">暂无工具调用记录</em>
            ) : (
              <>
                <div style={{ fontSize: '.8em', marginBottom: 6 }}>
                  放行 <b style={{ color: 'var(--green)' }}>{audit.summary.allow}</b> ·
                  需确认 <b style={{ color: 'var(--yellow)' }}>{audit.summary.ask_user}</b> ·
                  高危 <b style={{ color: 'var(--red)' }}>{audit.summary.dangerous}</b>
                </div>
                {audit.entries.slice(0, 5).map((e: any, i: number) => (
                  <div key={i} style={{ fontSize: '.76em', margin: '3px 0', color: e.tier === 'dangerous' ? 'var(--red)' : 'var(--text2)' }}>
                    <span className="mono">{e.time}</span> {e.tool} <span className="hint-text">[{e.tier}]</span>
                  </div>
                ))}
              </>
            )}
          </Section>
        </div>
  );
}
