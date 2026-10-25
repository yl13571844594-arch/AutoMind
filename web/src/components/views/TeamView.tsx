// 👥 团队协作工作台（v1.11.15）：
//   在线成员感知 / 任务详情（优先级·截止·标签·评论·活动）/ 按人过滤与汇总 / 实时同步。
//
// 设计取舍：
//   · 任务一次取全量（服务端上限 200 条），筛选/搜索在前端做 —— 输入时无延迟，
//     不会每敲一个字就发一次请求；后端的过滤参数仍然保留给脚本与集成用。
//   · 刷新走 WS 事件 + 500ms 防抖：一轮任务里可能连发多条 team_activity，
//     逐条重拉会把接口打爆，合并成一次即可。
//   · 统计卡始终用服务端按**全量任务**算的 summary，不随筛选跳数字。
import {
  App, AutoComplete, Button, Drawer, Input, Progress, Segmented, Select, Skeleton, Space,
  Tooltip, Typography,
} from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPost, errText } from '../../api/client';
import { writeAction } from '../../lib/writeAction';
import { useAsync } from '../../lib/useAsync';
import { isMySid, workspaceSid } from '../../store/app';
import { usePanel, type TeamMember, type TeamSummary } from '../../store/panel';
import { ErrorPanel } from '../ui/AsyncBoundary';
import { EmptyState, Tile, ViewHead } from '../ui/Panel';
import './TeamView.css';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

const NAME_KEY = 'automind_member_name';
const DAY_MS = 86400000;

// ── 数据模型（与 automind/core/team.py 的字段一一对应）──────────
interface TeamComment { id: string; author: string; text: string; time: string }
interface TeamActivity { time: string; kind: string; actor: string; detail: string }
interface TeamTask {
  id: string; title: string; desc: string;
  status: 'todo' | 'doing' | 'blocked' | 'done' | string;
  priority: 'low' | 'normal' | 'high' | 'urgent' | string;
  assignee: string; creator: string; due: string; tags: string[];
  comments: TeamComment[]; activity: TeamActivity[];
  created: string; updated: string;
}
interface TasksPayload { tasks: TeamTask[]; summary: TeamSummary }

interface Draft {
  title: string; desc: string; assignee: string; priority: string; due: string; tags: string;
}

// ── 常量 / 小工具 ─────────────────────────────────────────
const STATUS_COLS = [
  { key: 'todo', label: '待办', icon: '📥' },
  { key: 'doing', label: '进行中', icon: '🔧' },
  { key: 'blocked', label: '阻塞', icon: '⛔' },
  { key: 'done', label: '已完成', icon: '✅' },
] as const;

const PRIORITIES = ['low', 'normal', 'high', 'urgent'] as const;
const PRIORITY_META: Record<string, { label: string; short: string }> = {
  urgent: { label: '紧急', short: '急' },
  high: { label: '高', short: '高' },
  normal: { label: '普通', short: '中' },
  low: { label: '低', short: '低' },
};

const STATUS_LABEL: Record<string, string> = {
  todo: '待办', doing: '进行中', blocked: '阻塞', done: '已完成',
};

const ACTIVITY_ICON: Record<string, string> = {
  created: '📥', status: '↔️', done: '✅', assign: '👤', claim: '🙋',
  priority: '🎚️', due: '📅', edit: '✏️', update: '✏️',
};

const EMPTY_DRAFT: Draft = { title: '', desc: '', assignee: '', priority: 'normal', due: '', tags: '' };

/** 本地日期 YYYY-MM-DD（不能用 toISOString：那是 UTC，跨时区会差一天）。 */
function localToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** 截止日期徽标：逾期时返回"已逾期 N 天"并标红。 */
function dueInfo(due: string, status: string): { text: string; overdue: boolean } | null {
  if (!due) return null;
  if (status === 'done') return { text: `📅 ${due.slice(5)}`, overdue: false };
  const today = localToday();
  if (due < today) {
    const days = Math.max(1, Math.round((Date.parse(today) - Date.parse(due)) / DAY_MS));
    return { text: `⏰ 已逾期 ${days} 天`, overdue: true };
  }
  return { text: due === today ? '📅 今天截止' : `📅 ${due.slice(5)}`, overdue: false };
}

