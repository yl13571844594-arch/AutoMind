// 👥 团队协作：任务分配看板 + 实时操作通知流。
import { App, Button, Card, Input, Space, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { chatSid } from '../../store/app';
import { usePanel } from '../../store/panel';
import { Badge, EmptyState, EntityCard, Tile, ViewHead } from '../ui/Panel';

const { Text, Paragraph } = Typography;
const COLS: Record<string, { label: string; icon: string; tone?: 'green' | 'yellow' }> = {
  todo: { label: '待办', icon: '📥' },
  doing: { label: '进行中', icon: '🔧', tone: 'yellow' },
  done: { label: '已完成', icon: '✅', tone: 'green' },
};

export default function TeamView() {
  const { message, modal } = App.useApp();
  const [tasks, setTasks] = useState<any[]>([]);
  const [title, setTitle] = useState('');
  const [assignee, setAssignee] = useState('');
  const feed = usePanel((s) => s.teamFeed);

  const reload = () => apiGet('/team/tasks').then((r) => setTasks(r.tasks || [])).catch(() => {});
  useEffect(() => { reload(); }, [feed.length]);

  const doneCount = tasks.filter((t) => t.status === 'done').length;
  const done = tasks.length ? Math.round((doneCount / tasks.length) * 100) : 0;

  const move = async (id: string, status: string) => {
    await apiPost(`/team/tasks/${encodeURIComponent(id)}`, { status });
    reload();
  };

  return (
    <div>
      <ViewHead icon="👥" title="团队协作"
                sub="同一服务器即同一团队：工作区、模板、专家与任务历史全员共享。" />

      {/* 顶部只放看板上看不到的聚合量 —— 三列的数量下面各自标着，
          再在这里重复一遍纯属噪音 */}
      <div className="tile-grid">
        <Tile label="📋 任务总数" value={tasks.length} foot="全部成员的分配任务" />
        <Tile label="✅ 完成率" value={done} unit="%"
              tone={tasks.length ? (done >= 80 ? 'green' : done >= 40 ? 'yellow' : 'red') : undefined}
              foot={tasks.length ? `已完成 ${doneCount} / ${tasks.length}` : '暂无任务'} />
        <Tile label="🔧 未完成" value={tasks.length - doneCount}
              foot="待办 + 进行中" />
        <Tile label="🔔 本次会话活动" value={feed.length} foot="实时通知条数" />
      </div>

      <Card size="small" style={{ marginBottom: 14 }}>
        <Text strong style={{ fontSize: '.88em' }}>➕ 分配任务</Text>
        <Space.Compact style={{ width: '100%', marginTop: 8 }}>
          <Input placeholder="任务标题（如：重构登录模块）" maxLength={120}
                 value={title} onChange={(e) => setTitle(e.target.value)} />
          <Input style={{ width: 160 }} placeholder="指派给（成员名）" maxLength={40}
                 value={assignee} onChange={(e) => setAssignee(e.target.value)} />
          <Button type="primary" onClick={async () => {
            if (!title.trim()) { message.error('任务标题必填'); return; }
            const r = await apiPost('/team/tasks', { title: title.trim(), assignee: assignee.trim(), session_id: chatSid() });
            if (r.error) { message.error(r.error); return; }
            message.success('任务已分配');
            setTitle(''); setAssignee('');
            reload();
          }}>分配</Button>
        </Space.Compact>
      </Card>

      {/* 看板三列：列内卡片自适应，标题长了在卡内换行而不撑破布局 */}
      <div className="kanban">
        {(['todo', 'doing', 'done'] as const).map((s) => {
          const list = tasks.filter((t) => t.status === s);
          return (
            <div key={s} className="kanban-col">
              <div className="kanban-head">
                <span>{COLS[s].icon} {COLS[s].label}</span>
                <span className="sec-count">{list.length}</span>
              </div>
              {list.map((t) => (
                <EntityCard
                  key={t.id}
                  state={s === 'done' ? 'ok' : undefined}
                  icon={COLS[s].icon}
                  title={t.title}
                  badges={t.assignee && <Badge tone="muted">👤 {t.assignee}</Badge>}
                  desc={t.desc || undefined}
                  meta={t.created || undefined}
                  actions={<>
                    {s !== 'todo' && <Button size="small" title="退回待办" onClick={() => move(t.id, 'todo')}>↩</Button>}
                    {s !== 'doing' && <Button size="small" title="开始处理" onClick={() => move(t.id, 'doing')}>▶</Button>}
                    {s !== 'done' && <Button size="small" type="primary" title="标记完成" onClick={() => move(t.id, 'done')}>✓</Button>}
                    <Button size="small" danger type="text" title="删除" onClick={() => modal.confirm({
                      title: '删除该团队任务？',
                      onOk: async () => { await apiDelete(`/team/tasks/${encodeURIComponent(t.id)}`); reload(); },
                    })}>✕</Button>
                  </>}
                />
              ))}
              {!list.length && <div className="kanban-empty">暂无</div>}
            </div>
          );
        })}
      </div>

      <div className="sec-title">🔔 操作通知（本次会话，实时）</div>
      <Paragraph type="secondary" style={{ fontSize: '.76em', marginTop: -2 }}>
        同事的 Agent 完成任务或改动文件会实时出现在这里，并弹出提醒。
      </Paragraph>
      {feed.length ? (
        <div className="feed">
          {feed.slice(0, 20).map((d, i) => (
            <div key={i} className="feed-row">
              <span className="mono feed-time">{d.time || ''}</span>
              <span className="feed-body">
                {d.kind === 'task_done'
                  ? <><b>{d.sid === chatSid() ? '我' : '同事'}</b> 完成任务「{d.task || ''}」
                    {d.success ? <span style={{ color: 'var(--green)' }}> ✓</span>
                      : <span style={{ color: 'var(--red)' }}> ✗</span>}
                    {d.changed_files ? ` · ${d.changed_files} 个文件改动` : ''}</>
                  : <><b>新任务</b>「{d.title || ''}」{d.assignee ? ` → ${d.assignee}` : ''}</>}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState icon="🔔" title="暂无活动"
                    hint="任一成员执行任务后，这里会实时出现记录。" />
      )}

      <Paragraph type="secondary" style={{ fontSize: '.76em', marginTop: 14 }}>
        💡 共享语义：工作区（同一目录协同）、自定义模板（专业版）、专家（分享后全员可用，企业版含审批流）
        均为服务器级存储；配合企业版 SSO/RBAC 可获得成员身份与权限控制。
      </Paragraph>
    </div>
  );
}
