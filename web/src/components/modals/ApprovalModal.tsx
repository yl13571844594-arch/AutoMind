// 🙋 工具调用审批（ask 模式下由 WS approval_request 触发）
//
// 三种处置：拒绝 / 批准 / **修改参数后批准**。
// 最后一种对应 ApprovalAction.MODIFY —— 枚举与 ApprovalResponse.modifications
// 早就定义了，但此前整条链路（前端只有两个按钮、CLI 只有 A/D/S/Q）都无从表达，
// 于是遇到"命令基本对、就是路径写错了"只能拒绝再让模型重来一轮。
import { Button, Input, Modal, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { usePanel } from '../../store/panel';
import { sendApproval } from '../../ws';

/** 参数值 → 编辑框里的文本。对象/数组用 JSON 呈现，便于原样改。 */
const toText = (v: any): string =>
  v === null || v === undefined ? ''
    : typeof v === 'string' ? v
      : typeof v === 'object' ? JSON.stringify(v, null, 2)
        : String(v);

/**
 * 编辑框文本 → 参数值，按**原值类型**还原。
 *
 * 终端/输入框里的一切都是字符串；若原值是 `timeout: 30`（数字），回传 "60"
 * 会让工具拿到字符串而行为异常。转不动就保留原文，不猜。
 */
function fromText(text: string, original: any): any {
  if (typeof original === 'number') {
    const n = Number(text);
    return Number.isFinite(n) && text.trim() !== '' ? n : text;
  }
  if (typeof original === 'boolean') {
    return ['1', 'true', 'yes', 'y', 'on', '是', '真'].includes(text.trim().toLowerCase());
  }
  if (original !== null && typeof original === 'object') {
    try { return JSON.parse(text); } catch { return text; }
  }
  return text;
}

/** 剩余秒数 → "4 分 12 秒" */
const fmtLeft = (s: number) =>
  s >= 60 ? `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒` : `${s} 秒`;

export default function ApprovalModal() {
  const approval = usePanel((s) => s.approval);
  const setApproval = usePanel((s) => s.setApproval);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [left, setLeft] = useState<number | null>(null);

  // 可编辑的原始值优先；老服务端只发截断过的 params，退而求其次
  const source: Record<string, any> = useMemo(
    () => (approval?.editable && Object.keys(approval.editable).length
      ? approval.editable
      : (approval?.params || {})),
    [approval],
  );

  // 每来一个新的审批请求都要重置，否则上一次的草稿会串到这次
  useEffect(() => {
    setEditing(false);
    setDraft(Object.fromEntries(Object.entries(source).map(([k, v]) => [k, toText(v)])));
  }, [approval?.approval_id, source]);

  // 倒计时：后端等待上限到点后会按「拒绝」处理并结束任务。不显示的话，
  // 弹窗看起来可以一直等 —— 用户去泡杯茶回来，任务早已失败。
  useEffect(() => {
    if (!approval?.timeoutS || !approval.askedAt) { setLeft(null); return; }
    const deadline = approval.askedAt + approval.timeoutS * 1000;
    const tick = () => setLeft(Math.max(0, Math.round((deadline - Date.now()) / 1000)));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [approval?.approval_id, approval?.timeoutS, approval?.askedAt]);

  if (!approval) return null;

  const changed = Object.keys(source).filter((k) => draft[k] !== toText(source[k]));

  const respond = (ok: boolean, withEdits = false) => {
    const args = withEdits
      ? Object.fromEntries(Object.keys(source).map((k) => [k, fromText(draft[k] ?? '', source[k])]))
      : undefined;
    sendApproval(approval.approval_id, ok, args);
    setApproval(null);
  };

  const hasParams = Object.keys(source).length > 0;

  return (
    <Modal
      title="🙋 工具调用审批" open closable={false} width={editing ? 640 : 520}
      footer={
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <Button danger onClick={() => respond(false)}>拒绝</Button>
          {hasParams && !editing && (
            <Button onClick={() => setEditing(true)}>✎ 修改参数</Button>
          )}
          {editing && (
            <>
              <Button onClick={() => setEditing(false)}>取消修改</Button>
              <Button type="primary" disabled={!changed.length}
                      onClick={() => respond(true, true)}>
                {changed.length ? `改 ${changed.length} 项并批准` : '未做修改'}
              </Button>
            </>
          )}
          {!editing && <Button type="primary" onClick={() => respond(true)}>批准</Button>}
        </div>
      }
    >
      <div style={{ border: '1px solid var(--yellow)', borderRadius: 10, padding: 12 }}>
        <b>{approval.tool}</b>{' '}
        <Tag color={approval.tier === 'dangerous' ? 'red'
          : approval.tier === 'sensitive' ? 'gold' : 'green'}>{approval.tier}</Tag>
        <div style={{ fontSize: '.85em', color: 'var(--text2)', marginTop: 6 }}>
          {approval.reason}
        </div>
        {left !== null && (
          <div style={{
            marginTop: 8, fontSize: '.8em', fontWeight: 600,
            color: left <= 60 ? 'var(--red)' : left <= 120 ? 'var(--yellow)' : 'var(--text3)',
          }}>
            ⏳ 剩余 {fmtLeft(left)}
            <span style={{ fontWeight: 400, color: 'var(--text3)' }}>
              {' '}—— 超时将按「拒绝」处理并结束本次任务
            </span>
          </div>
        )}

        {!editing && Object.entries(approval.params || {}).map(([k, v]) => (
          <div key={k} className="mono hint-text" style={{ marginTop: 4, overflowWrap: 'anywhere' }}>
            {k} = {String(v)}
          </div>
        ))}

        {editing && (
          <div style={{ marginTop: 10 }}>
            <div className="hint-text" style={{ marginBottom: 8 }}>
              改完点「改 N 项并批准」，Agent 将<b>按修改后的参数</b>执行这一步。
            </div>
            {Object.keys(source).map((k) => {
              const multiline = toText(source[k]).length > 60
                || toText(source[k]).includes('\n');
              const dirty = draft[k] !== toText(source[k]);
              return (
                <div key={k} style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: '.78em', color: 'var(--text2)', marginBottom: 3 }}>
                    <code>{k}</code>
                    {dirty && <span style={{ color: 'var(--yellow)', marginLeft: 6 }}>已修改</span>}
                  </div>
                  {multiline ? (
                    <Input.TextArea className="mono" autoSize={{ minRows: 2, maxRows: 10 }}
                      value={draft[k] ?? ''}
                      status={dirty ? 'warning' : undefined}
                      onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
                  ) : (
                    <Input className="mono" value={draft[k] ?? ''}
                      status={dirty ? 'warning' : undefined}
                      onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </Modal>
  );
}
