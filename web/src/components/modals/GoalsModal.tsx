// 🎯 目标交付模式（v1.11.0）：设置 / 查看长期任务目标。
//
// 一个目标 = 很多轮任务。这里新建目标（标题 + 期望交付物 + 验收标准），挂到输入框上
// 之后每轮任务都会带着目标与最近进度去推进；任务结束时服务端把这一轮记进目标，
// 详情里就是"这个目标推进到哪了"的时间线。
import { App, Button, Empty, Form, Input, Modal, Segmented, Space, Tag } from 'antd';
import Popconfirm from '../ui/SafePopconfirm';
import { useEffect, useState } from 'react';
import { errText } from '../../api/client';
import { GOAL_STATUS_ZH, useRefs, type Goal, type GoalStatus } from '../../store/refs';
import { useUi } from '../../store/ui';

const STATUS_COLOR: Record<GoalStatus, string> = {
  active: 'blue', paused: 'default', done: 'green', archived: 'default',
};
const RUN_MARK: Record<string, string> = { ok: '✓', failed: '✗', cancelled: '⏹', interrupted: '⚠', error: '⚠' };

const fmtTime = (t: number) => new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
const fmtTok = (n: number) => (n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n || 0));

export default function GoalsModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal === 'goals');
  const close = useUi((s) => s.closeModal);
  const goals = useRefs((s) => s.goals);
  const current = useRefs((s) => s.goal);
  const [filter, setFilter] = useState<'open' | 'all'>('open');
  const [detail, setDetail] = useState<Goal | null>(null);
  const [editing, setEditing] = useState<Goal | 'new' | null>(null);
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    if (!open) return;
    setDetail(null); setEditing(null);
    useRefs.getState().loadGoals().catch((e) => message.error(`目标加载失败：${errText(e)}`));
  }, [open]);

  useEffect(() => {
    if (editing === 'new') form.setFieldsValue({ title: '', deliverable: '', criteria: '', notes: '' });
    else if (editing) form.setFieldsValue(editing);
  }, [editing]);

  const showDetail = async (g: Goal) => {
    try { setDetail(await useRefs.getState().getGoal(g.id)); } catch (e) { message.error(errText(e)); }
  };

  const save = async () => {
    const v = await form.validateFields();
    setBusy(true);
    try {
      const st = useRefs.getState();
      if (editing === 'new') {
        const g = await st.createGoal(v);
        st.setGoal({ id: g.id, title: g.title });
        message.success(`目标「${g.title}」已创建，并挂到输入框上 —— 之后的消息都会围绕它推进`);
      } else if (editing) {
        const g = await st.updateGoal(editing.id, v);
        if (detail?.id === g.id) setDetail(await st.getGoal(g.id));
        message.success('目标已更新');
      }
      setEditing(null);
    } catch (e) {
      message.error(errText(e));
    } finally { setBusy(false); }
  };

  const setStatus = async (g: Goal, status: GoalStatus) => {
    try {
      const st = useRefs.getState();
      const ng = await st.updateGoal(g.id, { status });
      if (status === 'archived' && st.goal?.id === g.id) st.setGoal(null);
      if (detail?.id === g.id) setDetail(await st.getGoal(ng.id));
    } catch (e) { message.error(errText(e)); }
  };

  const attach = (g: Goal) => {
    useRefs.getState().setGoal({ id: g.id, title: g.title });
    message.success(`已挂上目标「${g.title}」：之后发送的每条消息都会围绕它推进（点输入框上的 ✕ 摘掉）`);
    close();
  };

  const list = (goals || []).filter((g) => filter === 'all' || (g.status !== 'done' && g.status !== 'archived'));

  const renderForm = () => (
    <Form form={form} layout="vertical" requiredMark={false}>
      <Form.Item name="title" label="目标" rules={[{ required: true, message: '写一句话说清楚要达成什么' }]}>
        <Input maxLength={120} placeholder="例如：把 automind 的测试覆盖率提升到 80%" />
      </Form.Item>
      <Form.Item name="deliverable" label="期望交付物" extra="交付的是什么：文件、报告、可运行的功能…（写清楚了每轮推进才有方向）">
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} maxLength={2000}
          placeholder="例如：覆盖率报告 + 新增的测试用例（tests/ 下），CI 全绿" />
      </Form.Item>
      <Form.Item name="criteria" label="验收标准（可选）" extra="满足这些才算达成；模型声明达成时也只是「建议完成」，由你确认">
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} maxLength={2000}
          placeholder="例如：pytest --cov 报告总覆盖率 ≥ 80%，且没有被跳过的用例" />
      </Form.Item>
      <Form.Item name="notes" label="备注（可选）" extra="会一并告诉模型：约束、偏好、不要碰的地方…">
        <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} maxLength={2000} />
      </Form.Item>
    </Form>
  );

  const renderDetail = (g: Goal) => (
    <div className="goal-detail">
      <div className="goal-head">
        <Tag color={STATUS_COLOR[g.status]}>{GOAL_STATUS_ZH[g.status]}</Tag>
        <b>{g.title}</b>
      </div>
      {g.suggest_done && g.status !== 'done' && (
        <div className="goal-suggest">
          🤖 最近一轮模型声明「目标已达成」—— 请核对交付物后确认
          <Button size="small" type="primary" onClick={() => setStatus(g, 'done')}>确认已完成</Button>
        </div>
      )}
      {g.deliverable && <p><span className="hint-text">期望交付物：</span>{g.deliverable}</p>}
      {g.criteria && <p><span className="hint-text">验收标准：</span>{g.criteria}</p>}
      {g.notes && <p><span className="hint-text">备注：</span>{g.notes}</p>}
      <p><span className="hint-text">当前进度：</span>{g.progress || '（还没有推进记录）'}</p>
      <p className="hint-text">
        已推进 {g.run_count} 轮（成功 {g.success_count}）· 累计 {fmtTok(g.tokens)} token · 创建于 {fmtTime(g.created)}
      </p>
      <div className="goal-runs">
        {(g.runs || []).slice().reverse().map((r, i) => (
          <div key={i} className={'goal-run ' + (r.success ? 'ok' : 'fail')}>
            <div>
              <span className="goal-run-mark">{RUN_MARK[r.status] || '·'}</span>
              <b>{r.task}</b>
              <span className="hint-text"> · {fmtTime(r.at)}{r.tokens ? ` · ${fmtTok(r.tokens)} token` : ''}</span>
            </div>
            {r.progress && <div className="goal-run-progress">目标进度：{r.progress}</div>}
            {!r.progress && r.summary && <div className="hint-text goal-run-summary">{r.summary}</div>}
          </div>
        ))}
        {!(g.runs || []).length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="挂上这个目标后发送消息，每一轮的推进都会记在这里" />}
      </div>
    </div>
  );

  const footer = editing ? (
    <Space>
      <Button onClick={() => setEditing(null)}>取消</Button>
      <Button type="primary" loading={busy} onClick={save}>{editing === 'new' ? '创建并挂到输入框' : '保存'}</Button>
    </Space>
  ) : detail ? (
    <Space wrap>
      <Button onClick={() => setDetail(null)}>← 返回列表</Button>
      <Button onClick={() => setEditing(detail)}>✎ 编辑</Button>
      {detail.status === 'active' && <Button onClick={() => setStatus(detail, 'paused')}>暂停</Button>}
      {(detail.status === 'paused' || detail.status === 'done' || detail.status === 'archived') && (
        <Button onClick={() => setStatus(detail, 'active')}>重新激活</Button>
      )}
      {detail.status !== 'done' && <Button onClick={() => setStatus(detail, 'done')}>标记完成</Button>}
      {detail.status !== 'archived' && <Button onClick={() => setStatus(detail, 'archived')}>归档</Button>}
      {detail.status !== 'archived' && (
        <Button type="primary" disabled={current?.id === detail.id} onClick={() => attach(detail)}>
          {current?.id === detail.id ? '已挂在输入框上' : '🎯 围绕它继续推进'}
        </Button>
      )}
    </Space>
  ) : (
    <Space>
      <Button onClick={close}>关闭</Button>
      <Button type="primary" onClick={() => setEditing('new')}>＋ 新建目标</Button>
    </Space>
  );

  return (
    <Modal open={open} onCancel={close} width={680} footer={footer}
      title={editing ? (editing === 'new' ? '🎯 新建长期目标' : '🎯 编辑目标') : '🎯 目标交付模式'}>
      {editing ? renderForm() : detail ? renderDetail(detail) : (
        <>
          <div className="hint-text" style={{ marginBottom: 10 }}>
            一个目标可以跨很多轮任务持续推进：挂到输入框上后，每条消息都会带着目标、交付物与最近进度去做，
            结束时把这一轮记进目标。需要无人值守地定时推进，可配合「工作流」的定时触发器。
          </div>
          <Segmented size="small" value={filter} onChange={(v) => setFilter(v as any)}
            options={[{ label: '进行中', value: 'open' }, { label: '全部', value: 'all' }]} />
          <div className="goal-list">
            {goals === null && <div className="hint-text">加载中…</div>}
            {goals !== null && !list.length && (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={filter === 'open' ? '还没有进行中的目标' : '还没有目标'} />
            )}
            {list.map((g) => (
              <div key={g.id} className={'goal-item' + (current?.id === g.id ? ' current' : '')}
                onClick={() => showDetail(g)}>
                <div className="goal-item-main">
                  <Tag color={STATUS_COLOR[g.status]}>{GOAL_STATUS_ZH[g.status]}</Tag>
                  <b>{g.title}</b>
                  {current?.id === g.id && <Tag color="purple">已挂在输入框</Tag>}
                  {g.suggest_done && g.status !== 'done' && <Tag color="gold">待确认完成</Tag>}
                </div>
                <div className="hint-text goal-item-sub">
                  {g.progress ? `进度：${g.progress}` : (g.deliverable ? `交付物：${g.deliverable}` : '（还没有推进记录）')}
                </div>
                <div className="hint-text goal-item-sub">已推进 {g.run_count} 轮 · 更新于 {fmtTime(g.updated)}</div>
                <div className="goal-item-ops" onClick={(e) => e.stopPropagation()}>
                  {g.status !== 'archived' && current?.id !== g.id && (
                    <Button size="small" type="link" onClick={() => attach(g)}>挂到输入框</Button>
                  )}
                  <Popconfirm title="删除目标及其全部推进记录？" okText="删除" okButtonProps={{ danger: true }}
                    onConfirm={() => useRefs.getState().deleteGoal(g.id).catch((e) => message.error(errText(e)))}>
                    <Button size="small" type="link" danger>删除</Button>
                  </Popconfirm>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </Modal>
  );
}
