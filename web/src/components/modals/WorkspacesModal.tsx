// 🗂 工作区管理：每个工作区 = 独立目录 + 独立上下文；含版本数量限额展示。
import { App, Button, Input, Modal, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { useApp } from '../../store/app';
import { useChat } from '../../store/chat';
import { useUi } from '../../store/ui';

const { Text, Paragraph } = Typography;

function wsHash(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h).toString(36).slice(0, 8);
}

export default function WorkspacesModal() {
  const { message, modal } = App.useApp();
  const open = useUi((s) => s.modal) === 'workspaces';
  const close = useUi((s) => s.closeModal);
  const running = useApp((s) => s.running);
  const wsActive = useApp((s) => s.wsActive);
  const [data, setData] = useState<any>({ workspaces: [], active: '' });
  const [quota, setQuota] = useState<any>(null);
  const [name, setName] = useState('');
  const [path, setPath] = useState('');

  const reload = () => {
    apiGet('/workspaces').then(setData).catch(() => {});
    apiGet('/quota').then(setQuota).catch(() => {});
  };
  useEffect(() => { if (open) reload(); }, [open]);

  const switchTo = async (n: string) => {
    if (running) { message.error('有任务正在执行，请先停止再切换工作区'); return; }
    const r = await apiPost('/workspaces/switch', { name: n });
    if (r.error) { message.error(r.error); return; }
    useApp.getState().setWorkspace(n, n ? '_w' + wsHash(r.project || n) : '');
    useChat.getState().reload();
    useApp.getState().loadStatus();
    close();
    message.success(n ? `已切换到工作区「${n}」` : '已回到默认工作区');
  };

  const add = async () => {
    if (!name.trim() || !path.trim()) { message.error('名称与目录路径均必填'); return; }
    const r = await apiPost('/workspaces', { name: name.trim(), path: path.trim() });
    if (r.error) { message.error(r.error); return; }
    message.success(`工作区「${name}」已保存`);
    setName(''); setPath('');
    reload();
  };

  const wsLimit = quota?.workspaces?.limit;

  return (
    <Modal title="🗂 工作区管理" open={open} onCancel={close} width={620} footer={null}>
      <Paragraph type="secondary" style={{ fontSize: '.84em' }}>
        每个工作区 = 独立目录 + 独立上下文。切换后 Agent 在新目录下操作，各工作区的会话内容互不可见。
        当前 Agent 目录：<span className="mono">{data.active || ''}</span>
        {wsLimit != null && (
          <> · 数量 <b>{(data.workspaces || []).length}/{wsLimit}</b>
            <span className="hint-text">（{quota?.edition === 'community' ? '社区版限 3 个，专业版 30 个，企业版不限' : '专业版限 30 个，企业版不限'}）</span></>
        )}
      </Paragraph>
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        {(data.workspaces || []).map((w: any) => {
          const isActive = wsActive === w.name;
          return (
            <div key={w.name} style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px',
              border: `1px solid ${isActive ? 'var(--green)' : 'var(--border)'}`, borderRadius: 10,
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <Text strong>{isActive ? '● ' : ''}{w.name}</Text>
                <div className="mono hint-text" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={w.path}>{w.path}</div>
              </div>
              {isActive ? <Tag color="green">当前</Tag>
                : <Button size="small" type="primary" onClick={() => switchTo(w.name)}>切换</Button>}
              <Button size="small" danger onClick={() => {
                modal.confirm({
                  title: `删除工作区「${w.name}」？`, content: '只删记录，不删磁盘目录',
                  onOk: async () => { await apiDelete(`/workspaces/${encodeURIComponent(w.name)}`); message.info('已删除'); reload(); },
                });
              }}>删除</Button>
            </div>
          );
        })}
        {!(data.workspaces || []).length && <em className="hint-text">暂无已保存的工作区。在下方添加第一个 →</em>}
        {wsActive && <Button size="small" style={{ alignSelf: 'flex-start' }} onClick={() => switchTo('')}>↩ 回到默认工作区</Button>}
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <Text strong style={{ fontSize: '.9em' }}>➕ 新增工作区</Text>
          <Space.Compact style={{ width: '100%', marginTop: 8 }}>
            <Input style={{ width: 160 }} placeholder="名称（如：博客项目）" value={name} onChange={(e) => setName(e.target.value)} />
            <Input style={{ flex: 1 }} placeholder="目录绝对路径（如 D:\projects\blog）" value={path} onChange={(e) => setPath(e.target.value)} />
            <Button type="primary" onClick={add}>添加</Button>
          </Space.Compact>
        </div>
      </Space>
    </Modal>
  );
}
