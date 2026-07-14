// 🎓 专家市场：官方精选 / 我的专家 / 激活；专业版进阶（分享/导入导出/统计）。
import { App, Button, Card, Input, Modal, Space, Tag, Typography, Upload } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { chatSid, useApp } from '../../store/app';

const { Text, Paragraph } = Typography;

export default function ExpertsView() {
  const { message, modal } = App.useApp();
  const [data, setData] = useState<any>(null);
  const [form, setForm] = useState({ icon: '🎓', name: '', desc: '', prompt: '' });
  const [statsOpen, setStatsOpen] = useState(false);
  const [stats, setStats] = useState<any[]>([]);

  const reload = () => apiGet(`/experts?session_id=${encodeURIComponent(chatSid())}`).then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  if (!data) return <Card loading />;

  const active = data.active || '';
  const custom = data.installed.filter((e: any) => e.source === 'custom');
  const officialInstalled = data.installed.filter((e: any) => e.source === 'official');

  const activate = async (id: string) => {
    const r = await apiPost('/experts/activate', { id });
    if (r.error) { message.error(r.error); return; }
    message.success(id ? '专家已激活 — 之后的任务将带该角色设定执行' : '已取消专家模式');
    reload();
    useApp.getState().refreshExpert();
  };

  const del = (id: string) => modal.confirm({
    title: '删除/卸载该专家？',
    onOk: async () => { await apiDelete(`/experts/${encodeURIComponent(id)}`); message.info('已删除'); reload(); useApp.getState().refreshExpert(); },
  });

  const expertCard = (e: any, actions: React.ReactNode) => (
    <Card key={e.id} size="small" style={{ marginBottom: 8, borderColor: active === e.id ? 'var(--green)' : undefined }}>
      <Space align="start" style={{ width: '100%' }}>
        <span style={{ fontSize: '1.5em' }}>{e.icon || '🎓'}</span>
        <div style={{ flex: 1 }}>
          <Text strong>{e.name}</Text>
          {active === e.id && <Tag color="green" style={{ marginLeft: 6 }}>✓ 已激活</Tag>}
          {e.shared && <Tag style={{ marginLeft: 4, fontSize: '.68em' }}>👥 已分享{data.approval && !e.approved ? '·待审批' : ''}</Tag>}
          <div className="hint-text">{e.desc || ''}</div>
          {data.pro && e.usage ? <div className="hint-text">已调用 {e.usage} 次</div> : null}
        </div>
        <Space>{actions}</Space>
      </Space>
    </Card>
  );

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }} wrap>
        <h3>🎓 专家市场 <span className="hint-text" style={{ fontWeight: 400 }}>专家 = 可复用的角色设定；激活后所有任务带该设定执行</span></h3>
        {data.pro ? (
          <Space>
            <Button size="small" onClick={() => window.open('/api/experts/export', '_blank')}>📤 导出</Button>
            <Upload showUploadList={false} accept=".json" beforeUpload={async (f) => {
              try {
                const d = JSON.parse(await f.text());
                const r = await apiPost('/experts/import', d);
                if (r.error) message.error(r.error);
                else { message.success(`已导入 ${r.imported} 个专家`); reload(); }
              } catch { message.error('JSON 解析失败'); }
              return false;
            }}><Button size="small">📥 导入</Button></Upload>
            <Button size="small" onClick={async () => {
              const r = await apiGet('/experts/stats');
              if (r.error) { message.error(r.error); return; }
              setStats(r.stats || []); setStatsOpen(true);
            }}>📊 统计</Button>
          </Space>
        ) : <span className="hint-text">导入/导出/统计/分享 🔒 专业版</span>}
      </Space>

      {active && (
        <Card size="small" style={{ marginBottom: 10, borderColor: 'var(--green)' }}>
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <span>当前激活：<b>{(data.installed.find((e: any) => e.id === active) || {}).name || active}</b></span>
            <Button size="small" onClick={() => activate('')}>取消激活</Button>
          </Space>
        </Card>
      )}

      <Text strong>⭐ 我的专家（{data.custom_limit == null ? custom.length + ' · 不限' : `${data.custom_count}/${data.custom_limit}`}）</Text>
      <div style={{ marginTop: 8 }}>
        {custom.map((e: any) => expertCard(e, (
          <>
            {active !== e.id && <Button size="small" type="primary" onClick={() => activate(e.id)}>激活</Button>}
            {data.pro && !e.shared && <Button size="small" title="团队分享" onClick={async () => {
              const r = await apiPost(`/experts/${encodeURIComponent(e.id)}/share`, { shared: true });
              if (r.error) message.error(r.error);
              else { message.success(data.approval ? '已提交分享，待管理员审批' : '已分享给团队'); reload(); }
            }}>👥</Button>}
            <Button size="small" danger type="text" onClick={() => del(e.id)}>✕</Button>
          </>
        )))}
        {!custom.length && <div className="hint-text" style={{ margin: '6px 0' }}>还没有自建专家 — 在下方创建，或先从官方精选安装。</div>}
      </div>

      <Space.Compact style={{ width: '100%', marginTop: 8 }}>
        <Input style={{ width: 60 }} maxLength={4} value={form.icon} onChange={(e) => setForm({ ...form, icon: e.target.value })} />
        <Input style={{ width: 180 }} maxLength={24} placeholder="专家名称（如：SQL 优化师）" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <Input placeholder="一句话简介" maxLength={80} value={form.desc} onChange={(e) => setForm({ ...form, desc: e.target.value })} />
        <Button type="primary" onClick={async () => {
          if (!form.name.trim() || !form.prompt.trim()) { message.error('名称与角色设定必填'); return; }
          const r = await apiPost('/experts', { ...form, session_id: chatSid() });
          if (r.error) { message.error(r.error); return; }
          message.success(`专家「${form.name}」已创建`);
          setForm({ icon: '🎓', name: '', desc: '', prompt: '' });
          reload();
        }}>创建专家</Button>
      </Space.Compact>
      <Input.TextArea style={{ marginTop: 8 }} rows={3} value={form.prompt}
        onChange={(e) => setForm({ ...form, prompt: e.target.value })}
        placeholder="角色设定提示词（它是谁、擅长什么、输出风格与硬性要求）" />

      <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
        <Text strong>🏛️ 官方精选（{data.official.length}）</Text>
        <span className="hint-text" style={{ marginLeft: 6 }}>一键安装即可激活使用</span>
      </div>
      <div style={{ marginTop: 8 }}>
        {data.official.map((e: any) => {
          const inst = officialInstalled.find((i: any) => i.id === e.id);
          return expertCard(inst || e, e.installed ? (
            <>
              {active !== e.id && <Button size="small" type="primary" onClick={() => activate(e.id)}>激活</Button>}
              <Button size="small" danger type="text" onClick={() => del(e.id)}>✕</Button>
            </>
          ) : (
            <Button size="small" onClick={async () => {
              const r = await apiPost('/experts/install', { id: e.id });
              if (r.error) { message.error(r.error); return; }
              message.success(`已安装「${r.expert.name}」`);
              reload();
            }}>⬇ 安装</Button>
          ));
        })}
      </div>

      <Modal title="📊 专家使用统计" open={statsOpen} onCancel={() => setStatsOpen(false)} footer={null}>
        <Paragraph type="secondary" style={{ fontSize: '.82em' }}>哪个专家被调用最多（按任务注入次数累计）。</Paragraph>
        {stats.length === 0 ? <em className="hint-text">暂无使用记录</em> : stats.map((s) => {
          const max = Math.max(1, ...stats.map((x) => x.usage));
          return (
            <div key={s.name} style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '8px 0' }}>
              <span style={{ width: 130, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.icon} {s.name}</span>
              <div style={{ flex: 1, height: 10, background: 'var(--bg2)', borderRadius: 5, overflow: 'hidden' }}>
                <div style={{ width: `${Math.round((s.usage / max) * 100)}%`, height: '100%', background: 'var(--accent-grad)' }} />
              </div>
              <b style={{ width: 46, textAlign: 'right' }}>{s.usage}</b>
            </div>
          );
        })}
      </Modal>
    </div>
  );
}
