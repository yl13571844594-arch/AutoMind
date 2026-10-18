// 工作流运行卡片（v1.8.0）：逐步进度 + 停止 / 续跑 / 下载报告。
// 对话区的运行气泡与「工作流」视图的运行面板共用这一个组件。
import { App, Button, Progress, Tag, Tooltip } from 'antd';
import { useEffect } from 'react';
import { errText } from '../../api/client';
import { chatSid } from '../../store/app';
import {
  downloadReport, RUN_STATUS_ZH, STEP_ICON, STEP_TYPE_ICON, TRIGGER_ZH, useWorkflow,
  type WfRun, type WfStep,
} from '../../store/workflow';

const STATUS_COLOR: Record<string, string> = {
  running: 'processing', ok: 'success', completed: 'success', partial: 'warning',
  aborted: 'error', failed: 'error', cancelled: 'default', interrupted: 'default',
};

const stepColor = (s: WfStep['status']) =>
  s === 'ok' ? 'var(--green)' : s === 'failed' || s === 'aborted' ? 'var(--red)'
    : s === 'running' ? 'var(--accent)' : 'var(--text3)';

function StepRow({ st, index, child = false }: { st: WfStep; index: number; child?: boolean }) {
  const d = st.detail || {};
  const extra: string[] = [];
  if (st.type === 'parallel' && st.children?.length) {
    const ok = st.children.filter((c) => c.status === 'ok').length;
    extra.push(`并行 ${ok}/${st.children.length} 完成`
      + (d.max_concurrency ? `（最多同时 ${d.max_concurrency} 个）` : ''));
  }
  if (st.type === 'foreach' && d.max_concurrency) extra.push(`逐项并发 ${d.max_concurrency}`);
  if (d.resumed) extra.push('沿用上次结果');
  if (st.retries) extra.push(`重试 ${st.retries} 次`);
  if (st.iter?.total) extra.push(`第 ${st.iter.done}/${st.iter.total} 项`
    + (st.iter.failed ? `（${st.iter.failed} 项失败）` : ''));
  if (d.stop_reason && st.type === 'agent') extra.push(`结束原因 ${d.stop_reason}`);
  if (d.approval) extra.push(d.approval === 'approved' ? '已批准' : d.approval === 'denied' ? '未批准' : '无审批通道');
  return (
    <div className="wf-step" data-status={st.status} style={child ? { marginLeft: 22 } : undefined}>
      <span className="wf-step-icon" style={{ color: stepColor(st.status) }}>
        {st.status === 'running' ? <span className="wf-spin">◌</span> : STEP_ICON[st.status] || '○'}
      </span>
      <div className="wf-step-main">
        <div>
          <span className="hint-text">{child ? '└' : `${index + 1}.`}</span>{' '}
          {STEP_TYPE_ICON[st.type] || '•'} <code>{st.id}</code>
          {st.line ? <span className="hint-text">（第 {st.line} 行）</span> : null}
          {st.duration_ms ? <span className="hint-text"> · {(st.duration_ms / 1000).toFixed(1)}s</span> : null}
          {extra.length > 0 && <span className="hint-text"> · {extra.join(' · ')}</span>}
        </div>
        {st.error && st.status !== 'ok' && (
          <div className="wf-step-error">{st.error}</div>
        )}
        {Array.isArray(d.denied_tools) && d.denied_tools.length > 0 && (
          <div className="wf-step-error">⚠ 越权调用已拒绝：{d.denied_tools.join('、')}</div>
        )}
        {st.children && st.children.length > 0 && (
          <div className="wf-children">
            {st.children.map((c, i) => <StepRow key={c.id} st={c} index={i} child />)}
          </div>
        )}
        {st.status === 'running' && st.actions && st.actions.length > 0 && (
          <div className="hint-text">
            最近动作：{st.actions.slice(-4).map((a, i) => (
              <span key={i} style={{ color: a.ok ? undefined : 'var(--red)' }}>{a.tool}{a.ok ? '' : '✗'} </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function WorkflowRunCard({ runId, compact = false, onOpen, onResumed }: {
  runId: string; compact?: boolean; onOpen?: (run: WfRun) => void;
  /** 续跑成功后回调新运行的 id（所在面板据此切到新运行） */
  onResumed?: (newRunId: string) => void;
}) {
  const { message } = App.useApp();
  const run = useWorkflow((s) => s.runs[runId]);
  const fetchRun = useWorkflow((s) => s.fetchRun);
  const cancel = useWorkflow((s) => s.cancel);
  const resume = useWorkflow((s) => s.resume);

  // 刷新页面后内存里没有这次运行 —— 从服务端补一份（运行记录在磁盘上）
  useEffect(() => {
    if (!run) fetchRun(runId).catch(() => { /* 卡片里显示"读取失败" */ });
  }, [run, runId, fetchRun]);

  if (!run) {
    return <div className="wf-card"><span className="hint-text">正在读取工作流运行 {runId}…</span></div>;
  }

  const done = run.steps.filter((s) => !['pending', 'running'].includes(s.status)).length;
  const total = run.steps.length || 1;
  const running = run.status === 'running';
  const pct = Math.round((done / total) * 100);

  const doCancel = async () => {
    try { await cancel(run.run_id); message.info('已请求停止，当前步骤会被标记为「已取消」'); }
    catch (e) { message.error(errText(e)); }
  };
  const doResume = async () => {
    try {
      const nid = await resume(run.run_id, run.session_id || chatSid());
      message.success(`已从断点续跑（新运行 ${nid}），已成功的步骤不会重复执行`);
      onResumed?.(nid);
    } catch (e) { message.error(errText(e)); }
  };

  return (
    <div className="wf-card">
      <div className="wf-card-head">
        <span>🔀 <b>{run.workflow}</b></span>
        <Tag color={STATUS_COLOR[run.status] || 'default'}>
          {run.stopping && running ? '停止中…' : RUN_STATUS_ZH[run.status] || run.status}
        </Tag>
        {running && run.queuePosition != null && (
          <Tooltip title="企业版并发配额：同团队同时运行数已满，轮到时自动开始（可随时停止）">
            <Tag color="orange">排队中 · 第 {run.queuePosition} 位</Tag>
          </Tooltip>
        )}
        {run.trigger && run.trigger !== 'manual' && (
          <Tooltip title="由触发器发起的无人值守运行（专业版）">
            <Tag color="purple">{TRIGGER_ZH[run.trigger] || run.trigger}</Tag>
          </Tooltip>
        )}
        {run.resumed_from && (
          <Tooltip title={`接续运行 ${run.resumed_from}；沿用了 ${run.reused?.length || 0} 个已成功步骤的结果`}>
            <Tag>↻ 续跑</Tag>
          </Tooltip>
        )}
        <span className="hint-text mono" style={{ marginLeft: 'auto' }}>{run.run_id}</span>
      </div>
      {running && <Progress percent={pct} size="small" showInfo={false} status="active" />}
      {!compact || running || run.status !== 'ok' ? (
        <div className="wf-steps">
          {run.steps.map((st, i) => <StepRow key={st.id} st={st} index={i} />)}
        </div>
      ) : null}
      {run.summary_line && <div className="hint-text" style={{ marginTop: 6 }}>{run.summary_line}</div>}
      {!running && run.error && run.status !== 'ok' && (
        <div className="wf-step-error" style={{ marginTop: 4 }}>{run.error}</div>
      )}
      <div className="wf-card-actions">
        {running && (
          <Button size="small" danger disabled={run.stopping} onClick={doCancel}>⏹ 停止</Button>
        )}
        {!running && run.resumable && (
          <Tooltip title="已成功的步骤直接沿用上次结果，从第一个未成功的步骤接着跑（计 1 次任务额度）">
            <Button size="small" type="primary" onClick={doResume}>↻ 从断点续跑</Button>
          </Tooltip>
        )}
        {!running && run.report && (
          <Button size="small" onClick={() => downloadReport(run)}>⬇ 下载报告</Button>
        )}
        {onOpen && <Button size="small" type="link" onClick={() => onOpen(run)}>在工作流视图中查看</Button>}
      </div>
    </div>
  );
}
