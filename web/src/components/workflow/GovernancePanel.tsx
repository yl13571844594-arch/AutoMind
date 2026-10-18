// 🛡 发布审批（v1.10.0，企业版，R-301）：草稿 → 提交审批 → 批准后成为"已发布版本"。
// 触发器只运行已发布的版本；文件改一个字摘要就变，必须重新提交审批。
import { App, Button, Input, Space, Tag, Tooltip } from 'antd';
import { useState } from 'react';
import { apiPost, errText } from '../../api/client';

export interface PublishState {
  status: 'draft' | 'pending' | 'published' | 'changed';
  approved_digest?: string; approved_by?: any; approved_at?: string;
  pending?: { digest: string; by: any; at: string; comment?: string } | null;
  last_rejection?: { by: any; at: string; comment?: string } | null;
}

export const PUBLISH_TAG: Record<string, { color: string; text: string }> = {
  draft: { color: 'default', text: '草稿' },
  pending: { color: 'processing', text: '待审批' },
  published: { color: 'success', text: '已发布' },
  changed: { color: 'warning', text: '已修改未发布' },
};

const who = (b: any) => (b ? `${b.user || '?'}${b.role ? `（${b.role}）` : ''}` : '');

export default function GovernancePanel({ file, state, canSubmit, canApprove, dirty, onChanged }: {
  file: string; state: PublishState; canSubmit: boolean; canApprove: boolean;
  dirty: boolean; onChanged: () => void;
}) {
  const { message } = App.useApp();
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const tag = PUBLISH_TAG[state.status] || PUBLISH_TAG.draft;

  const act = async (verb: 'submit' | 'approve' | 'reject') => {
    setBusy(true);
    try {
      await apiPost(`/workflow/governance/${encodeURIComponent(file)}/${verb}`, { comment });
      message.success({ submit: '已提交发布审批', approve: '已批准发布', reject: '已驳回' }[verb]);
      setComment('');
      onChanged();
    } catch (e) { message.error(errText(e), 6); }
    finally { setBusy(false); }
  };

  return (
    <div className="wf-governance">
      <Space wrap size={6}>
        <span>🛡 发布状态</span>
        <Tag color={tag.color}>{tag.text}</Tag>
        {state.approved_digest && (
          <Tooltip title={`已发布版本摘要 ${state.approved_digest}`}>
            <span className="hint-text">
              已发布 {state.approved_digest.slice(0, 10)} · {who(state.approved_by)} · {state.approved_at}
            </span>
          </Tooltip>
        )}
        {state.pending && (
          <span className="hint-text">
            待审：{who(state.pending.by)} 于 {state.pending.at} 提交{state.pending.comment ? `（${state.pending.comment}）` : ''}
          </span>
        )}
      </Space>
      {state.status !== 'published' && (
        <div className="hint-text" style={{ margin: '4px 0' }}>
          {state.status === 'changed'
            ? '文件在发布之后被修改过：触发器暂停运行，直到新版本通过发布审批。'
            : '还没有已发布版本：定时 / webhook 触发器不会运行这份工作流。'}
          {state.last_rejection && !state.pending
            && ` 上次被 ${who(state.last_rejection.by)} 驳回：${state.last_rejection.comment || '（无说明）'}`}
        </div>
      )}
      <Space wrap size={6} style={{ marginTop: 4 }}>
        <Input size="small" style={{ width: 240 }} placeholder="说明（可选）" value={comment}
          onChange={(e) => setComment(e.target.value)} />
        {!state.pending && state.status !== 'published' && (
          <Tooltip title={dirty ? '先保存修改再提交' : canSubmit ? '' : '没有编辑这份工作流的权限'}>
            <Button size="small" disabled={!canSubmit || dirty} loading={busy}
              onClick={() => act('submit')}>提交发布审批</Button>
          </Tooltip>
        )}
        {state.pending && (
          <>
            <Tooltip title={canApprove ? '批准的必须是提交的那一版；不能批准自己提交的版本' : '没有批准发布的权限'}>
              <Button size="small" type="primary" disabled={!canApprove} loading={busy}
                onClick={() => act('approve')}>批准发布</Button>
            </Tooltip>
            <Button size="small" danger disabled={!canApprove} loading={busy}
              onClick={() => act('reject')}>驳回</Button>
          </>
        )}
      </Space>
    </div>
  );
}
