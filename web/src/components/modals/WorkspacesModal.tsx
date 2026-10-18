// 🗂 工作区管理：每个工作区 = 独立目录 + 独立上下文；含版本数量限额展示。
import { App, Button, Input, Modal, Space, Tag, Typography } from 'antd';
import { useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { writeAction } from '../../lib/writeAction';
import { useAsync } from '../../lib/useAsync';
import { ErrorPanel } from '../ui/AsyncBoundary';
import DirPicker from '../ui/DirPicker';
import { useApp } from '../../store/app';
import { useChat } from '../../store/chat';
import { useUi } from '../../store/ui';

const { Text, Paragraph } = Typography;

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024 * 1024) return (n / 1073741824).toFixed(2) + ' GB';
  if (n >= 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1024).toFixed(0) + ' KB';
}

/**
 * 会话隔离副本（v1.7.7）：开启「会话独立工作目录」后每个会话一份项目副本。
 * 此前它们只在"下一次跑任务"时才按数量淘汰，不跑任务就永远占着磁盘，
 * 界面上也看不到占了多少。这里列出来，并能按会话删除 / 一键清理闲置。
 */
function SessionCopies({ open }: { open: boolean }) {
  const { message, modal } = App.useApp();
  const owner = localStorage.getItem('automind_sid') || '';
  const st = useAsync<any>(
    () => (open ? apiGet(`/session-workspaces?owner=${encodeURIComponent(owner)}`) : Promise.resolve(null)),
    [open]);
  const d = st.data;
  if (!d || (!d.enabled && !(d.copies || []).length)) return null;
  const ago = (t: number) => {
    const h = (Date.now() / 1000 - t) / 3600;
    return h < 1 ? `${Math.max(1, Math.round(h * 60))} 分钟前` : h < 48 ? `${Math.round(h)} 小时前` : `${Math.round(h / 24)} 天前`;
  };
  return (
    <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Text strong style={{ fontSize: '.9em' }}>🧩 会话隔离副本</Text>
        <span className="hint-text">共 {(d.copies || []).length} 个 · {fmtBytes(d.total_bytes || 0)}
          · 闲置 {d.ttl_hours} 小时自动清理 · 最多保留 {d.keep} 个</span>
        <span style={{ flex: 1 }} />
        <Button size="small" onClick={writeAction('清理闲置副本', async () => {
          const r = await apiPost('/session-workspaces/cleanup', {});
          message.success(r.removed?.length ? `已清理 ${r.removed.length} 个闲置副本` : '没有需要清理的副本');
          st.reload();
        })}>立即清理闲置</Button>
      </div>
      <div style={{ marginTop: 8, maxHeight: 180, overflowY: 'auto' }}>
        {(d.copies || []).length === 0 && <em className="hint-text">当前没有会话副本</em>}
        {(d.copies || []).map((c: any) => (
          <div key={c.session} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', fontSize: '.84em' }}>
            <span className="mono" style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              title={c.path}>{c.session}</span>
            <span className="hint-text">{fmtBytes(c.bytes)} · {c.files} 文件 · {ago(c.last_used)}</span>
            {c.changed > 0 && <Tag color="gold" title="副本里有尚未应用到项目的改动">{c.changed} 处改动</Tag>}
            {c.running ? <Tag color="green">运行中</Tag> : (
              <Button size="small" danger onClick={() => modal.confirm({
                title: '删除这个会话副本？',
                content: c.changed > 0 ? `副本里有 ${c.changed} 处改动尚未应用到项目，删除后将丢失。` : '副本可在下次运行时重建。',
                okText: '删除', okButtonProps: { danger: true },
                onOk: writeAction('删除副本', async () => {
                  await apiDelete(`/session-workspaces/${encodeURIComponent(c.session)}?owner=${encodeURIComponent(owner)}`);
                  st.reload();
                }),
              })}>删除</Button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

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
  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [picking, setPicking] = useState(false);

  // 打开弹窗才取数；失败时明说 —— 此前会画成"一个工作区都没有"，
  // 用户以为配置丢了，于是重新建一遍。
  const st = useAsync<any>(
    () => (open ? apiGet('/workspaces') : Promise.resolve(null)), [open]);
  const quotaSt = useAsync<any>(
    () => (open ? apiGet('/quota') : Promise.resolve(null)), [open]);
  const data = st.data || { workspaces: [], active: '' };
  const quota = quotaSt.data;
  const reload = () => { st.reload(); quotaSt.reload(); };

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
                  onOk: writeAction('删除工作区', async () => {
                    await apiDelete(`/workspaces/${encodeURIComponent(w.name)}`);
                    message.info('已删除'); reload();
                  }),
                });
              }}>删除</Button>
            </div>
          );
        })}
        {st.error && !st.loaded && <ErrorPanel error={st.error} onRetry={reload} what="工作区列表" />}
        {st.loading && !st.loaded && <em className="hint-text">正在读取工作区…</em>}
        {st.loaded && !(data.workspaces || []).length
          && <em className="hint-text">暂无已保存的工作区。在下方添加第一个 →</em>}
        {wsActive && <Button size="small" style={{ alignSelf: 'flex-start' }} onClick={() => switchTo('')}>↩ 回到默认工作区</Button>}
        <SessionCopies open={open} />
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <Text strong style={{ fontSize: '.9em' }}>➕ 新增工作区</Text>
          <Space.Compact style={{ width: '100%', marginTop: 8 }}>
            <Input style={{ width: 160 }} placeholder="名称（如：博客项目）" value={name} onChange={(e) => setName(e.target.value)} />
            <Input style={{ flex: 1 }} placeholder="目录绝对路径（如 D:\projects\blog）" value={path} onChange={(e) => setPath(e.target.value)} />
            <Button onClick={() => setPicking(!picking)}>📁 浏览</Button>
            <Button type="primary" onClick={add}>添加</Button>
          </Space.Compact>
          {picking && (
            <div style={{ marginTop: 8 }}>
              <DirPicker initial={path} pickLabel="✓ 用这个目录" onCancel={() => setPicking(false)}
                onPick={(p) => {
                  setPath(p); setPicking(false);
                  if (!name.trim()) setName(p.split(/[\\/]/).filter(Boolean).pop() || '');
                }} />
            </div>
          )}
        </div>
      </Space>
    </Modal>
  );
}
