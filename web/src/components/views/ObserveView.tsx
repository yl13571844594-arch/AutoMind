// 📈 观测中心：执行流程可视化（DAG）+ 实时看板。
//
// 版本边界：
//   社区版 —— 仅「当前任务」的实时 DAG，只读、无历史、无看板（下方显示升级卡片）；
//   专业版 —— 追加运行历史回看、实时看板聚合与导出；
//   企业版 —— 看板再加按会话（多用户）维度分组。
//
// DAG 用原生 SVG 手绘（不引入 d3/reactflow 等依赖）：桌面版整包离线分发，
// 每多一个前端依赖都会加大体积与离线风险，而这里的图是分层树状结构，
// 布局规则简单，自绘完全够用且渲染更快。
import {
  App, Badge, Button, Card, Empty, Segmented, Space, Statistic, Table, Tag, Tooltip, Typography,
} from 'antd';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { apiGet } from '../../api/client';
import { SID } from '../../api/client';
import { useApp } from '../../store/app';
import { type DagNode, useObserve } from '../../store/observe';

const { Text, Paragraph } = Typography;

// ── 状态配色（与全局 CSS 变量保持一致，暗/亮主题自动适配）──
const STATUS_STYLE: Record<string, { fill: string; stroke: string; label: string }> = {
  pending: { fill: 'var(--bg3)', stroke: 'var(--border)', label: '待执行' },
  running: { fill: 'rgba(123,159,255,.18)', stroke: 'var(--accent)', label: '执行中' },
  ok: { fill: 'rgba(82,196,26,.16)', stroke: '#52c41a', label: '成功' },
  fail: { fill: 'rgba(255,77,79,.16)', stroke: '#ff4d4f', label: '失败' },
  backtrack: { fill: 'rgba(250,173,20,.18)', stroke: '#faad14', label: '回溯' },
  cancelled: { fill: 'var(--bg3)', stroke: 'var(--text3)', label: '已中断' },
};

const NODE_W = 190;
const NODE_H = 46;
const GAP_Y = 26;
const GAP_X = 34;

interface Placed extends DagNode { x: number; y: number }

/** 分层布局：主链（task→step→step…）竖排，工具调用挂在所属步骤右侧。 */
function layout(nodes: DagNode[], edges: { f: string; t: string; kind: string }[]) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const callChildren = new Map<string, string[]>();
  for (const e of edges) {
    if (e.kind === 'call') {
      callChildren.set(e.f, [...(callChildren.get(e.f) || []), e.t]);
    }
  }
  // 主链顺序：root 开头，其余按 seq/sub 边推进；孤立节点兜底追加
  const chain: string[] = [];
  const seen = new Set<string>();
  let cur: string | undefined = nodes.find((n) => n.id === 'root')?.id;
  while (cur && !seen.has(cur)) {
    chain.push(cur); seen.add(cur);
    cur = edges.find((e) => e.f === cur && e.kind !== 'call')?.t;
  }
  for (const n of nodes) {
    if (!seen.has(n.id) && n.kind !== 'action') { chain.push(n.id); seen.add(n.id); }
  }

  const placed: Placed[] = [];
  let y = 16;
  for (const id of chain) {
    const node = byId.get(id);
    if (!node) continue;
    placed.push({ ...node, x: 16, y });
    const calls = callChildren.get(id) || [];
    calls.forEach((cid, i) => {
      const c = byId.get(cid);
      if (c) placed.push({ ...c, x: 16 + NODE_W + GAP_X, y: y + i * (NODE_H + 10) });
    });
    y += Math.max(NODE_H + GAP_Y, calls.length * (NODE_H + 10) + GAP_Y);
  }
  return { placed, height: y + 10, width: 16 + NODE_W * 2 + GAP_X + 16 };
}

