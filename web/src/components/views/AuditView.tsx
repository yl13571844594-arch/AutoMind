// 🛡️ 安全审计（v1.11.19）：工具调用风险评估与授权决策记录 + 运行底座治理账本。
//
// 这里有两层数据，界面刻意把它们分开，避免把"内存审计"当成"持久化账本"：
//   · 上层「运行底座治理」卡片：GET /api/governance/summary —— 治理策略快照 +
//     持久化账本状态（条数 / 文件 / 体积 / 最近序号 / 哈希链校验），并提供
//     「验证审计链」「导出 JSON」两个动作；
//   · 下方列表：优先消费 GET /api/governance/audit（JSONL 持久化账本，覆盖
//     direct / react / plan / workflow 全通道）；若账本还没有记录（刚升级、
//     或审计写入被关闭），回退展示旧 GET /api/audit 的内存审计并明确提示，
//     任何一种取数失败都不会让本页空白或无限转圈。
// 清空按钮只清内存审计（DELETE /api/audit）；账本文件不提供界面清空入口 ——
// 那是取证数据，误删之后就再也无法自证发生过什么。
import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { App, Button, Card, Select, Skeleton, Space, Tag, Tooltip, Typography } from 'antd';
import { ApiError, apiDelete, apiGet, errText } from '../../api/client';
import { writeAction } from '../../lib/writeAction';
import { useAsync, type AsyncState } from '../../lib/useAsync';
import { useApp } from '../../store/app';
import { EmptyState, ViewHead } from '../ui/Panel';

const { Text } = Typography;

// ── 展示映射：后端存英文枚举，界面统一显示中文 ──────────────────────
const APPROVAL_LABEL: Record<string, string> = {
  ask: '询问', auto: '自动', approve_all: '全批准',
};
const APPROVAL_COLOR: Record<string, string> = {
  ask: 'gold', auto: 'green', approve_all: 'red',
};
const CONFLICT_LABEL: Record<string, string> = {
  off: '关闭', warn: '警告', block: '阻断',
};
const EDITION_LABEL: Record<string, string> = {
  community: '社区版', pro: '专业版', enterprise: '企业版',
};
const DECISION_LABEL: Record<string, string> = {
  allow: '放行', deny: '拒绝', ask_user: '需确认', approved: '已批准',
  denied: '已拒绝', modified: '已修改', timeout: '超时', error: '错误',
  allow_unattended: '无人值守放行', deny_unattended: '无人值守拦截', executed: '执行',
};
const DECISION_COLOR: Record<string, string> = {
  allow: 'green', approved: 'green', allow_unattended: 'green', executed: 'blue',
  deny: 'red', denied: 'red', deny_unattended: 'red', error: 'red',
  ask_user: 'gold', modified: 'gold', timeout: 'orange',
};
const SOURCE_LABEL: Record<string, string> = {
  direct: '直连', react: 'ReAct', plan: '计划', workflow: '工作流',
};
const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'direct', label: '直连（direct）' },
  { value: 'react', label: 'ReAct' },
  { value: 'plan', label: '计划（plan）' },
  { value: 'workflow', label: '工作流（workflow）' },
];
const DECISION_OPTIONS = [
  { value: '', label: '全部决策' },
  ...Object.keys(DECISION_LABEL).map((k) => ({ value: k, label: `${DECISION_LABEL[k]}（${k}）` })),
];
const LIMIT_OPTIONS = [
  { value: 50, label: '最近 50 条' },
  { value: 100, label: '最近 100 条' },
  { value: 200, label: '最近 200 条' },
  { value: 500, label: '最近 500 条' },
];

type GovSummaryState = AsyncState<any>;

