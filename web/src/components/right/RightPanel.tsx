// 右栏：📊 观测面板（实时状态 / Token 用量 / 文件改动 / 计划 / HTML 预览 / 审计）
//      📄 代码标签（文件树 + Monaco 编辑器 + Diff）。
import { App } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet } from '../../api/client';
import { estCost, fmtCost, setCustomPrice, clearCustomPrice, modelPrice } from '../../lib/pricing';
import { esc } from '../../lib/markdown';
import { useApp } from '../../store/app';
import { usePanel } from '../../store/panel';
import { useUi } from '../../store/ui';
import CodeTab from './CodeTab';

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

export default function RightPanel() {
  const { message, modal } = App.useApp();
  const [tab, setTab] = useState<'panel' | 'code'>('panel');
  const stats = usePanel((s) => s.stats);
  const plan = usePanel((s) => s.plan);
  const tick = usePanel((s) => s.refreshTick);
  const model = useApp((s) => s.status?.model || '');
  const [tokens, setTokens] = useState<any>({});
  const [changes, setChanges] = useState<any[]>([]);
  const [htmlFiles, setHtmlFiles] = useState<any[]>([]);
  const [audit, setAudit] = useState<any>(null);

  const refresh = () => {
    apiGet('/tokens').then(setTokens).catch(() => {});
    apiGet('/changes').then((r) => setChanges(r.changes || [])).catch(() => {});
    apiGet('/files/html').then((r) => setHtmlFiles(Array.isArray(r) ? r : [])).catch(() => {});
    apiGet('/audit?limit=5').then(setAudit).catch(() => {});
  };
  useEffect(refresh, [tick]);

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
      onOk: async () => {
        const r = await fetch('/api/changes/rollback', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(path ? { path } : { all: true }),
        }).then((x) => x.json());
        if (r.error) { message.error(r.error); return; }
        message.success(path ? '已恢复' : `已回滚 ${r.restored} 个文件`);
        refresh();
      },
    });
  };

  const dedupChanges = (() => {
    const seen = new Set<string>();
    return changes.filter((c) => !seen.has(c.path) && seen.add(c.path)).slice(0, 8);
  })();

  return (
    <div style={{
      width: tab === 'code' ? 560 : 300, flexShrink: 0, display: 'flex', flexDirection: 'column',
      borderLeft: '1px solid var(--border)', background: 'var(--bg1)', transition: 'width .2s',
    }}>
      <div style={{ display: 'flex', borderBottom: '1px solid var(--border)' }}>
        {(['panel', 'code'] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} style={{
            flex: 1, padding: '9px 0', border: 'none', cursor: 'pointer', fontSize: '.82em',
            background: tab === t ? 'var(--bg2)' : 'transparent',
            color: tab === t ? 'var(--text)' : 'var(--text3)',
            borderBottom: tab === t ? '2px solid var(--accent)' : '2px solid transparent',
          }}>
            {t === 'panel' ? '📊 面板' : '📄 代码'}
          </button>
        ))}
      </div>

      {tab === 'code' ? <CodeTab /> : (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <Section title="📊 实时状态">
            <div className="stat-grid">
              <div className="stat-item"><div className="label">步骤</div><div className="value">{stats.steps}</div></div>
              <div className="stat-item"><div className="label">回溯</div><div className="value">{stats.backtracks}</div></div>
              <div className="stat-item"><div className="label">本次 Tokens</div><div className="value">{stats.tokens}</div></div>
              <div className="stat-item"><div className="label">耗时</div><div className="value">{stats.duration_ms}ms</div></div>
            </div>
          </Section>

          <Section title="🪙 Token 用量统计" extra={
            <a style={{ fontSize: '.9em', cursor: 'pointer' }} onClick={async () => {
              await apiDelete('/tokens'); refresh(); message.info('Token 统计已重置');
            }}>重置</a>
          }>
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
            {dedupChanges.length === 0 ? (
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
            {htmlFiles.length === 0 ? (
              <em className="hint-text">项目中暂无 HTML 文件</em>
            ) : htmlFiles.slice(0, 8).map((f: any) => (
              <div key={f.path} style={{ padding: '4px 0', fontSize: '.8em', cursor: 'pointer', color: 'var(--text2)' }}
                onClick={() => useUi.getState().openPreview({ url: `/api/preview/file?path=${encodeURIComponent(f.path)}`, label: f.path })}>
                🔍 {f.path}
              </div>
            ))}
          </Section>

          <Section title="🛡️ 审计概览">
            {!audit || !audit.entries?.length ? (
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
      )}
    </div>
  );
}
