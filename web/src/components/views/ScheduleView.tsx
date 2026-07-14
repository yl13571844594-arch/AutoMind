// ⏰ 定时任务（专业版 scheduler；社区版显示升级卡片）。
import { App, Button, Card, Input, Select, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { MODE_LABELS, useApp } from '../../store/app';

const { Text } = Typography;

const fmtInterval = (s: number) => s >= 86400 ? Math.round(s / 86400) + '天'
  : s >= 3600 ? Math.round(s / 3600) + '小时' : s >= 60 ? Math.round(s / 60) + '分钟' : s + '秒';

export default function ScheduleView() {
  const { message } = App.useApp();
  const featureOn = useApp((s) => s.featureOn);
  const [list, setList] = useState<any[]>([]);
  const [form, setForm] = useState({ name: '', task: '', interaction: 'chat', interval: 3600 });

  const locked = !featureOn('scheduler');
  const reload = () => {
    if (locked) return;
    apiGet('/schedule').then((r) => setList(Array.isArray(r) ? r : [])).catch(() => {});
  };
  useEffect(() => { reload(); }, [locked]);

  if (locked) {
    return (
      <div>
        <h3>⏰ 定时任务</h3>
        <Card size="small" style={{ marginTop: 12 }}>
          🔒 <b>定时任务</b>（按固定间隔自动执行任意模式的任务、后台调度与结果记录）为<b>专业版</b>功能
          <div className="hint-text" style={{ marginTop: 4 }}>安装 automind-pro 并配置许可证（AUTOMIND_LICENSE）后重启服务即可解锁</div>
        </Card>
      </div>
    );
  }

  return (
    <div>
      <h3>⏰ 定时任务</h3>
      <Card size="small" style={{ marginTop: 10 }}>
        <Input placeholder="名称（可选）" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <Input.TextArea style={{ marginTop: 8 }} rows={2} placeholder="要定时执行的任务内容..."
          value={form.task} onChange={(e) => setForm({ ...form, task: e.target.value })} />
        <Space style={{ marginTop: 8 }}>
          <Select value={form.interaction} onChange={(v) => setForm({ ...form, interaction: v })} style={{ width: 100 }}
            options={['chat', 'work', 'coding', 'loop', 'multi'].map((m) => ({ value: m, label: MODE_LABELS[m] }))} />
          <Select value={form.interval} onChange={(v) => setForm({ ...form, interval: v })} style={{ width: 120 }}
            options={[
              { value: 300, label: '每5分钟' }, { value: 3600, label: '每小时' },
              { value: 21600, label: '每6小时' }, { value: 86400, label: '每天' },
            ]} />
          <Button type="primary" onClick={async () => {
            if (!form.task.trim()) { message.error('请输入任务内容'); return; }
            await apiPost('/schedule', form);
            message.success('定时任务已添加');
            setForm({ ...form, name: '', task: '' });
            reload();
          }}>添加</Button>
        </Space>
      </Card>
      <div style={{ marginTop: 10 }}>
        {list.map((s) => (
          <Card key={s.id} size="small" style={{ marginBottom: 8, borderColor: s.enabled ? 'var(--green)' : undefined }}>
            <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
              <div>
                <Text strong>{s.name}</Text> <Tag color="green">{MODE_LABELS[s.interaction] || s.interaction}</Tag>
                <div style={{ fontSize: '.82em', color: 'var(--text2)', marginTop: 4 }}>{(s.task || '').slice(0, 80)}</div>
                <div className="hint-text" style={{ marginTop: 2 }}>
                  每 {fmtInterval(s.interval)} · 已运行 {s.runs} 次 {s.last_status ? '· ' + s.last_status : ''}
                  {s.enabled && s.next_in != null ? ` · 下次 ${fmtInterval(s.next_in)}后` : ''}
                </div>
              </div>
              <Space>
                <Button size="small" onClick={async () => { await apiPost(`/schedule/${s.id}/run`); message.info('已触发运行'); }}>立即运行</Button>
                <Button size="small" onClick={async () => { await apiPost(`/schedule/${s.id}/toggle`, { enabled: !s.enabled }); reload(); }}>{s.enabled ? '暂停' : '启用'}</Button>
                <Button size="small" danger onClick={async () => { await apiDelete(`/schedule/${s.id}`); message.info('已删除'); reload(); }}>删除</Button>
              </Space>
            </Space>
          </Card>
        ))}
        {!list.length && <em className="hint-text">暂无定时任务</em>}
      </div>
    </div>
  );
}