/** 数字兜底：后端字段缺失/为 null 时显示 0，而不是空白或非法数字。 */
function num(v: any): number {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

/** 体积格式化（只做整数与一位小数运算，不依赖特殊数字格式化 API）。 */
function fmtBytes(v: any): string {
  const n = num(v);
  if (n < 1024) return `${n} B`;
  const kb = Math.round((n / 1024) * 10) / 10;
  if (kb < 1024) return `${kb} KB`;
  return `${Math.round((n / (1024 * 1024)) * 10) / 10} MB`;
}

/** epoch 秒 → HH:MM:SS（与旧 /api/audit 的 time 字段保持同一展示口径）。 */
function fmtClock(ts: any): string {
  const n = typeof ts === 'number' ? ts : Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '-';
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return '-';
  const pad = (x: number) => String(x).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** epoch 秒 → 完整时间（悬停提示用，便于跨天排查）。 */
function fmtFull(ts: any): string {
  const n = typeof ts === 'number' ? ts : Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  const d = new Date(n * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

function onOff(v: any): ReactNode {
  return v ? <Tag color="green">开启</Tag> : <Tag>关闭</Tag>;
}

export default function AuditView() {
  const { message, modal } = App.useApp();
  const featureOn = useApp((s) => s.featureOn);

  // 旧接口（内存审计）：保留既有统计卡，并在账本为空时回退展示。
  const legacy = useAsync<any>(() => apiGet('/audit'), []);
  // 旧接口失败且没有旧数据时不阻塞页面：下方只显示一条错误条（治理账本仍可用）。

  const [source, setSource] = useState('');
  const [decision, setDecision] = useState('');
  const [limit, setLimit] = useState(200);
  const query = useMemo(() => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (source) q.set('source', source);
    if (decision) q.set('decision', decision);
    return q.toString();
  }, [source, decision, limit]);

  // 治理摘要（策略快照 + 账本状态）：进入页面 / 点刷新时取 —— 它要扫全账本，
  // 不必随筛选变化重扫；useAsync 会在后台刷新时保留旧值，不会闪空。
  const govSummary = useAsync<any>(() => apiGet('/governance/summary'), []);
  // 治理账本列表（GET /api/governance/audit）：来源 / 决策 / limit 变化时重取。
  const govAuditState = useAsync<any>(() => apiGet(`/governance/audit?${query}`), [query]);

  const s = legacy.data?.summary || {};
  const govAudit = govAuditState.data;
  const ledgerEntries: any[] = Array.isArray(govAudit?.entries) ? govAudit.entries : [];
  const legacyEntries: any[] = Array.isArray(legacy.data?.entries) ? legacy.data.entries : [];
  const hasFilters = Boolean(source || decision);
  const govUnavailable = Boolean(govAuditState.error && !govAuditState.loaded);
  const govLoading = govAuditState.loading && !govAuditState.loaded;
  const legacyLoading = legacy.loading && !legacy.loaded;
  const govErrorText = govAuditState.error
    ? (govAuditState.error instanceof ApiError ? govAuditState.error.friendly : govAuditState.error.message)
    : '';

  let listNode: ReactNode;
  if (govUnavailable) {
    // 治理账本接口不可用（远程未配管理员令牌 / 老版本后端）→ 回退旧接口，绝不空白
    listNode = (
      <>
        <div className="load-stale">
          ⚠️ 治理账本接口不可用（{govErrorText}），下方回退展示本次运行的内存审计。
          <Button size="small" type="link" onClick={govAuditState.reload}>重试</Button>
        </div>
        {legacyLoading && <Skeleton active paragraph={{ rows: 3 }} />}
        {!legacyLoading && legacyEntries.map((e: any, i: number) => <LegacyEntry key={i} e={e} />)}
        {!legacyLoading && legacyEntries.length === 0 && (
          <em className="hint-text">暂无工具调用记录。治理账本恢复后可在这里查看持久化记录。</em>
        )}
      </>
    );
  } else if (govLoading) {
    listNode = <Skeleton active paragraph={{ rows: 3 }} />;
  } else if (ledgerEntries.length > 0) {
    // 后端 tail() 按 seq 升序（旧→新）；界面最新在前，方便排查刚发生的事。
    const shown = [...ledgerEntries].reverse();
    listNode = (
      <>
        <div className="hint-text" style={{ marginBottom: 6 }}>
          持久化治理账本：{num(govAudit?.count) || shown.length} 条
          {govAudit?.truncated ? `（已截断，仅显示最近 ${limit} 条）` : ''}
          ；账本文件是取证数据，界面不提供清空入口（需要归档请用「导出 JSON」）。
        </div>
        {shown.map((e: any, i: number) => <LedgerEntry key={`${e?.seq}-${i}`} e={e} />)}
      </>
    );
  } else if (hasFilters) {
    listNode = (
      <EmptyState icon="🔎" title="没有符合筛选条件的账本记录"
        hint="试试放宽「来源 / 决策」筛选；或确认新记录产生后账本已写入。" />
    );
  } else if (legacyEntries.length > 0) {
    // 账本已启用但还没有记录（刚升级 / 审计开关刚打开）→ 兼容旧数据并说明
    listNode = (
      <>
        <div className="load-stale">
          ℹ️ 账本已启用，新产生的记录会持久化在这里；当前账本还没有记录，
          下方展示本次运行的内存审计（重启即清空）。
        </div>
        {legacyEntries.map((e: any, i: number) => <LegacyEntry key={i} e={e} />)}
      </>
    );
  } else if (legacyLoading) {
    listNode = <Skeleton active paragraph={{ rows: 3 }} />;
  } else {
    listNode = (
      <em className="hint-text">
        暂无工具调用记录。在工作 / 编程 / 工作流模式执行任务后，这里会记录每次操作的风险评估、
        授权决策与执行结果（持久化到治理账本）。
      </em>
    );
  }

  return (
    <div>
      <ViewHead
        icon="🛡️"
        title="安全审计日志"
        sub="运行底座治理：策略快照 / 持久化治理账本 / 哈希链校验；下方记录每次工具调用的放行、需确认与高危拦截。"
        extra={<Space>
          <Button size="small" title={featureOn('audit_export') ? '导出 PDF 审计报告' : '专业版功能'}
            onClick={() => {
              if (!featureOn('audit_export')) { message.error('「审计报告导出（PDF）」是专业版功能'); return; }
              window.open('/api/audit/export?format=pdf', '_blank');
            }}>📄 导出报告{featureOn('audit_export') ? '' : ' 🔒'}</Button>
          <Button size="small" danger onClick={() => modal.confirm({
            title: '清空内存审计日志？',
            content: '只清空本次运行的内存审计日志（旧 /api/audit 数据），不影响持久化治理账本。'
              + '治理账本记录每次调用的取证信息、不支持在界面清空；如需归档请用「导出 JSON」。',
            onOk: writeAction('清空审计日志', async () => {
              await apiDelete('/audit');
              legacy.reload();
              message.info('内存审计日志已清空');
            }),
          })}>清空</Button>
        </Space>}
      />

      <GovernanceSection state={govSummary} />

      <div className="stat-grid" style={{ margin: '12px 0', gridTemplateColumns: 'repeat(4,1fr)' }}>
        <div className="stat-item"><div className="label">总调用</div><div className="value">{num(s.total)}</div></div>
        <div className="stat-item"><div className="label">放行</div><div className="value ok">{num(s.allow)}</div></div>
        <div className="stat-item"><div className="label">需确认</div><div className="value" style={{ color: 'var(--yellow)' }}>{num(s.ask_user)}</div></div>
        <div className="stat-item"><div className="label">高危操作</div><div className="value" style={{ color: 'var(--red)' }}>{num(s.dangerous)}</div></div>
      </div>
      <div className="hint-text" style={{ marginBottom: 8 }}>
        统计来自本次运行的内存审计日志（重启即清空）；持久化治理账本的状态见上方「运行底座治理」。
      </div>

      {legacy.error && legacy.loaded && (
        <div className="load-stale">
          ⚠️ 内存审计刷新失败（{legacy.error instanceof ApiError ? legacy.error.friendly : legacy.error.message}），
          下方为上一次的数据。
          <Button size="small" type="link" onClick={legacy.reload}>重试</Button>
        </div>
      )}

      <Space wrap style={{ margin: '8px 0' }}>
        <Select size="small" style={{ width: 150 }} value={source}
          options={SOURCE_OPTIONS} onChange={(v: string) => setSource(v)} aria-label="按来源筛选" />
        <Select size="small" style={{ width: 190 }} value={decision}
          options={DECISION_OPTIONS} onChange={(v: string) => setDecision(v)} aria-label="按决策筛选" />
        <Select size="small" style={{ width: 130 }} value={limit}
          options={LIMIT_OPTIONS} onChange={(v: number) => setLimit(v)} aria-label="返回条数" />
        <Button size="small" onClick={() => { govSummary.reload(); govAuditState.reload(); legacy.reload(); }}>刷新</Button>
      </Space>

      {listNode}
    </div>
  );
}

/** 「运行底座治理」卡片：策略快照 + 账本状态 + 链路校验 / 导出动作。 */
function GovernanceSection({ state }: { state: GovSummaryState }) {
  const { message } = App.useApp();
  const [verifying, setVerifying] = useState(false);
  const [verified, setVerified] = useState<any>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');

  const shell = (body: ReactNode) => (
    <Card size="small" style={{ marginBottom: 12 }} title="🏛 运行底座治理">
      {body}
    </Card>
  );

  if (state.loading && !state.loaded) {
    return shell(<Skeleton active paragraph={{ rows: 3 }} />);
  }
  if (state.error && !state.loaded) {
    const friendly = state.error instanceof ApiError ? state.error.friendly : state.error.message;
    const is403 = state.error instanceof ApiError && state.error.status === 403;
    return shell(
      <div className="load-error" style={{ padding: 10 }}>
        <div className="le-title">治理账本加载失败</div>
        <div className="le-msg">{friendly}</div>
        {is403 && (
          <div className="le-msg" style={{ marginTop: 6 }}>
            治理账本属于管理动作：远程访问需配置管理员令牌（浏览器 localStorage 的
            automind_admin_token，对应服务端 AUTOMIND_ADMIN_TOKEN）；本机访问无需令牌。
          </div>
        )}
        <Button size="small" onClick={state.reload} style={{ marginTop: 10 }}>重试</Button>
      </div>,
    );
  }

  const data = state.data || {};
  const policy = data.policy || {};
  const ledger = data.ledger || {};
  const summary = data.summary || {};
  const tools = policy.tools || {};
  const byTier = tools.by_tier || {};
  const dangerous: string[] = Array.isArray(tools.dangerous) ? tools.dangerous : [];
  const approval = String(policy.approval_mode || '');
  const conflict = String(policy.write_conflict_policy || '');
  const edition = String(policy.edition || '');
  const chain = verified || data.chain || null;
  const chainOk = chain ? chain.ok : null;
  const checked = chain ? num(chain.checked) : 0;
  const brokenAt = chain ? chain.broken_at : null;

  let chainNode: ReactNode;
  if (chainOk === true) {
    chainNode = (
      <span style={{ color: 'var(--green)' }}>
        ✅ 通过（{verified ? '完整校验' : '最近'} {checked} 条）
      </span>
    );
  } else if (chainOk === false) {
    chainNode = (
      <Tooltip title={[
        chain.reason ? `原因：${chain.reason}` : '',
        chain.file ? `文件：${chain.file}` : '',
        chain.expected ? `期望哈希：${chain.expected}` : '',
        chain.actual ? `实际哈希：${chain.actual}` : '',
      ].filter(Boolean).join('\n')}>
        <span style={{ color: 'var(--red)' }}>
          ❌ 失败：第 {brokenAt == null ? '?' : brokenAt} 条断链
          {chain.reason ? `（${chain.reason}）` : ''}
        </span>
      </Tooltip>
    );
  } else {
    chainNode = <span className="hint-text">未校验（点击「验证审计链」）</span>;
  }

  async function onVerify() {
    setVerifying(true);
    try {
      // GET /api/governance/verify —— 逐条重算哈希链（前端按钮「验证审计链」）
      const r = await apiGet<any>('/governance/verify?max_records=20000');
      setVerified(r);
      if (r && r.ok) message.success(`审计链校验通过：已校验 ${num(r.checked)} 条记录`);
      else message.warning(`审计链校验失败：第 ${r?.broken_at ?? '?'} 条记录断链（${r?.reason || '未知原因'}）`);
    } catch (e) {
      message.error(`验证审计链失败：${errText(e)}`);
    } finally {
      setVerifying(false);
    }
  }

  async function onExport() {
    setExporting(true);
    setExportError('');
    try {
      // GET /api/governance/export —— 下载 JSON（前端按钮「导出 JSON」）
      const data = await apiGet<any>('/governance/export?limit=1000');
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      a.href = url;
      a.download = `governance-ledger-${stamp}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 立刻 revoke 在部分浏览器会让下载拿不到内容，延迟释放更稳
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      const n = Array.isArray(data?.entries) ? data.entries.length : 0;
      message.success(`已导出 ${n} 条治理记录`);
    } catch (e) {
      setExportError(errText(e));
      message.error(`导出治理账本失败：${errText(e)}`);
    } finally {
      setExporting(false);
    }
  }

  const box: React.CSSProperties = {
    border: '1px solid var(--border)', borderRadius: 8, padding: '10px 12px', minWidth: 0,
  };
  const row: React.CSSProperties = {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    gap: 8, margin: '4px 0', fontSize: '.88em',
  };

  return shell(
    <>
      {state.error && state.loaded && (
        <div className="load-stale" style={{ marginBottom: 8 }}>
          ⚠️ 治理摘要刷新失败，下方为上一次的数据。
          <Button size="small" type="link" onClick={state.reload}>重试</Button>
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(300px,1fr))', gap: 12 }}>
        {/* 治理策略快照 */}
        <div style={box}>
          <div className="sec-title">
            <span>治理策略快照</span>
            <span className="sec-count">{EDITION_LABEL[edition] || edition || '未知版本'}</span>
          </div>
          <div style={row}>
            <span className="hint-text">审批模式</span>
            <span>
              <Tag color={APPROVAL_COLOR[approval] || 'default'}>
                {APPROVAL_LABEL[approval] || approval || '未知'}
              </Tag>
            </span>
          </div>
          <div style={row}>
            <span className="hint-text">工作区隔离</span>
            <span>{onOff(policy.workspace_isolation)}</span>
          </div>
          <div style={row}>
            <span className="hint-text">写冲突策略</span>
            <span>{CONFLICT_LABEL[conflict] || conflict || '未知'}</span>
          </div>
          <div style={row}>
            <span className="hint-text">审计开关</span>
            <span>{policy.audit_enabled === null ? <Tag>未知</Tag> : onOff(policy.audit_enabled)}</span>
          </div>
          <div style={row}>
            <span className="hint-text">风险分级</span>
            <span>
              共 {num(tools.total)} 个 · 安全 {num(byTier.safe)} / 敏感 {num(byTier.sensitive)} / 高危{' '}
              <span style={{ color: num(byTier.dangerous) ? 'var(--red)' : 'inherit' }}>{num(byTier.dangerous)}</span>
            </span>
          </div>
          <div style={{ marginTop: 4, fontSize: '.85em' }}>
            <div className="hint-text" style={{ marginBottom: 3 }}>高危工具</div>
            {dangerous.length > 0
              ? <Space size={[4, 4]} wrap>{dangerous.map((n: string) => <Tag key={n} color="red">{n}</Tag>)}</Space>
              : <span className="hint-text">无（当前注册表里没有高危等级工具）</span>}
          </div>
          <div className="mono hint-text" style={{ marginTop: 6, wordBreak: 'break-all' }}>
            数据目录：{policy.sensitive?.data_dir || '未知'}
          </div>
        </div>

        {/* 账本状态 + 校验 / 导出 */}
        <div style={box}>
          <div className="sec-title">
            <span>治理账本状态</span>
            <span className="sec-count">{ledger.enabled === false ? '写入已关闭' : '已启用'}</span>
          </div>
          <div style={row}>
            <span className="hint-text">记录条数</span>
            <b>{num(summary.total)}</b>
          </div>
          <div style={row}>
            <span className="hint-text">文件 / 体积</span>
            <span>{num(ledger.files)} 个 / {fmtBytes(ledger.bytes)}</span>
          </div>
          <div style={row}>
            <span className="hint-text">最近序号</span>
            <span className="mono">{num(ledger.last_seq)}</span>
          </div>
          <div style={row}>
            <span className="hint-text">链路校验</span>
            <span>{chainNode}</span>
          </div>
          <Space wrap style={{ marginTop: 8 }}>
            <Tooltip title="对账本 JSONL 逐条重算 sha256 并与前一条哈希比对；断链会给出位置与期望/实际哈希">
              <Button size="small" loading={verifying} onClick={onVerify}>🔗 验证审计链</Button>
            </Tooltip>
            <Tooltip title="下载最近 1000 条治理记录（JSON，含链路校验结果）；PDF 报告仍属专业版">
              <Button size="small" loading={exporting} onClick={onExport}>⬇ 导出 JSON</Button>
            </Tooltip>
          </Space>
          {exportError && (
            <div className="hint-text" style={{ color: 'var(--red)', marginTop: 6 }}>{exportError}</div>
          )}
          <div className="hint-text" style={{ marginTop: 8 }}>
            账本文件持久化保存、不支持界面清空（取证数据）；如需归档请「导出 JSON」。
          </div>
        </div>
      </div>
    </>,
  );
}

/** 持久化账本的一条记录（决策 + 执行都走这里）。 */
function LedgerEntry({ e }: { e: any }) {
  const row = e || {};
  const decision = String(row.decision || '');
  const tier = String(row.tier || '');
  const source = String(row.source || 'direct');
  const kind = String(row.kind || '');
  const params = row.params && typeof row.params === 'object' ? row.params : {};
  const border = tier === 'dangerous' ? 'var(--red)' : tier === 'sensitive' ? 'var(--yellow)' : 'var(--green)';
  const tierColor = tier === 'dangerous' ? 'red' : tier === 'sensitive' ? 'gold' : tier === 'safe' ? 'green' : 'default';
  const decisionColor = DECISION_COLOR[decision] || 'default';
  const risk = typeof row.risk === 'number' ? row.risk : '-';
  const duration = typeof row.duration_ms === 'number' ? Math.round(row.duration_ms) : null;
  return (
    <Card size="small" style={{ marginBottom: 8, borderColor: border }}>
      <span className="mono" title={fmtFull(row.ts)}>{fmtClock(row.ts)}</span>
      <Tag style={{ marginLeft: 6 }}>#{row.seq == null ? '?' : row.seq}</Tag>
      {kind === 'execution' && <Tag style={{ marginLeft: 6 }} color="blue">执行</Tag>}
      <Text strong style={{ marginLeft: 6 }}>{row.tool || '(未知工具)'}</Text>
      {tier && <Tag style={{ marginLeft: 6 }} color={tierColor}>{tier}</Tag>}
      <Tag style={{ marginLeft: 6 }} color="purple">{SOURCE_LABEL[source] || source}</Tag>
      {row.governed === false && <Tag style={{ marginLeft: 6 }} color="volcano">未受管</Tag>}
      <span style={{ float: 'right', fontSize: '.8em' }}>
        <Tag color={decisionColor}>{DECISION_LABEL[decision] || decision || '未知'}</Tag>
        <span className="hint-text">风险 {risk}</span>
      </span>
      <div style={{ fontSize: '.8em', color: 'var(--text2)', marginTop: 4 }}>
        {row.reason || (kind === 'execution'
          ? (row.success ? '执行成功' : '执行失败')
          : '')}
        {duration === null ? '' : ` · 耗时 ${duration}ms`}
        {row.error ? <span style={{ color: 'var(--red)' }}> · {row.error}</span> : null}
      </div>
      {Object.keys(params).length > 0 && (
        <div className="mono hint-text" style={{ marginTop: 3, wordBreak: 'break-all' }}>
          {JSON.stringify(params)}
        </div>
      )}
    </Card>
  );
}

/** 旧接口 /api/audit 的记录（内存审计，作为账本为空/不可用时的回退）。 */
function LegacyEntry({ e }: { e: any }) {
  return (
    <Card size="small" style={{
      marginBottom: 8,
      borderColor: e.tier === 'dangerous' ? 'var(--red)' : e.tier === 'sensitive' ? 'var(--yellow)' : 'var(--green)',
    }}>
      <span className="mono">{e.time}</span>
      <Text strong style={{ marginLeft: 8 }}>{e.tool}</Text>
      <Tag style={{ marginLeft: 6 }} color={e.tier === 'dangerous' ? 'red' : e.tier === 'sensitive' ? 'gold' : 'green'}>{e.tier}</Tag>
      <span style={{ float: 'right', fontSize: '.8em', color: e.decision === 'allow' ? 'var(--green)' : 'var(--yellow)' }}>
        {e.decision} · 风险{e.risk}
      </span>
      <div style={{ fontSize: '.8em', color: 'var(--text2)', marginTop: 4 }}>{e.reason}</div>
      {Object.keys(e.params || {}).length > 0 && (
        <div className="mono hint-text" style={{ marginTop: 3 }}>{JSON.stringify(e.params)}</div>
      )}
    </Card>
  );
}
