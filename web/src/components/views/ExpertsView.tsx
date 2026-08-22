// 🎓 专家市场：官方精选 / 我的专家 / 激活；专业版进阶（分享/导入导出/统计）。
import { App, Button, Card, Input, Modal, Space, Tag, Typography, Upload } from 'antd';
import { useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { writeAction } from '../../lib/writeAction';
import { useAsync } from '../../lib/useAsync';
import { chatSid, useApp } from '../../store/app';
import { AsyncBoundary } from '../ui/AsyncBoundary';
import { Badge, EmptyState, EntityCard, SectionTitle, ViewHead } from '../ui/Panel';

const { Text, Paragraph } = Typography;

export default function ExpertsView() {
  // 此前是 `.catch(() => {})` + `if (!data) return <Card loading />` ——
  // 取数失败时 data 永远是 null，专家市场就**永远转圈**，
  // 界面上"加载慢"和"接口挂了"长得一模一样。
  const st = useAsync<any>(
    () => apiGet(`/experts?session_id=${encodeURIComponent(chatSid())}`), []);
  return (
    <AsyncBoundary state={st} what="专家市场" rows={5}>
      {(data) => <ExpertsBody data={data} reload={st.reload} />}
    </AsyncBoundary>
  );
}

function ExpertsBody({ data, reload }: { data: any; reload: () => void }) {
  const { message, modal } = App.useApp();
  const [form, setForm] = useState({ icon: '🎓', name: '', desc: '', prompt: '' });
  const [statsOpen, setStatsOpen] = useState(false);
  const [stats, setStats] = useState<any[]>([]);

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
    onOk: writeAction('删除专家', async () => {
      await apiDelete(`/experts/${encodeURIComponent(id)}`);
      message.info('已删除'); reload(); useApp.getState().refreshExpert();
    }),
  });

  const expertCard = (e: any, actions: React.ReactNode) => (
    <EntityCard
      key={e.id}
      state={active === e.id ? 'ok' : undefined}
      icon={e.icon || '🎓'}
      title={e.name}
      badges={<>
        {active === e.id && <Badge tone="builtin">✓ 已激活</Badge>}
        {e.source === 'official' && <Badge tone="mcp">官方精选</Badge>}
        {e.shared && <Badge tone="muted">👥 已分享{data.approval && !e.approved ? ' · 待审批' : ''}</Badge>}
      </>}
      desc={e.desc || '（无简介）'}
      meta={data.pro && e.usage ? <>已调用 {e.usage} 次</> : undefined}
      actions={actions}
    />
  );

  return (
    <div>
      <ViewHead icon="🎓" title="专家市场"
                sub="专家 = 可复用的角色设定；激活后所有任务都会带上该设定执行。"
                extra={data.pro ? (
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
        ) : <span className="hint-text">导入 / 导出 / 统计 / 分享 🔒 专业版</span>} />

      {active && (
        <Card size="small" style={{ marginBottom: 10, borderColor: 'var(--green)' }}>
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <span>当前激活：<b>{(data.installed.find((e: any) => e.id === active) || {}).name || active}</b></span>
            <Button size="small" onClick={() => activate('')}>取消激活</Button>
          </Space>
        </Card>
      )}

      <SectionTitle count={custom.length}>
        ⭐ 我的专家{data.custom_limit == null ? '（不限）' : `（上限 ${data.custom_limit}）`}
      </SectionTitle>
      <div className="ent-grid">
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
      </div>
      {!custom.length && (
        <EmptyState icon="⭐" title="还没有自建专家"
                    hint="在下方填写名称与角色设定即可创建；也可以先从「官方精选」里安装一个改改。" />
      )}

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

      <SectionTitle count={data.official.length}>🏛️ 官方精选 · 一键安装即可激活</SectionTitle>
      <div className="ent-grid">
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
