// 📊 统计分析：社区版基础聚合 + 专业版高级仪表盘（命中率环图/趋势/记忆指标）。
import { App, Button, Card, Space, Tag } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet } from '../../api/client';
import { MODE_LABELS } from '../../store/app';
import { Badge, EmptyState, EntityCard, SectionTitle, Tile, ViewHead } from '../ui/Panel';

const MODE_ICONS: Record<string, string> = {
  chat: '💬', work: '⚙️', coding: '💻', multi: '👥', loop: '🔁', other: '❔',
};
// 早期记录没存 interaction，服务端归到 other 桶 —— 界面上不能直接显示
// 一个英文的 "other"，那既不像模式名也不像错误，用户只会困惑
const MODE_NAME = (m: string) => MODE_LABELS[m] || (m === 'other' ? '未记录模式' : m);

/** 毫秒转成人读得懂的量级 —— 6 位数字的毫秒值没人能一眼读出是几分钟。 */
const fmtMs = (ms: number) => {
  if (!ms) return '0 ms';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`;
};

const ringColor = (p: number | null) => p == null ? 'var(--text3)' : p >= 80 ? 'var(--green)' : p >= 50 ? 'var(--accent)' : p >= 30 ? 'var(--yellow)' : 'var(--red)';

function Ring({ label, pct }: { label: string; pct: number | null }) {
  const v = pct == null ? '—' : pct + '%';
  const deg = (pct || 0) * 3.6;
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{
        width: 74, height: 74, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: `conic-gradient(${ringColor(pct)} ${deg}deg, var(--bg3) ${deg}deg)`, margin: '0 auto',
      }}>
        <div style={{
          width: 56, height: 56, borderRadius: '50%', background: 'var(--bg1)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '.86em',
        }}>{v}</div>
      </div>
      <div className="hint-text" style={{ marginTop: 4 }}>{label}</div>
    </div>
  );
}

function Sparkline({ points }: { points: any[] }) {
  const vals = points.map((p) => p.tool_hit_rate).filter((v) => v != null);
  if (vals.length < 2) return <div className="hint-text">数据不足，至少需 2 次任务</div>;
  const w = 280, h = 50;
  const step = w / (vals.length - 1);
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / 100) * h).toFixed(1)}`).join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: '100%', height: 50 }} preserveAspectRatio="none">
      <defs>
        <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--accent)" stopOpacity=".35" />
          <stop offset="1" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`0,${h} ${pts} ${w},${h}`} fill="url(#sg)" />
      <polyline points={pts} fill="none" stroke={ringColor(vals[vals.length - 1])} strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: '.86em' }}>
      <span style={{ color: 'var(--text2)' }}>{label}</span><b>{value}</b>
    </div>
  );
}