function initial(name: string): string {
  const s = (name || '?').trim();
  return s ? s.slice(0, 1).toUpperCase() : '?';
}

function defaultMemberName(sid: string): string {
  return `成员-${(sid || '').slice(-4) || '0000'}`;
}

function splitTags(raw: string): string[] {
  return raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean).slice(0, 20);
}

function tasksOf(tasks: TeamTask[], status: string): TeamTask[] {
  return tasks.filter((t) => t.status === status);
}

// ── 任务卡 ────────────────────────────────────────────────
function TaskCard({ task, load, onOpen }: {
  task: TeamTask;
  load: { total: number; doing: number; overdue: number } | undefined;
  onOpen: () => void;
}) {
  const due = dueInfo(task.due, task.status);
  const prio = PRIORITY_META[task.priority] ? task.priority : 'normal';
  return (
    <div className={`tv-card pri-${prio}`} onClick={onOpen} role="button" tabIndex={0}
         onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onOpen(); }}>
      <div className="tv-card-title">{task.title || '（无标题）'}</div>
      <div className="tv-card-meta">
        <span className="tv-card-who">
          {task.assignee
            ? <><span className="tv-avatar sm">{initial(task.assignee)}</span>
              <span className="tv-card-name">{task.assignee}</span>
              {load && load.total > 0 && <span className="tv-card-load">{load.total}</span>}</>
            : <span className="tv-unassigned">未分配</span>}
        </span>
        {due && <span className={`tv-due${due.overdue ? ' overdue' : ''}`}>{due.text}</span>}
      </div>
      <div className="tv-card-foot">
        <span className={`tv-prio tv-prio-${prio}`}>{PRIORITY_META[prio].short}</span>
        {!!(task.comments || []).length && (
          <span className="tv-card-stat">💬 {task.comments.length}</span>
        )}
        {!!(task.tags || []).length && (
          <span className="tv-card-tags">
            {task.tags.slice(0, 2).map((tag) => <span key={tag} className="tv-tag">{tag}</span>)}
            {task.tags.length > 2 && <span className="tv-tag">+{task.tags.length - 2}</span>}
          </span>
        )}
      </div>
    </div>
  );
}