function fmtMs(ms?: number | null) {
  if (!ms && ms !== 0) return '—';
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** 统计小卡片 —— 观测中心顶部的指标栅格，统一配色与间距。 */
function StatCard({ icon, title, value, tone }: {
  icon: string; title: string; value: ReactNode; tone?: 'ok' | 'warn' | 'danger';
}) {
  const color = tone === 'danger' ? '#ff4d4f' : tone === 'warn' ? '#faad14' : tone === 'ok' ? '#52c41a' : 'var(--text)';
  return (
    <div style={{
      flex: '1 1 110px', minWidth: 110, borderRadius: 10, padding: '12px 14px',
      border: '1px solid var(--border)', background: 'var(--bg2)',
    }}>
      <div className="hint-text" style={{ fontSize: '.74em', display: 'flex', alignItems: 'center', gap: 4 }}>
        <span>{icon}</span>{title}
      </div>
      <div style={{ fontSize: '1.5em', fontWeight: 600, color, marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </div>
    </div>
  );
}

function DagCanvas({ onSelect, selected }: { onSelect: (id: string) => void; selected: string | null }) {
  const nodes = useObserve((s) => s.nodes);
  const edges = useObserve((s) => s.edges);
  const wrapRef = useRef<HTMLDivElement>(null);
  const { placed, height, width } = useMemo(() => layout(nodes, edges), [nodes, edges]);

  // 有新节点时自动滚到底，保持"正在执行"可见
  useEffect(() => {
    const el = wrapRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [nodes.length]);

  if (!nodes.length) {
    return <Empty description="尚无执行数据 —— 发起一个任务即可看到实时执行流程" />;
  }

  const pos = new Map(placed.map((p) => [p.id, p]));
  return (
    <div ref={wrapRef} style={{
      overflow: 'auto', maxHeight: 480, border: '1px solid var(--border)', borderRadius: 10,
      background: 'var(--bg0)', padding: 8,
    }}>
      <svg width={width} height={height} style={{ display: 'block', minWidth: '100%' }}>
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3"
            orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L0,6 L7,3 z" fill="var(--text3)" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const a = pos.get(e.f); const b = pos.get(e.t);
          if (!a || !b) return null;
          const isCall = e.kind === 'call';
          const x1 = isCall ? a.x + NODE_W : a.x + NODE_W / 2;
          const y1 = isCall ? a.y + NODE_H / 2 : a.y + NODE_H;
          const x2 = isCall ? b.x : b.x + NODE_W / 2;
          const y2 = isCall ? b.y + NODE_H / 2 : b.y;
          const d = isCall
            ? `M${x1},${y1} C${x1 + 18},${y1} ${x2 - 18},${y2} ${x2},${y2}`
            : `M${x1},${y1} L${x2},${y2}`;
          return (
            <path key={i} d={d} fill="none" stroke="var(--text3)" strokeWidth={1.2}
              strokeDasharray={isCall ? '4 3' : undefined} markerEnd="url(#arrow)" opacity={0.75} />
          );
        })}
        {placed.map((n) => {
          const st = STATUS_STYLE[n.status] || STATUS_STYLE.pending;
          const isSel = selected === n.id;
          return (
            <g key={n.id} onClick={() => onSelect(n.id)} style={{ cursor: 'pointer' }}>
              <title>{`${n.kind} · ${n.label || n.id}${n.tool ? `\n工具：${n.tool}` : ''}\n状态：${(STATUS_STYLE[n.status] || {}).label || n.status}`}</title>
              <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx={8}
                fill={st.fill} stroke={isSel ? 'var(--accent)' : st.stroke}
                strokeWidth={isSel ? 2.2 : 1.2} />
              {n.status === 'running' && (
                <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx={8}
                  fill="none" stroke="var(--accent)" strokeWidth={2} opacity={0.9}>
                  <animate attributeName="opacity" values="0.9;0.25;0.9" dur="1.3s" repeatCount="indefinite" />
                </rect>
              )}
              <text x={n.x + 11} y={n.y + 19} fontSize={11} fill="var(--text3)">
                {n.kind === 'task' ? '任务' : n.kind === 'action' ? '🛠 工具' : '步骤'}
                {n.tool && n.kind !== 'action' ? ` · ${String(n.tool).slice(0, 12)}` : ''}
              </text>
              <text x={n.x + 11} y={n.y + 35} fontSize={12.5} fill="var(--text)">
                {(n.label || '').slice(0, 22)}{(n.label || '').length > 22 ? '…' : ''}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function UpgradeCard() {
  return (
    <Card size="small" style={{ borderStyle: 'dashed' }}>
      🔒 <b>实时看板与运行历史</b> 为<b>专业版</b>功能
      <div className="hint-text" style={{ marginTop: 6, lineHeight: 1.9 }}>
        社区版已包含 <b>当前任务的实时执行 DAG</b>（只读、仅当前任务）。<br />
        升级专业版可解锁：历史运行回看、成功率/耗时分位/工具热度/失败归因看板、JSON 导出；
        企业版另增按会话（多用户）维度的分组统计。<br />
        安装 automind-pro 并配置许可证（AUTOMIND_LICENSE）后重启服务即可解锁。
      </div>
    </Card>
  );
}

export default function ObserveView() {
  const { message } = App.useApp();
  const featureOn = useApp((s) => s.featureOn);
  const hasPro = featureOn('observability');

  const st = useObserve();
  const [tab, setTab] = useState<string>('live');
  const [dash, setDash] = useState<any>(null);
  const [runs, setRuns] = useState<any[]>([]);
  const [windowH, setWindowH] = useState(24);

  // 刷新页面后用后端快照补回当前任务的图
  useEffect(() => {
    if (st.nodes.length) return;
    apiGet(`/observe/dag?session_id=${encodeURIComponent(SID)}`)
      .then((r) => { if (r?.graph) useObserve.getState().hydrate(r.graph); })
      .catch(() => { /* 社区版首次无数据属正常 */ });
  }, []);

  const loadPro = () => {
    if (!hasPro) return;
    apiGet(`/observe/dashboard?window_h=${windowH}`).then(setDash).catch(() => {});
    apiGet('/observe/runs?limit=50').then((r) => setRuns(r?.runs || [])).catch(() => {});
  };
  useEffect(() => { loadPro(); }, [hasPro, windowH, st.status]);

  const sel = st.nodes.find((n) => n.id === st.selected) || null;
  const statusTag = st.status === 'running' ? <Badge status="processing" text="执行中" />
    : st.status === 'ok' ? <Badge status="success" text="已完成" />
      : st.status === 'fail' ? <Badge status="error" text="失败" />
        : st.status === 'cancelled' ? <Badge status="default" text="已中断" />
          : <Badge status="default" text="空闲" />;

  const live = (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card size="small" title={<Space>🧭 当前任务执行流程 {statusTag}</Space>}
        extra={<Space>
          {!hasPro && <Tag>社区版 · 仅当前任务</Tag>}
          <Button size="small" onClick={() => { useObserve.getState().clear(); message.info('已清空当前视图'); }}>
            清空
          </Button>
        </Space>}>
        {st.task && <Paragraph style={{ marginBottom: 10 }} ellipsis={{ rows: 2 }}>
          <Text type="secondary">任务：</Text>{st.task}
        </Paragraph>}
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
          <StatCard icon="🧩" title="步骤" value={st.counters.steps} />
          <StatCard icon="🛠" title="工具调用" value={st.counters.actions} />
          <StatCard icon="❌" title="失败" value={st.counters.failures} tone={st.counters.failures ? 'danger' : undefined} />
          <StatCard icon="↩️" title="回溯" value={st.counters.backtracks} tone={st.counters.backtracks ? 'warn' : undefined} />
          <StatCard icon="⏱" title="耗时" value={fmtMs(
            st.startedAt ? (st.finishedAt || Date.now()) - st.startedAt : 0)} />
          <StatCard icon="🔁" title="状态" value={st.status === 'running' ? '执行中'
            : st.status === 'ok' ? '完成' : st.status === 'fail' ? '失败'
              : st.status === 'cancelled' ? '中断' : '空闲'} tone={st.status === 'fail' ? 'danger' : st.status === 'ok' ? 'ok' : undefined} />
        </div>

        <DagCanvas selected={st.selected} onSelect={(id) => useObserve.getState().select(id)} />

        <div style={{ marginTop: 12, display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          {Object.entries(STATUS_STYLE).map(([k, v]) => (
            <Tag key={k} style={{
              marginInlineEnd: 0, fontSize: '.76em', background: v.fill,
              borderColor: v.stroke, color: 'var(--text2)',
            }}>{v.label}</Tag>
          ))}
          {st.counters.truncated > 0 && (
            <Text type="secondary" style={{ fontSize: '.78em' }}>
              （节点过多，已省略 {st.counters.truncated} 个）
            </Text>
          )}
        </div>
      </Card>

      {sel && (
        <Card size="small" title={`节点详情 · ${sel.label || sel.id}`}
          extra={<Button size="small" type="text" onClick={() => useObserve.getState().select(null)}>关闭</Button>}>
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Text type="secondary">类型：{sel.kind === 'task' ? '任务' : sel.kind === 'action' ? '工具调用' : '计划步骤'}
              {sel.tool ? ` · 工具 ${sel.tool}` : ''}</Text>
            <Text type="secondary">状态：{(STATUS_STYLE[sel.status] || {}).label || sel.status}</Text>
            <Text type="secondary">耗时：{fmtMs(sel.t0 && sel.t1 ? sel.t1 - sel.t0 : null)}</Text>
            {sel.error && <Paragraph type="danger" style={{ marginBottom: 0 }}
              ellipsis={{ rows: 4, expandable: true }}>{sel.error}</Paragraph>}
          </Space>
        </Card>
      )}

      {!hasPro && <UpgradeCard />}
    </Space>
  );

  const dashboard = !hasPro ? <UpgradeCard /> : (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Space>
        <Text type="secondary">统计窗口</Text>
        <Segmented value={windowH} onChange={(v) => setWindowH(Number(v))}
          options={[{ label: '1 小时', value: 1 }, { label: '24 小时', value: 24 },
            { label: '7 天', value: 168 }, { label: '30 天', value: 720 }]} />
        <Button size="small" onClick={loadPro}>刷新</Button>
        <Button size="small" onClick={() => window.open('/api/observe/export', '_blank')}>
          导出 JSON
        </Button>
      </Space>
      {dash && (
        <>
          <Space size="large" wrap>
            <Statistic title="运行次数" value={dash.total} />
            <Statistic title="成功率" value={dash.success_rate} suffix="%"
              valueStyle={{ color: dash.success_rate >= 80 ? '#52c41a' : '#faad14' }} />
            <Statistic title="失败" value={dash.failed} valueStyle={{ color: dash.failed ? '#ff4d4f' : undefined }} />
            <Statistic title="平均耗时" value={fmtMs(dash.avg_ms)} />
            <Statistic title="P50" value={fmtMs(dash.p50_ms)} />
            <Statistic title="P95" value={fmtMs(dash.p95_ms)} />
            <Statistic title="总步骤" value={dash.total_steps} />
            <Statistic title="总工具调用" value={dash.total_actions} />
          </Space>
          <Card size="small" title="🔧 工具热度与失败率">
            <Table size="small" pagination={false} rowKey="tool"
              dataSource={dash.top_tools || []}
              columns={[
                { title: '工具', dataIndex: 'tool' },
                { title: '调用次数', dataIndex: 'calls', width: 110 },
                { title: '失败', dataIndex: 'failures', width: 90,
                  render: (v: number) => (v ? <Text type="danger">{v}</Text> : v) },
                { title: '失败率', width: 100,
                  render: (_: any, r: any) => `${r.calls ? Math.round(r.failures / r.calls * 100) : 0}%` },
              ]} />
          </Card>
          <Card size="small" title="⚠ 失败归因 Top">
            <Table size="small" pagination={false} rowKey="reason"
              dataSource={dash.top_failures || []}
              locale={{ emptyText: '窗口内无失败记录' }}
              columns={[
                { title: '原因', dataIndex: 'reason', ellipsis: true },
                { title: '次数', dataIndex: 'count', width: 90 },
              ]} />
          </Card>
          {dash.by_session && (
            <Card size="small" title={<Space>👥 会话维度 <Tag color="purple">企业版</Tag></Space>}>
              <Table size="small" pagination={false} rowKey="session_id"
                dataSource={dash.by_session}
                columns={[
                  { title: '会话', dataIndex: 'session_id', ellipsis: true },
                  { title: '运行次数', dataIndex: 'runs', width: 110 },
                  { title: '失败', dataIndex: 'failed', width: 90 },
                ]} />
            </Card>
          )}
        </>
      )}
    </Space>
  );

  const history = !hasPro ? <UpgradeCard /> : (
    <Card size="small" title="📜 运行历史" extra={<Button size="small" onClick={loadPro}>刷新</Button>}>
      <Table size="small" rowKey="id" dataSource={runs} pagination={{ pageSize: 10 }}
        locale={{ emptyText: '暂无历史运行' }}
        columns={[
          { title: '任务', dataIndex: 'task', ellipsis: true,
            render: (v: string) => v || <Text type="secondary">（无描述）</Text> },
          { title: '结局', dataIndex: 'status', width: 90,
            render: (v: string) => <Tag color={v === 'ok' ? 'success' : v === 'fail' ? 'error' : 'default'}>
              {v === 'ok' ? '成功' : v === 'fail' ? '失败' : '中断'}</Tag> },
          { title: '步骤', dataIndex: 'steps', width: 70 },
          { title: '工具', dataIndex: 'actions', width: 70 },
          { title: '耗时', dataIndex: 'elapsed_ms', width: 90, render: fmtMs },
          { title: '开始时间', dataIndex: 'started_at', width: 160,
            render: (v: number) => (v ? new Date(v).toLocaleString() : '—') },
          { title: '', width: 80,
            render: (_: any, r: any) => (
              <Button size="small" type="link" onClick={async () => {
                const d = await apiGet(`/observe/runs/${encodeURIComponent(r.id)}`);
                if (d?.graph) { useObserve.getState().hydrate(d.graph); setTab('live'); }
                else message.warning('该运行记录已过期');
              }}>回看</Button>
            ) },
        ]} />
    </Card>
  );

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Space align="center" style={{ justifyContent: 'space-between', width: '100%' }}>
        <h3 style={{ margin: 0 }}>📈 观测中心</h3>
        <Tooltip title={hasPro ? '专业版：含历史与看板' : '社区版：仅当前任务的只读实时 DAG'}>
          <Tag color={hasPro ? 'blue' : undefined}>{hasPro ? '专业版' : '社区版 · 简化'}</Tag>
        </Tooltip>
      </Space>
      <Segmented value={tab} onChange={(v) => setTab(String(v))}
        options={[
          { label: '实时流程', value: 'live' },
          { label: hasPro ? '实时看板' : '实时看板 🔒', value: 'dashboard' },
          { label: hasPro ? '运行历史' : '运行历史 🔒', value: 'history' },
        ]} />
      {tab === 'live' ? live : tab === 'dashboard' ? dashboard : history}
    </Space>
  );
}
