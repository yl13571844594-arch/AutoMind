// 🔑 凭据与并发配额（v1.10.0，企业版，R-305 / R-306）。
// 凭据只写不读：列表只有名字与来源；值写进去之后界面上再也看不到。
import { Alert, App, Button, Input, InputNumber, Modal, Space, Table, Tag } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost, errText } from '../../api/client';

export default function SecretsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = App.useApp();
  const [locked, setLocked] = useState('');
  const [rows, setRows] = useState<any[]>([]);
  const [encrypted, setEncrypted] = useState(false);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [conc, setConc] = useState<{ default: number | null; teams: Record<string, number> }>({ default: null, teams: {} });
  const [snap, setSnap] = useState<any>({});
  const [teamName, setTeamName] = useState('');
  const [teamLimit, setTeamLimit] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await apiGet('/workflow/secrets');
      if (d?.feature) { setLocked(String(d.error || '')); return; }
      setLocked('');
      setRows(d.secrets || []);
      setEncrypted(!!d.encrypted);
      const c = await apiGet('/workflow/concurrency');
      setConc({ default: c.config?.default ?? null, teams: c.config?.teams || {} });
      setSnap(c);
    } catch (e) { message.error(errText(e)); }
  }, [message]);
  useEffect(() => { if (open) load(); }, [open, load]);

  const save = async () => {
    try {
      await apiPost('/workflow/secrets', { name: name.trim(), value });
      message.success(`已保存凭据 ${name.trim()}`);
      setName(''); setValue('');
      load();
    } catch (e) { message.error(errText(e), 6); }
  };
  const remove = async (n: string) => {
    try { await apiDelete(`/workflow/secrets/${encodeURIComponent(n)}`); load(); }
    catch (e) { message.error(errText(e)); }
  };
  const saveConc = async (next: typeof conc) => {
    try {
      const d = await apiPost('/workflow/concurrency', next);
      setConc({ default: d.config?.default ?? null, teams: d.config?.teams || {} });
      message.success('已保存并发配额');
    } catch (e) { message.error(errText(e), 6); }
  };

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={720} title="🔑 凭据与并发配额（企业版）" destroyOnClose>
      {locked ? <Alert type="info" showIcon message="企业版功能" description={locked} /> : (
        <div>
          <h4 style={{ marginTop: 0 }}>凭据</h4>
          <div className="hint-text" style={{ marginBottom: 6 }}>
            工作流里写 <code>{'{{ secrets.NAME }}'}</code>（只能用在 tool 步骤的参数里）。值不会出现在报告、日志、
            运行记录与审计导出里。也可以用环境变量 <code>AUTOMIND_SECRET_NAME</code> 注入（优先级更高）。
            {encrypted ? ' 凭据文件已静态加密。' : ' 凭据文件未加密：设置 AUTOMIND_SECRETS_KEY（Fernet 密钥）即可加密存储。'}
          </div>
          <Space style={{ marginBottom: 8 }} wrap>
            <Input style={{ width: 200 }} placeholder="名字，如 ITSM_TOKEN" value={name}
              onChange={(e) => setName(e.target.value.toUpperCase())} />
            <Input.Password style={{ width: 260 }} placeholder="值（至少 4 个字符）" value={value}
              onChange={(e) => setValue(e.target.value)} autoComplete="new-password" />
            <Button type="primary" disabled={!name.trim() || value.length < 4} onClick={save}>保存</Button>
          </Space>
          <Table size="small" rowKey="name" pagination={false} dataSource={rows}
            locale={{ emptyText: '还没有凭据' }}
            columns={[
              { title: '名字', dataIndex: 'name', render: (v: string) => <code>{v}</code> },
              { title: '来源', dataIndex: 'source', width: 110,
                render: (v: string, r: any) => (v === 'env' ? <Tag>环境变量</Tag>
                  : <Tag color={r.encrypted ? 'success' : 'default'}>{r.encrypted ? '文件 · 已加密' : '文件'}</Tag>) },
              { title: '更新时间', dataIndex: 'updated_at', width: 170 },
              { title: '', width: 70, render: (_: any, r: any) => (r.source === 'file'
                ? <Button size="small" type="link" danger onClick={() => remove(r.name)}>删除</Button> : null) },
            ]} />

          <h4 style={{ marginTop: 18 }}>并发配额（按团队）</h4>
          <div className="hint-text" style={{ marginBottom: 6 }}>
            同一团队同时运行的工作流数超过上限时，后来的运行自动排队，界面显示位次。团队取 SSO 用户的 team
            （没配置则按角色）；触发器发起的运行归入 triggers 团队。留空 = 不限。
          </div>
          <Space wrap>
            <span>默认上限</span>
            <InputNumber min={1} value={conc.default ?? undefined} placeholder="不限"
              onChange={(v) => setConc({ ...conc, default: v ? Number(v) : null })} />
            <Button onClick={() => saveConc(conc)}>保存</Button>
          </Space>
          <div style={{ marginTop: 8 }}>
            {Object.entries(conc.teams).map(([t, n]) => (
              <Tag key={t} closable onClose={() => {
                const teams = { ...conc.teams }; delete teams[t];
                saveConc({ ...conc, teams });
              }}>{t}：{n}</Tag>
            ))}
            <Space style={{ marginTop: 6 }}>
              <Input size="small" style={{ width: 140 }} placeholder="团队名" value={teamName}
                onChange={(e) => setTeamName(e.target.value)} />
              <InputNumber size="small" min={1} placeholder="上限" value={teamLimit ?? undefined}
                onChange={(v) => setTeamLimit(v ? Number(v) : null)} />
              <Button size="small" disabled={!teamName.trim() || !teamLimit} onClick={() => {
                saveConc({ ...conc, teams: { ...conc.teams, [teamName.trim()]: teamLimit as number } });
                setTeamName(''); setTeamLimit(null);
              }}>添加</Button>
            </Space>
          </div>
          <div className="hint-text" style={{ marginTop: 8 }}>
            正在运行：{Object.entries(snap.running || {}).map(([t, ids]: any) => `${t} ${ids.length} 个`).join('，') || '无'}
            {' · '}排队：{Object.entries(snap.queued || {}).map(([t, ids]: any) => `${t} ${ids.length} 个`).join('，') || '无'}
          </div>
        </div>
      )}
    </Modal>
  );
}
