// 🙋 工具调用审批（ask 模式下由 WS approval_request 触发）
import { Button, Modal, Tag } from 'antd';
import { usePanel } from '../../store/panel';
import { sendApproval } from '../../ws';

export default function ApprovalModal() {
  const approval = usePanel((s) => s.approval);
  const setApproval = usePanel((s) => s.setApproval);
  if (!approval) return null;

  const respond = (ok: boolean) => {
    sendApproval(approval.approval_id, ok);
    setApproval(null);
  };

  return (
    <Modal
      title="🙋 工具调用审批" open closable={false} footer={
        <>
          <Button danger onClick={() => respond(false)}>拒绝</Button>
          <Button type="primary" onClick={() => respond(true)}>批准</Button>
        </>
      }
    >
      <div style={{ border: '1px solid var(--yellow)', borderRadius: 10, padding: 12 }}>
        <b>{approval.tool}</b> <Tag color={approval.tier === 'dangerous' ? 'red' : approval.tier === 'sensitive' ? 'gold' : 'green'}>{approval.tier}</Tag>
        <div style={{ fontSize: '.85em', color: 'var(--text2)', marginTop: 6 }}>{approval.reason}</div>
        {Object.entries(approval.params).map(([k, v]) => (
          <div key={k} className="mono hint-text" style={{ marginTop: 4 }}>{k} = {v}</div>
        ))}
      </div>
    </Modal>
  );
}