// ── 主视图 ────────────────────────────────────────────────
export default function TeamView() {
  const { message, modal } = App.useApp();
  const st = useAsync<TasksPayload>(
    () => apiGet('/team/tasks').then((r) => ({
      tasks: Array.isArray(r?.tasks) ? r.tasks : [],
      summary: (r?.summary || {}) as TeamSummary,
    })), []);
  const tasks = st.data?.tasks || [];
  const summary = st.data?.summary || {};
  const reload = st.reload;
  const loaded = st.loaded;

  // 实时数据来自 panel（ws.ts 把 team_activity / team_presence 落进 store）
  const feed = usePanel((s) => s.teamFeed);
  const teamRev = usePanel((s) => s.teamRev);
  const members = usePanel((s) => s.teamMembers);

  const [myName, setMyName] = useState<string>(() => {
    try { return localStorage.getItem(NAME_KEY) || ''; } catch { return ''; }
  });
  const displayName = myName.trim() || defaultMemberName(workspaceSid());

  const [q, setQ] = useState('');
  const [assigneeFilter, setAssigneeFilter] = useState('all');
  const [priorityFilter, setPriorityFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [dueSort, setDueSort] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [form, setForm] = useState<Draft>(EMPTY_DRAFT);

  const [detailId, setDetailId] = useState('');
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [commentText, setCommentText] = useState('');

  // 成员条：WS 事件没到过（刚打开页面）时补拉一次 HTTP 快照
  useEffect(() => {
    if (usePanel.getState().teamMembers.length) return;
    let alive = true;
    apiGet('/team/members').then((r) => {
      if (alive && Array.isArray(r?.members)) usePanel.getState().setTeamPresence(r.members);
    }).catch(() => { /* 成员条拉不到不影响看板 */ });
    return () => { alive = false; };
  }, []);

  // 看板刷新：team_activity 到达 → 合并成一次 500ms 后的重拉
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastRev = useRef(teamRev);
  useEffect(() => {
    if (teamRev === lastRev.current) return;
    lastRev.current = teamRev;
    if (timerRef.current) return;              // 已在排队：同批事件共用一次刷新
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      reloadRef.current();
    }, 500);
  }, [teamRev]);
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  // 把最近一次 summary 同步进全局 store（其它面板/后续扩展可直接读）
  useEffect(() => {
    if (st.data?.summary) usePanel.getState().setTeamSummary(st.data.summary);
  }, [st.data]);

  const memberLoad = useMemo(() => {
    const map: Record<string, { total: number; doing: number; overdue: number }> = {};
    const today = localToday();
    for (const t of tasks) {
      const name = t.assignee || '';
      if (!name) continue;
      const row = map[name] || { total: 0, doing: 0, overdue: 0 };
      row.total += 1;
      if (t.status === 'doing') row.doing += 1;
      if (t.due && t.status !== 'done' && t.due < today) row.overdue += 1;
      map[name] = row;
    }
    return map;
  }, [tasks]);

  const onlineMembers = useMemo(() => members.filter((m) => m.online), [members]);
  const assigneeOptions = useMemo(() => {
    const names = new Set<string>();
    for (const m of members) if (m.name) names.add(m.name);
    for (const t of tasks) if (t.assignee) names.add(t.assignee);
    return [...names].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  }, [members, tasks]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    let rows = tasks.filter((t) => {
      if (statusFilter !== 'all' && t.status !== statusFilter) return false;
      if (priorityFilter !== 'all' && t.priority !== priorityFilter) return false;
      if (assigneeFilter === '__me__') {
        if (t.assignee !== displayName) return false;
      } else if (assigneeFilter !== 'all' && t.assignee !== assigneeFilter) {
        return false;
      }
      if (kw) {
        const hay = [t.title, t.desc, t.assignee, ...(t.tags || [])].join(' ').toLowerCase();
        if (!hay.includes(kw)) return false;
      }
      return true;
    });
    if (dueSort) {
      rows = rows.slice().sort((a, b) => (a.due || '9999-12-31').localeCompare(b.due || '9999-12-31'));
    }
    return rows;
  }, [tasks, q, statusFilter, priorityFilter, assigneeFilter, dueSort, displayName]);

  const detailTask = tasks.find((t) => t.id === detailId) || null;

  // ── 取名：写 localStorage + 同步服务端（供负责人 / 评论 / 认领署名）──
  const saveName = async () => {
    const sid = workspaceSid();
    const name = myName.trim() || defaultMemberName(sid);
    try {
      const r = await apiPost('/team/members/name', { sid, name });
      try { localStorage.setItem(NAME_KEY, name); } catch { /* 隐私模式忽略 */ }
      setMyName(name);
      if (Array.isArray(r?.members)) usePanel.getState().setTeamPresence(r.members);
      message.success(`昵称已保存：${name}`);
    } catch (e) {
      message.error(`昵称保存失败：${errText(e)}`);
    }
  };

  // ── 新建任务 ──
  const openCreate = () => {
    setForm({ ...EMPTY_DRAFT, assignee: assigneeFilter !== 'all' && assigneeFilter !== '__me__' ? assigneeFilter : '' });
    setCreateOpen(true);
  };
  const submitCreate = async () => {
    if (!form.title.trim()) { message.error('任务标题必填'); return; }
    try {
      await apiPost('/team/tasks', {
        title: form.title.trim(), desc: form.desc, assignee: form.assignee.trim(),
        priority: form.priority, due: form.due, tags: splitTags(form.tags),
        creator: displayName, session_id: workspaceSid(),
      });
      setCreateOpen(false);
      message.success('任务已创建');
      reload();
    } catch (e) {
      message.error(`创建失败：${errText(e)}`);
    }
  };

  // ── 详情抽屉 ──
  const openDetail = (t: TeamTask) => {
    setDetailId(t.id);
    setDraft({
      title: t.title || '', desc: t.desc || '', assignee: t.assignee || '',
      priority: t.priority || 'normal', due: t.due || '', tags: (t.tags || []).join(', '),
    });
    setCommentText('');
  };

  const moveTask = async (t: TeamTask, status: string) => {
    try {
      await apiPost(`/team/tasks/${encodeURIComponent(t.id)}`, {
        status, actor: displayName, session_id: workspaceSid(),
      });
      message.success(`已移到「${STATUS_LABEL[status] || status}」`);
      reload();
    } catch (e) {
      message.error(`更新失败：${errText(e)}`);
    }
  };

  const claimTask = async (t: TeamTask) => {
    try {
      await apiPost(`/team/tasks/${encodeURIComponent(t.id)}/claim`, {
        member: displayName, session_id: workspaceSid(),
      });
      setDraft((d) => ({ ...d, assignee: displayName }));
      message.success('已认领，任务进入进行中');
      reload();
    } catch (e) {
      message.error(`认领失败：${errText(e)}`);
    }
  };

  const saveDetail = async (t: TeamTask) => {
    if (!draft.title.trim()) { message.error('任务标题必填'); return; }
    try {
      await apiPost(`/team/tasks/${encodeURIComponent(t.id)}`, {
        title: draft.title.trim(), desc: draft.desc, assignee: draft.assignee.trim(),
        priority: draft.priority, due: draft.due, tags: splitTags(draft.tags),
        actor: displayName, session_id: workspaceSid(),
      });
      message.success('任务已保存');
      reload();
    } catch (e) {
      message.error(`保存失败：${errText(e)}`);
    }
  };

  const sendComment = async (t: TeamTask) => {
    const text = commentText.trim();
    if (!text) { message.error('评论内容不能为空'); return; }
    try {
      await apiPost(`/team/tasks/${encodeURIComponent(t.id)}/comments`, {
        text, author: displayName, session_id: workspaceSid(),
      });
      setCommentText('');
      reload();
    } catch (e) {
      message.error(`评论发送失败：${errText(e)}`);
    }
  };

  const deleteTask = (t: TeamTask) => modal.confirm({
    title: '删除该团队任务？',
    content: '不可撤销：评论与活动记录会一并删除。',
    okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: writeAction('删除团队任务', async () => {
      await apiDelete(`/team/tasks/${encodeURIComponent(t.id)}`);
      setDetailId('');
      message.success('任务已删除');
      reload();
    }),
  });

  const done = summary.by_status?.done ?? tasksOf(tasks, 'done').length;
  const total = summary.total ?? tasks.length;
  const rate = Math.round(summary.completion_rate ?? (total ? (done / total) * 100 : 0));

  const feedText = (d: any) => {
    const actor = d.actor || (d.sid && isMySid(d.sid) ? '我' : '同事');
    const title = d.title || d.task?.title || '';
    switch (d.kind) {
      case 'task_done':
        return <>
          <b>{isMySid(d.sid) ? '我' : '同事'}</b> 完成任务「{d.task || ''}」
          {d.success ? <span style={{ color: 'var(--green)' }}> ✓</span>
            : <span style={{ color: 'var(--red)' }}> ✗</span>}
          {d.changed_files ? ` · ${d.changed_files} 个文件改动` : ''}
        </>;
      case 'task_created':
        return <><b>{actor}</b> 新建任务「{title}」{d.assignee ? ` → ${d.assignee}` : ''}</>;
      case 'task_moved':
        return <><b>{actor}</b> 移动任务「{title}」{d.detail ? `（${d.detail}）` : ''}</>;
      case 'task_updated':
        return <><b>{actor}</b> 更新任务「{title}」{d.detail ? `：${d.detail}` : ''}</>;
      case 'task_commented':
        return <><b>{actor}</b> 评论了「{title}」{d.detail ? `：${d.detail}` : ''}</>;
      case 'task_claimed':
        return <><b>{actor}</b> 认领了「{title}」</>;
      case 'task_deleted':
        return <><b>{actor}</b> 删除任务「{title}」</>;
      case 'task_assigned':   // 旧后端事件：保留渲染，避免升级期通知变成空白
        return <><b>{actor}</b> 分配任务「{title}」{d.assignee ? ` → ${d.assignee}` : ''}</>;
      default:
        return <><b>{actor}</b> 团队任务更新「{title}」</>;
    }
  };

  return (
    <div className="tv-root">
      <ViewHead
        icon="👥"
        title="团队协作"
        sub="同一服务器即同一团队：在线成员实时可见，任务、评论与进度全员同步；社区版开箱可用。"
        extra={<Button type="primary" onClick={openCreate}>＋ 新建任务</Button>}
      />

      {/* ── 在线成员 + 我的昵称 ── */}
      <div className="tv-hero">
        <div className="tv-name-row">
          <span className="tv-name-label">🙋 我的昵称</span>
          <Input
            size="small"
            style={{ width: 170 }}
            maxLength={40}
            value={myName}
            placeholder={defaultMemberName(workspaceSid())}
            onChange={(e) => setMyName(e.target.value)}
            onPressEnter={saveName}
          />
          <Button size="small" onClick={saveName}>保存昵称</Button>
          <span className="tv-name-hint">用于负责人 / 评论 / 认领署名，保存在本地并同步到服务器。</span>
        </div>
        <div className="tv-presence">
          <div className="tv-presence-head">
            <span className="tv-dot-inline">🟢</span>
            <span>在线成员</span>
            <span className="tv-count">{onlineMembers.length}</span>
          </div>
          <div className="tv-members">
            {onlineMembers.map((m: TeamMember) => {
              const load = memberLoad[m.name];
              const n = load?.total ?? m.total ?? 0;
              const mine = isMySid(m.sid);
              return (
                <Tooltip
                  key={m.sid || m.name}
                  title={`${m.name}${mine ? '（我）' : ''} · sid …${(m.sid || '').slice(-4) || '—'}`
                    + `${(m.tabs || 1) > 1 ? ` · ${m.tabs} 个页面` : ''} · 任务 ${n}`}
                >
                  <span className={`tv-member${mine ? ' me' : ''}`}>
                    <span className="tv-avatar">
                      {initial(m.name)}
                      <i className={`tv-online-dot${m.online ? ' on' : ''}`} />
                    </span>
                    <span className="tv-member-name">{m.name}</span>
                    {n > 0 && <span className="tv-member-badge">{n}</span>}
                  </span>
                </Tooltip>
              );
            })}
            {!onlineMembers.length && (
              <span className="tv-members-empty">
                暂无其他在线成员 —— 再开一个浏览器标签页，或让同事访问本机地址，即可看到在线状态与实时协作。
              </span>
            )}
          </div>
        </div>
      </div>

      {/* ── 统计卡（按全量任务，不随筛选变化）── */}
      <div className="tile-grid">
        <Tile label="📋 任务总数" value={total}
              foot={`${summary.by_status?.todo ?? 0} 待办 · ${summary.by_status?.blocked ?? 0} 阻塞`} />
        <Tile label="🔧 进行中" value={summary.by_status?.doing ?? 0} tone="yellow"
              foot={`${(summary.by_assignee || []).filter((r) => r.assignee).length} 位成员有任务`} />
        <Tile label="⏰ 已逾期" value={summary.overdue ?? 0}
              tone={(summary.overdue ?? 0) > 0 ? 'red' : undefined}
              foot="未完成且已过截止日期" />
        <Tile label="✅ 今日完成" value={summary.done_today ?? 0} tone="green"
              foot={`近 7 天完成 ${summary.done_7d ?? 0} 个`} />
        <Tile label="🎯 完成率" value={rate} unit="%"
              tone={rate >= 80 ? 'green' : rate >= 40 ? 'yellow' : 'red'}
              foot={<Progress percent={rate} size="small" showInfo={false}
                              strokeColor={{ from: '#7b9fff', to: '#3ce68a' }} />} />
      </div>

      {/* ── 工具栏 ── */}
      <div className="tv-toolbar">
        <Input.Search
          allowClear placeholder="搜索标题 / 描述 / 标签 / 负责人…" value={q}
          onChange={(e) => setQ(e.target.value)} style={{ width: 240 }}
        />
        <Select
          value={assigneeFilter} onChange={(v) => setAssigneeFilter(String(v))}
          style={{ width: 160 }}
          options={[
            { value: 'all', label: '全部负责人' },
            { value: '__me__', label: `👤 只看我的（${displayName}）` },
            ...assigneeOptions.map((n) => ({ value: n, label: n })),
          ]}
        />
        <Segmented
          size="small" value={priorityFilter} onChange={(v) => setPriorityFilter(String(v))}
          options={[
            { label: '全部优先级', value: 'all' },
            ...PRIORITIES.map((p) => ({ label: PRIORITY_META[p].label, value: p })),
          ]}
        />
        <Segmented
          size="small" value={statusFilter} onChange={(v) => setStatusFilter(String(v))}
          options={[
            { label: '全部状态', value: 'all' },
            ...STATUS_COLS.map((c) => ({ label: `${c.icon}${c.label}`, value: c.key })),
          ]}
        />
        <Button type={dueSort ? 'primary' : 'default'} onClick={() => setDueSort((v) => !v)}>
          📅 按截止日期{dueSort ? '（升序）' : ''}
        </Button>
      </div>

      {/* ── 三态：加载中 ≠ 没有任务 ≠ 加载失败 ── */}
      {st.loading && !loaded && <div className="tv-loading"><Skeleton active paragraph={{ rows: 4 }} /></div>}
      {!st.loading && st.error && !loaded && (
        <ErrorPanel error={st.error} onRetry={reload} what="团队任务" />
      )}
      {loaded && !!st.error && (
        <div className="load-stale">
          ⚠️ 刷新失败（{errText(st.error)}），下方为上一次的数据。
          <Button size="small" type="link" onClick={reload}>重试</Button>
        </div>
      )}
      {loaded && !tasks.length && (
        <EmptyState icon="👥" title="团队还没有任务"
                    hint="点右上角「＋ 新建任务」创建第一个任务；同一服务器上的成员会实时看到它，并可评论、认领、移动状态。" />
      )}
      {loaded && !!tasks.length && !filtered.length && (
        <div className="tv-filter-empty">没有符合筛选条件的任务 —— 试试放宽搜索，或把负责人 / 优先级 / 状态切回「全部」。</div>
      )}

      {/* ── 四列看板 ── */}
      {!!tasks.length && (
        <div className="tv-board">
          {STATUS_COLS.map((col) => {
            const list = filtered.filter((t) => t.status === col.key);
            return (
              <div key={col.key} className={`tv-col tv-col-${col.key}`}>
                <div className="tv-col-head">
                  <span>{col.icon} {col.label}</span>
                  <span className="tv-count">{list.length}</span>
                </div>
                <div className="tv-col-body">
                  {list.map((t) => (
                    <TaskCard key={t.id} task={t} load={memberLoad[t.assignee]}
                              onOpen={() => openDetail(t)} />
                  ))}
                  {!list.length && (
                    <div className="tv-col-empty">
                      {statusFilter !== 'all' && statusFilter !== col.key ? '已按状态筛选' : '暂无任务'}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── 会话活动流（保留原有能力，文案覆盖新事件类型）── */}
      <div className="sec-title">🔔 会话活动通知（实时）</div>
      <Paragraph type="secondary" style={{ fontSize: '.76em', marginTop: -2 }}>
        同事完成任务、创建 / 评论 / 认领任务时实时出现在这里；活动也会记进任务的详情时间线。
      </Paragraph>
      {feed.length ? (
        <div className="feed tv-feed">
          {feed.slice(0, 20).map((d, i) => (
            <div key={`${d.time}-${d.task_id || d.task || ''}-${i}`} className="feed-row">
              <span className="mono feed-time">{d.time || ''}</span>
              <span className="feed-body">{feedText(d)}</span>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState icon="🔔" title="暂无活动"
                    hint="任一成员新建 / 移动 / 评论任务后，这里会实时出现记录。" />
      )}

      {/* ── 新建任务弹窗 ── */}
      <Drawer
        title="＋ 新建团队任务"
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        width={480}
        extra={<Button type="primary" onClick={submitCreate}>创建</Button>}
      >
        <div className="tv-form">
          <label>标题 *</label>
          <Input maxLength={120} value={form.title} placeholder="如：重构登录模块"
                 onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <label>描述</label>
          <TextArea rows={3} maxLength={1000} value={form.desc} placeholder="补充背景、验收标准…"
                    onChange={(e) => setForm({ ...form, desc: e.target.value })} />
          <div className="tv-form-row">
            <div>
              <label>负责人</label>
              <AutoComplete
                style={{ width: '100%' }} value={form.assignee} maxLength={40}
                placeholder="成员名（可留空）"
                options={assigneeOptions.map((n) => ({ value: n }))}
                onChange={(v) => setForm({ ...form, assignee: String(v) })}
                filterOption={(input, option) =>
                  String(option?.value || '').toLowerCase().includes(input.toLowerCase())}
              />
            </div>
            <div>
              <label>优先级</label>
              <Select
                style={{ width: '100%' }} value={form.priority}
                onChange={(v) => setForm({ ...form, priority: String(v) })}
                options={PRIORITIES.map((p) => ({ value: p, label: PRIORITY_META[p].label }))}
              />
            </div>
          </div>
          <div className="tv-form-row">
            <div>
              <label>截止日期</label>
              <Input type="date" value={form.due}
                     onChange={(e) => setForm({ ...form, due: e.target.value })} />
            </div>
            <div>
              <label>标签（逗号分隔）</label>
              <Input value={form.tags} placeholder="后端, 安全"
                     onChange={(e) => setForm({ ...form, tags: e.target.value })} />
            </div>
          </div>
        </div>
      </Drawer>

      {/* ── 任务详情抽屉 ── */}
      <Drawer
        title={detailTask ? `${STATUS_COLS.find((c) => c.key === detailTask.status)?.icon || '📋'} 任务详情` : '任务详情'}
        open={!!detailId}
        onClose={() => setDetailId('')}
        width={540}
      >
        {!detailTask ? (
          <EmptyState icon="🗑" title="任务不存在或已被删除"
                      hint="它可能刚被同事删除。关闭抽屉后看板会自动刷新。" />
        ) : (
          <div className="tv-detail">
            <div className="tv-detail-status">
              {STATUS_COLS.map((c) => (
                <Button
                  key={c.key}
                  size="small"
                  type={detailTask.status === c.key ? 'primary' : 'default'}
                  disabled={detailTask.status === c.key}
                  onClick={() => moveTask(detailTask, c.key)}
                >{c.icon} {c.label}</Button>
              ))}
              <Button size="small" type="dashed" onClick={() => claimTask(detailTask)}>
                🙋 认领给我
              </Button>
            </div>

            <div className="tv-form">
              <label>标题</label>
              <Input maxLength={120} value={draft.title}
                     onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
              <label>描述</label>
              <TextArea rows={3} maxLength={1000} value={draft.desc}
                        onChange={(e) => setDraft({ ...draft, desc: e.target.value })} />
              <div className="tv-form-row">
                <div>
                  <label>负责人</label>
                  <AutoComplete
                    style={{ width: '100%' }} value={draft.assignee} maxLength={40}
                    options={assigneeOptions.map((n) => ({ value: n }))}
                    onChange={(v) => setDraft({ ...draft, assignee: String(v) })}
                    filterOption={(input, option) =>
                      String(option?.value || '').toLowerCase().includes(input.toLowerCase())}
                  />
                </div>
                <div>
                  <label>优先级</label>
                  <Select
                    style={{ width: '100%' }} value={draft.priority}
                    onChange={(v) => setDraft({ ...draft, priority: String(v) })}
                    options={PRIORITIES.map((p) => ({ value: p, label: PRIORITY_META[p].label }))}
                  />
                </div>
              </div>
              <div className="tv-form-row">
                <div>
                  <label>截止日期</label>
                  <Input type="date" value={draft.due}
                         onChange={(e) => setDraft({ ...draft, due: e.target.value })} />
                </div>
                <div>
                  <label>标签（逗号分隔）</label>
                  <Input value={draft.tags}
                         onChange={(e) => setDraft({ ...draft, tags: e.target.value })} />
                </div>
              </div>
              <Space style={{ marginTop: 4 }}>
                <Button type="primary" onClick={() => saveDetail(detailTask)}>💾 保存修改</Button>
                <Button danger onClick={() => deleteTask(detailTask)}>🗑 删除任务</Button>
              </Space>
            </div>

            <div className="tv-detail-meta">
              <Text type="secondary" style={{ fontSize: '.76em' }}>
                创建人 {detailTask.creator || '—'} · 创建于 {detailTask.created || '—'}
                {detailTask.updated ? ` · 更新于 ${detailTask.updated}` : ''}
              </Text>
            </div>

            <div className="sec-title">💬 评论 <span className="sec-count">{detailTask.comments.length}</span></div>
            <div className="tv-comments">
              {detailTask.comments.length
                ? detailTask.comments.map((c) => (
                  <div key={c.id} className="tv-comment">
                    <span className="tv-avatar sm">{initial(c.author)}</span>
                    <div className="tv-comment-body">
                      <div className="tv-comment-head">
                        <b>{c.author || '匿名'}</b>
                        <span className="mono">{c.time || ''}</span>
                      </div>
                      <div className="tv-comment-text">{c.text}</div>
                    </div>
                  </div>
                ))
                : <div className="tv-muted">还没有评论 —— 写下第一条，所有成员实时可见。</div>}
            </div>
            <Space.Compact style={{ width: '100%', marginTop: 8 }}>
              <Input value={commentText} maxLength={1000} placeholder="写评论，Enter 发送…"
                     onChange={(e) => setCommentText(e.target.value)}
                     onPressEnter={() => sendComment(detailTask)} />
              <Button type="primary" onClick={() => sendComment(detailTask)}>发送</Button>
            </Space.Compact>

            <div className="sec-title">🕘 活动时间线 <span className="sec-count">{detailTask.activity.length}</span></div>
            <div className="tv-timeline">
              {[...detailTask.activity].reverse().slice(0, 20).map((a, i) => (
                <div key={`${a.time}-${i}`} className="tv-tl-row">
                  <span className="tv-tl-ico">{ACTIVITY_ICON[a.kind] || '•'}</span>
                  <span className="tv-tl-body">
                    <b>{a.actor || '系统'}</b> {a.detail || a.kind}
                  </span>
                  <span className="mono tv-tl-time">{(a.time || '').slice(5, 16)}</span>
                </div>
              ))}
              {!detailTask.activity.length && <div className="tv-muted">暂无活动记录。</div>}
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
