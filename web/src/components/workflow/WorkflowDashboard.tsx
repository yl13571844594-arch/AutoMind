// 📊 工作流看板（v1.9.0，专业版，R-207）：按工作流统计成功率、耗时、Token 与趋势。
// 数据源与运行记录是同一份（每次运行计一次），看板上的数字能逐条对回报告。
import { Alert, Empty, Progress, Segmented, Space, Table, Tag, Tooltip } from 'antd';
import { useEffect, useState } from 'react';
import { apiGet, errText } from '../../api/client';
import { TRIGGER_ZH } from '../../store/workflow';

const ms = (v?: number) => {
  if (!v) return '—';
  if (v < 1000) return `${Math.round(v)} 毫秒`;
  return v >= 60000 ? `${(v / 60000).toFixed(1)} 分` : `${(v / 1000).toFixed(1)} 秒`;
};

function Spark({ daily }: { daily: { date: string; runs: number; ok: number; failed: number }[] }) {
  if (!daily?.length) return null;
  const max = Math.max(...daily.map((d) => d.runs), 1);
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: 28 }}>
      {daily.slice(-30).map((d) => (
        <Tooltip key={d.date} title={`${d.date}：${d.runs} 次，成功 ${d.ok}，失败 ${d.failed}`}>
          <div style={{
            width: 5, height: Math.max(3, (d.runs / max) * 28), borderRadius: 1,
            background: d.failed ? 'var(--red)' : 'var(--green)', opacity: 0.8,
          }} />
        </Tooltip>
      ))}
    </div>
  );
}

export default function WorkflowDashboard({ onOpenFile }: { onOpenFile?: (file: string) => void }) {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<any>(null);
  const [locked, setLocked] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    setErr('');
    apiGet(`/workflow/dashboard?days=${days}`)
      .then((d) => {
        if (d?.feature) { setLocked(String(d.error || '')); return; }   // 社区版：403 + 升级提示
        setData(d); setLocked('');
      })
      .catch((e) => setErr(errText(e)));
  }, [days]);

  if (locked) {
    return <Alert type="info" showIcon message="工作流看板为专业版功能"
      description={<span>{locked}<br /><span className="hint-text">
        社区版保留最近 50 次运行记录（在每个工作流的「运行记录」里查看）；专业版保留 90 天并提供按工作流的统计看板。
      </span></span>} />;
  }
  if (err) return <div className="wf-step-error">{err}</div>;
  if (!data) return <div className="hint-text">正在统计…</div>;

  const t = data.totals || {};
  return (
    <div>
      <Space style={{ marginBottom: 10 }} wrap>
        <Segmented size="small" value={days} onChange={(v) => setDays(Number(v))}
          options={[{ label: '7 天', value: 7 }, { label: '30 天', value: 30 }, { label: '90 天', value: 90 }]} />
        <span className="hint-text">
          共 {t.runs || 0} 次运行 · 成功率 {t.success_rate ?? '—'}% · Token {t.tokens || 0}
          {data.keep_days ? ` · 运行记录保留 ${data.keep_days} 天` : ''}
        </span>
      </Space>
      {!data.workflows?.length ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这段时间没有工作流运行" />
      ) : (
        <Table size="small" rowKey="file" pagination={false} dataSource={data.workflows}
          columns={[
            { title: '工作流', render: (_: any, w: any) => (
              <div>
                <a onClick={() => onOpenFile?.(w.file)}>{w.workflow}</a>
                <div className="hint-text">{w.file}</div>
              </div>) },
            { title: '运行', width: 150, render: (_: any, w: any) => (
              <div className="hint-text">
                {w.runs} 次：成功 {w.ok}{w.partial ? ` · 部分 ${w.partial}` : ''}
                {w.failed ? ` · 失败 ${w.failed}` : ''}{w.cancelled ? ` · 取消 ${w.cancelled}` : ''}
                <div>{Object.entries(w.by_trigger || {}).map(([k, v]) => (
                  <Tag key={k} style={{ marginInlineEnd: 2 }}>{TRIGGER_ZH[k] || k} {String(v)}</Tag>))}</div>
              </div>) },
            { title: '成功率', width: 110, render: (_: any, w: any) => (w.success_rate == null ? '—'
              : <Progress percent={w.success_rate} size="small"
                status={w.success_rate < 80 ? 'exception' : 'success'} format={(p) => `${p}%`} />) },
            { title: '耗时（平均 / P95）', width: 140,
              render: (_: any, w: any) => <span className="hint-text">{ms(w.avg_duration_ms)} / {ms(w.p95_duration_ms)}</span> },
            { title: 'Token', width: 90, render: (_: any, w: any) => <span className="hint-text">{w.tokens}</span> },
            { title: '最常失败', render: (_: any, w: any) => (
              <span className="hint-text">{(w.failed_steps || []).map((f: any) => `${f.step}×${f.count}`).join('、') || '—'}</span>) },
            { title: '趋势', width: 170, render: (_: any, w: any) => <Spark daily={w.daily} /> },
          ]} />
      )}
    </div>
  );
}