export default function StatsView() {
  const { message } = App.useApp();
  const [base, setBase] = useState<any>(null);
  const [detail, setDetail] = useState<any>(null);
  const [hist, setHist] = useState<any>(null);

  const reload = async () => {
    try {
      const [b, d, h] = await Promise.all([apiGet('/stats'), apiGet('/stats/detail'), apiGet('/stats/history')]);
      setBase(b);
      setDetail(d && d.feature === 'advanced_stats' ? null : d);
      setHist(h && h.feature ? null : h);
    } catch { /* ignore */ }
  };
  useEffect(() => { reload(); }, []);

  if (!base) return <Card loading />;

  const tk = base.tokens || {};
  const toolTags = Object.entries(base.tool_usage || {}).slice(0, 8).map(([t, n]: any) => (
    <Tag key={t} color="green" style={{ margin: 3 }}>{t} ×{n}</Tag>
  ));

  if (!detail) {
    // 社区版基础统计
    const byMode = base.by_mode || {};
    const rate = base.success_rate || 0;
    return (
      <div>
        <ViewHead icon="📊" title="统计分析"
                  sub="本机任务的执行概况；数据只存在本地，不会上传。"
                  extra={<Button size="small" onClick={reload}>🔄 刷新</Button>} />
        <div className="tile-grid">
          <Tile label="📋 任务总数" value={base.tasks_total || 0}
                foot={`成功 ${base.success_total || 0} 次`} />
          <Tile label="✅ 成功率" value={rate} unit="%"
                tone={rate >= 80 ? 'green' : rate >= 50 ? 'yellow' : 'red'}
                foot={base.tasks_total ? '基于全部历史任务' : '暂无任务'} />
          <Tile label="⏱ 平均耗时" value={fmtMs(base.avg_duration_ms || 0)} foot="每次任务" />
          <Tile label="🔢 累计 Token" value={((tk.prompt || 0) + (tk.completion || 0)).toLocaleString()}
                foot={`输入 ${(tk.prompt || 0).toLocaleString()} · 输出 ${(tk.completion || 0).toLocaleString()}`} />
        </div>

        <SectionTitle count={Object.keys(byMode).length}>🧭 按模式聚合</SectionTitle>
        {Object.keys(byMode).length ? (
          <div className="ent-grid">
            {Object.entries(byMode).map(([m, v]: any) => (
              <EntityCard key={m} icon={MODE_ICONS[m] || '❔'} title={MODE_NAME(m)}
                badges={<Badge tone="muted">{v.count} 次</Badge>}
                meta={<>成功 {v.success} · {v.tokens.toLocaleString()} tk · 平均 {fmtMs(v.avg_ms)}</>} />
            ))}
          </div>
        ) : <EmptyState icon="🧭" title="还没有任务记录" hint="在对话工作台跑一个任务后，这里会按模式汇总耗时与 Token。" />}

        <SectionTitle count={Object.keys(base.tool_usage || {}).length}>🛠 工具使用 Top</SectionTitle>
        {toolTags.length ? <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>{toolTags}</div>
          : <EmptyState icon="🛠" title="暂无工具调用" hint="对话模式不调用工具；试试「工作」或「编程」模式。" />}

        <Card size="small" style={{ marginTop: 14 }}>
          🔒 <b>高级统计仪表盘</b>（命中率环图 · 上下文使用率 · Token 效率 · 记忆指标 · 趋势折线）为<b>专业版</b>功能
          <div className="hint-text">安装 automind-pro 并配置许可证后即可解锁</div>
        </Card>
      </div>
    );
  }

  const hr = detail.hit_rates || {};
  const ctx = detail.context || { estimated_tokens: 0, max_tokens: 100000, usage_pct: 0 };
  const eff = detail.efficiency || {};
  const mem = detail.memory || {};
  const totals = detail.totals || {};

  return (
    <div>
      <ViewHead icon="📊" title="高级统计仪表盘"
                sub="命中率、上下文占用、Token 效率与记忆指标；数据只存在本地。"
                extra={<Button size="small" onClick={reload}>🔄 刷新</Button>} />
      <div className="tile-grid">
        <Tile label="📋 任务 / 成功" value={`${totals.tasks_total || 0} / ${totals.tasks_success || 0}`}
              foot="累计执行" />
        <Tile label="🛠 工具调用 / 成功" value={`${totals.tool_calls || 0} / ${totals.tool_successes || 0}`}
              foot="累计调用" />
        <Tile label="🎯 综合命中率"
              value={hr.average_hit_rate != null ? hr.average_hit_rate : '—'}
              unit={hr.average_hit_rate != null ? '%' : undefined}
              tone={hr.average_hit_rate == null ? undefined
                : hr.average_hit_rate >= 80 ? 'green' : hr.average_hit_rate >= 50 ? 'yellow' : 'red'}
              foot="工具/计划/成功/自纠 平均" />
        <Tile label="📐 Token 效率"
              value={eff.token_efficiency_chars_per_token ?? '—'}
              unit={eff.token_efficiency_chars_per_token != null ? ' 字/tk' : undefined}
              foot="产出字符 ÷ 消耗 Token" />
      </div>
      <Card size="small" title="命中率仪表盘" style={{ marginTop: 10 }}>
        <div style={{ display: 'flex', justifyContent: 'space-around', flexWrap: 'wrap', gap: 12 }}>
          <Ring label="工具命中" pct={hr.tool_hit_rate ?? null} />
          <Ring label="计划命中" pct={hr.plan_hit_rate ?? null} />
          <Ring label="任务成功" pct={hr.task_success_rate ?? null} />
          <Ring label="自我修正" pct={hr.self_correction_rate ?? null} />
        </div>
        <div style={{ textAlign: 'center', marginTop: 10 }}>
          {hr.average_hit_rate != null
            ? <Tag color="blue">★ 综合平均命中率 {hr.average_hit_rate}%</Tag>
            : <span className="hint-text">暂无足够数据计算命中率</span>}
        </div>
      </Card>
      <Card size="small" title="上下文使用率" style={{ marginTop: 10 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '.86em' }}>
          <span>{(ctx.estimated_tokens || 0).toLocaleString()} / {(ctx.max_tokens || 0).toLocaleString()} tokens</span>
          <b>{ctx.usage_pct}%</b>
        </div>
        <div style={{ height: 8, background: 'var(--bg3)', borderRadius: 4, marginTop: 6, overflow: 'hidden' }}>
          <div style={{ width: `${Math.min(ctx.usage_pct || 0, 100)}%`, height: '100%', background: 'var(--accent-grad)' }} />
        </div>
        <div className="hint-text" style={{ marginTop: 6 }}>
          {ctx.compressed ? `⚠ 已触发压缩 · 摘要 ${ctx.summary_length} 字` : '未触发压缩'} · 窗口 {ctx.message_count} 条消息
        </div>
      </Card>
      <Card size="small" title="效率与用量" style={{ marginTop: 10 }}>
        <Metric label="Token 效率" value={eff.token_efficiency_chars_per_token != null ? `${eff.token_efficiency_chars_per_token} 字/Token` : '—'} />
        <Metric label="累计 Token（输入/输出）" value={`${(eff.total_prompt_tokens || 0).toLocaleString()} / ${(eff.total_completion_tokens || 0).toLocaleString()}`} />
        <Metric label="总输出字符" value={(eff.total_output_chars || 0).toLocaleString()} />
        <Metric label="任务 / 成功" value={`${totals.tasks_total || 0} / ${totals.tasks_success || 0}`} />
        <Metric label="工具调用 / 成功" value={`${totals.tool_calls || 0} / ${totals.tool_successes || 0}`} />
      </Card>
      {hist && (
        <Card size="small" title={`📈 工具命中率趋势（最近 ${hist.count} 次）`} style={{ marginTop: 10 }}>
          <Sparkline points={hist.points || []} />
        </Card>
      )}
      <Card size="small" title="🧠 记忆系统" style={{ marginTop: 10 }}>
        <Metric label="向量存储" value={`${mem.long_term_docs || 0} docs`} />
        <Metric label="知识图谱" value={`${mem.kg_entities || 0} 实体 / ${mem.kg_relations || 0} 关系`} />
        <Metric label="短期窗口" value={`${mem.short_term_msgs || 0} 条消息`} />
      </Card>
      <Card size="small" title="工具使用 Top" style={{ marginTop: 10 }}>
        {toolTags.length ? toolTags : <em className="hint-text">暂无工具调用</em>}
        <div className="hint-text" style={{ marginTop: 8 }}>
          审批请求 {base.audit?.ask_user || 0} · 高危 {base.audit?.dangerous || 0} · 定时任务 {base.scheduled_tasks || 0}
          <Button size="small" style={{ float: 'right' }} onClick={async () => { await apiDelete('/tokens'); reload(); message.info('Token 统计已重置'); }}>重置Token</Button>
        </div>
      </Card>
    </div>
  );
}
