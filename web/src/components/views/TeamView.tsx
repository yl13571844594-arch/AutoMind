// 👥 团队协作：任务分配看板 + 实时操作通知流。
import { App, Button, Card, Input, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { chatSid } from '../../store/app';
import { usePanel } from '../../store/panel';

const { Text, Paragraph } = Typography;
const COLS: Record<string, string> = { todo: '📥 待办', doing: '🔧 进行中', done: '✅ 已完成' };

export default function TeamView() {
  const { message, modal } = App.useApp();
  const [tasks, setTasks] = useState<any[]>([]);
  const [title, setTitle] = useState('');
  const [assignee, setAssignee] = useState('');
  const feed = usePanel((s) => s.teamFeed);

  const reload = () => apiGet('/team/tasks').then((r) => setTasks(r.tasks || [])).catch(() => {});
  useEffect(() => { reload(); }, [feed.length]);

  return (
    <div>
      <h3>👥 团队协作 <span className="hint-text" style={{ fontWeight: 400 }}>同一服务器 = 同一团队：工作区 / 模板 / 专家 / 任务历史全员共享</span></h3>

      <Text strong style={{ display: 'block', marginTop: 12 }}>📋 任务分配（{tasks.length}）</Text>
      <Space.Compact style={{ width: '100%', marginTop: 8 }}>
        <Input placeholder="任务标题（如：重构登录模块）" maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} />
        <Input style={{ width: 150 }} placeholder="指派给（成员名）" maxLength={40} value={assignee} onChange={(e) => setAssignee(e.target.value)} />
        <Button type="primary" onClick={async () => {
          if (!title.trim()) { message.error('任务标题必填'); return; }
          const r = await apiPost('/team/tasks', { title: title.trim(), assignee: assignee.trim(), session_id: chatSid() });
          if (r.error) { message.error(r.error); return; }
          message.success('任务已分配');
          setTitle(''); setAssignee('');
          reload();
        }}>＋ 分配任务</Button>
      </Space.Compact>

      {(['todo', 'doing', 'done'] as const).map((s) => {
        const list = tasks.filter((t) => t.status === s);
        return (
          <div key={s} style={{ marginTop: 12 }}>
            <Text strong style={{ fontSize: '.86em', color: 'var(--text2)' }}>{COLS[s]}（{list.length}）</Text>
            {list.map((t) => (
              <Card key={t.id} size="small" style={{
                marginTop: 6,
                borderColor: s === 'done' ? 'var(--green)' : s === 'doing' ? 'var(--yellow)' : undefined,
              }}>
                <Space style={{ width: '100%' }} align="center">
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Text strong>{t.title}</Text>
                    {t.assignee && <Tag style={{ marginLeft: 6, fontSize: '.68em' }}>👤 {t.assignee}</Tag>}
                    <div className="hint-text">{t.created || ''}{t.desc ? ` · ${t.desc.slice(0, 60)}` : ''}</div>
                  </div>
                  {s !== 'todo' && <Button size="small" title="退回待办" onClick={async () => { await apiPost(`/team/tasks/${encodeURIComponent(t.id)}`, { status: 'todo' }); reload(); }}>↩</Button>}
                  {s !== 'doing' && <Button size="small" title="开始" onClick={async () => { await apiPost(`/team/tasks/${encodeURIComponent(t.id)}`, { status: 'doing' }); reload(); }}>▶</Button>}
                  {s !== 'done' && <Button size="small" type="primary" title="完成" onClick={async () => { await apiPost(`/team/tasks/${encodeURIComponent(t.id)}`, { status: 'done' }); reload(); }}>✓</Button>}
                  <Button size="small" danger type="text" onClick={() => modal.confirm({
                    title: '删除该团队任务？',
                    onOk: async () => { await apiDelete(`/team/tasks/${encodeURIComponent(t.id)}`); reload(); },
                  })}>✕</Button>
                </Space>
              </Card>
            ))}
            {!list.length && <div className="hint-text" style={{ padding: '4px 2px' }}>（空）</div>}
          </div>
        );
      })}

      <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
        <Text strong style={{ fontSize: '.9em' }}>🔔 操作通知（本次会话，实时）</Text>
        <span className="hint-text" style={{ marginLeft: 6 }}>同事的 Agent 完成任务/改文件会实时出现在这里并弹提醒</span>
      </div>
      {feed.length ? feed.slice(0, 20).map((d, i) => (
        <div key={i} style={{ fontSize: '.8em', padding: '5px 0', borderBottom: '1px dashed var(--border)' }}>
          <span className="mono hint-text">{d.time || ''}</span>{' '}
          {d.kind === 'task_done'
            ? <><b>{d.sid === chatSid() ? '我' : '同事'}</b> 完成任务「{d.task || ''}」{d.success ? <span style={{ color: 'var(--green)' }}>✓</span> : <span style={{ color: 'var(--red)' }}>✗</span>}{d.changed_files ? ` · ${d.changed_files} 个文件改动` : ''}</>
            : <><b>新任务</b>「{d.title || ''}」{d.assignee ? ` → ${d.assignee}` : ''}</>}
        </div>
      )) : <div className="hint-text" style={{ marginTop: 6 }}>暂无活动 — 任一成员执行任务后这里会出现记录</div>}

      <Paragraph type="secondary" style={{ fontSize: '.78em', marginTop: 12 }}>
        💡 共享语义：工作区（同一目录协同）、自定义模板（专业版）、专家（分享后全员可用，企业版含审批流）均为服务器级存储；配合企业版 SSO/RBAC 可获得成员身份与权限控制。
      </Paragraph>
    </div>
  );
}
