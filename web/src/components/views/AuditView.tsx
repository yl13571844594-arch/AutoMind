// 🛡️ 安全审计：工具调用风险评估与授权决策记录；专业版可导出 PDF。
import { App, Button, Card, Space, Tag, Typography } from 'antd';
import { apiDelete, apiGet } from '../../api/client';
import { useAsync } from '../../lib/useAsync';
import { useApp } from '../../store/app';
import { AsyncBoundary } from '../ui/AsyncBoundary';

const { Text } = Typography;

export default function AuditView() {
  // 三态取数：此前失败时 data 恒为 null，`if (!data) return <Card loading />`
  // 会让界面**永远转圈**——服务挂了和加载慢在界面上完全无法区分。
  const st = useAsync<any>(() => apiGet('/audit'), []);
  const reload = st.reload;

  return (
    <AsyncBoundary state={st} what="审计日志">
      {(data) => <AuditBody data={data} reload={reload} />}
    </AsyncBoundary>
  );
}

function AuditBody({ data, reload }: { data: any; reload: () => void }) {
  const { message, modal } = App.useApp();
  const featureOn = useApp((s) => s.featureOn);
  const s = data.summary || {};

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
        <h3>🛡️ 安全审计日志</h3>
        <Space>
          <Button size="small" title={featureOn('audit_export') ? '导出 PDF 审计报告' : '专业版功能'}
            onClick={() => {
              if (!featureOn('audit_export')) { message.error('「审计报告导出（PDF）」是专业版功能'); return; }
              window.open('/api/audit/export?format=pdf', '_blank');
            }}>📄 导出报告{featureOn('audit_export') ? '' : ' 🔒'}</Button>
          <Button size="small" danger onClick={() => modal.confirm({
            title: '清空审计日志？',
            onOk: async () => { await apiDelete('/audit'); reload(); message.info('审计日志已清空'); },
          })}>清空</Button>
        </Space>
      </Space>
      <div className="stat-grid" style={{ margin: '12px 0', gridTemplateColumns: 'repeat(4,1fr)' }}>
        <div className="stat-item"><div className="label">总调用</div><div className="value">{s.total}</div></div>
        <div className="stat-item"><div className="label">放行</div><div className="value ok">{s.allow}</div></div>
        <div className="stat-item"><div className="label">需确认</div><div className="value" style={{ color: 'var(--yellow)' }}>{s.ask_user}</div></div>
        <div className="stat-item"><div className="label">高危操作</div><div className="value" style={{ color: 'var(--red)' }}>{s.dangerous}</div></div>
      </div>
      {(data.entries || []).length === 0 && (
        <em className="hint-text">暂无工具调用记录。在工作/编程模式执行任务后，这里会记录每次操作的风险评估与授权决策。</em>
      )}
      {(data.entries || []).map((e: any, i: number) => (
        <Card key={i} size="small" style={{
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
      ))}
    </div>
  );
}
