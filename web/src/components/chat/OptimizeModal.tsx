// v1.11.20 「✨ 优化问题」弹窗 —— 先预览、可编辑，确认后才替换输入框。
//
// 为什么是"弹窗确认"而不是"点一下直接改写输入框"：问题优化会改动**用户亲手写的
// 文字**，直接替换等于替用户做了决定；而模糊问题恰恰是最需要人再看一眼的。
// 这里只做展示与编辑，不自己发请求、不自己关闭 —— 请求与采用动作都留在 ChatPanel，
// 弹窗因此能被单独渲染/测试，也不会在关闭动画里丢状态。
import { Alert, Button, Input, Modal } from 'antd';
import { useEffect, useState } from 'react';

import type { OptimizeResult } from '../../lib/promptOptimize';

interface Props {
  open: boolean;
  loading: boolean;
  result: OptimizeResult | null;
  error: string;
  onClose: () => void;
  onAdopt: (optimized: string) => void;
  onRetry: (text: string) => void;
}

export default function OptimizeModal({
  open, loading, result, error, onClose, onAdopt, onRetry,
}: Props) {
  // 当前编辑中的文本：默认取优化结果，用户改过的内容"再优化一次"要带上。
  const [edited, setEdited] = useState('');

  // 每次拿到新结果（首次优化 / 再优化）都重置编辑区，避免把上一版残留当新版采用。
  useEffect(() => {
    setEdited(result?.optimized ?? '');
  }, [result]);

  const ready = edited.trim().length > 0;
  const unchangedClear = !!result && !result.changed && result.clear_enough;

  const footer = (
    <div className="opt-foot">
      <Button onClick={onClose} disabled={loading}>取消</Button>
      <span style={{ flex: 1 }} />
      <Button
        onClick={() => onRetry(edited)}
        disabled={loading || !ready}
        title="把编辑区里的内容再优化一次"
      >
        再优化一次
      </Button>
      <Button
        type="primary"
        onClick={() => onAdopt(edited)}
        loading={loading}
        disabled={!ready}
        title="把编辑区里的内容替换到输入框（不会自动发送）"
      >
        采用并填入输入框
      </Button>
    </div>
  );

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={760}
      title="✨ 优化问题"
      footer={footer}
      maskClosable={!loading}
      keyboard={!loading}
      closable={!loading}
      className="opt-modal"
    >
      <div className="opt-wrap">
        <div className="opt-sec">
          <div className="opt-label">原问题</div>
          <div className="opt-orig">{result?.original || ''}</div>
        </div>

        <div className="opt-sec">
          <div className="opt-label">
            优化后
            <span className="hint-text">（可直接编辑；只填入输入框，不会发送）</span>
          </div>
          <Input.TextArea
            className="opt-edit"
            value={edited}
            onChange={(e) => setEdited(e.target.value)}
            autoSize={{ minRows: 7, maxRows: 16 }}
            placeholder="优化结果会出现在这里"
          />
        </div>

        {unchangedClear && (
          <div className="opt-good">
            ✓ 问题已经比较清楚，可直接发送；采用不会改动原文。
          </div>
        )}

        {!!result?.notes?.length && (
          <div className="opt-sec">
            <div className="opt-label">改动点</div>
            <ul className="opt-list opt-scroll">
              {result.notes.map((n, i) => <li key={`n${i}`}>{n}</li>)}
            </ul>
          </div>
        )}

        {!!result?.questions?.length && (
          <div className="opt-sec">
            <div className="opt-label">
              待确认
              <span className="hint-text">（补充到问题里再发送会让结果更准）</span>
            </div>
            <ul className="opt-list opt-scroll">
              {result.questions.map((q, i) => <li key={`q${i}`}>{q}</li>)}
            </ul>
          </div>
        )}

        {result?.by === 'rule' && (
          <div className="opt-rule">
            ℹ️ 本地规则兜底（模型未配置或调用失败）：内容按固定结构整理，未做智能改写。
          </div>
        )}

        {error && (
          <Alert type="error" showIcon message={error} className="opt-err" />
        )}
      </div>
    </Modal>
  );
}
