// 🙋 用户提问弹窗（v1.11.11）—— 模型用 ask_user_question 工具提问时由 WS 事件触发。
//
// 与「审批」的区别：审批是对一个**即将执行的动作**批准/拒绝；这里要的是信息本身
// （目标目录、输出格式、覆盖还是另存…），所以有选项、多选与自定义输入。
//
// 三条硬要求：
//   1. 回答必须真的送达 —— 送不出去就保留弹窗 + 报错，绝不假装提交成功；
//   2. 超时不是同意 —— 倒计时归零后如实告诉用户"模型会按默认方式继续"；
//   3. 多标签页一致 —— 任一窗口作答后服务端广播 resolved，其余窗口的弹窗要关掉。
import { App, Button, Checkbox, Input, Modal, Radio } from 'antd';
import { useEffect, useState } from 'react';
import { usePanel } from '../../store/panel';
import { sendUserAnswer } from '../../ws';

/** 剩余秒数 → "4 分 12 秒" */
const fmtLeft = (s: number) =>
  s >= 60 ? `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒` : `${s} 秒`;

/** 每个问题的作答草稿；key = 问题 id */
type Draft = Record<string, { selected: string[]; custom: string }>;

export default function UserQuestionModal() {
  const { message } = App.useApp();
  const question = usePanel((s) => s.question);
  const clearQuestion = usePanel((s) => s.clearQuestion);
  const [answers, setAnswers] = useState<Draft>({});
  const [left, setLeft] = useState<number | null>(null);

  const qid = question?.question_id || '';

  // 新问题来了就清空草稿：上一条问题的勾选绝不能串到这一条
  useEffect(() => {
    if (!question) { setAnswers({}); return; }
    const init: Draft = {};
    for (const q of question.questions) init[q.id] = { selected: [], custom: '' };
    setAnswers(init);
  }, [qid]);   // eslint-disable-line react-hooks/exhaustive-deps

  // 倒计时：以**收到事件的本地时间**为起点（askedAt），与服务端开始等待的时刻只差网络延迟。
  // 不显示的话，弹窗看起来可以无限等 —— 而工具那边到点就会按超时失败返回。
  useEffect(() => {
    if (!question || !question.timeout_s) { setLeft(null); return; }
    const deadline = question.askedAt + question.timeout_s * 1000;
    const tick = () => setLeft(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [qid, question?.timeout_s, question?.askedAt]);   // eslint-disable-line react-hooks/exhaustive-deps

  // 超时：收起弹窗并说清楚后果。绝不把"没回答"当成默认选项/同意。
  useEffect(() => {
    if (left !== 0 || !question) return;
    clearQuestion();
    message.info('问题已超时，模型会按默认方式继续（超时不会被当成同意）', 6);
  }, [left, question, clearQuestion, message]);

  if (!question) return null;

  const toggle = (id: string, label: string, multi: boolean, checked: boolean) => {
    setAnswers((prev) => {
      const cur = prev[id] || { selected: [], custom: '' };
      const selected = multi
        ? (checked ? [...cur.selected, label] : cur.selected.filter((x) => x !== label))
        : (checked ? [label] : []);
      return { ...prev, [id]: { ...cur, selected } };
    });
  };
  const setCustom = (id: string, custom: string) => {
    setAnswers((prev) => ({ ...prev, [id]: { ...(prev[id] || { selected: [], custom: '' }), custom } }));
  };

  const submit = () => {
    // 允许只回答其中几个问题（服务端把未答的当成"用户没选"），但一个都没答就拦住
    const answered = question.questions.filter((q) => {
      const a = answers[q.id];
      return !!a && (a.selected.length > 0 || a.custom.trim().length > 0);
    });
    if (!answered.length) {
      message.warning('请至少选择一个选项或填写自定义回答');
      return;
    }
    const payload = answered.map((q) => {
      const a = answers[q.id] || { selected: [], custom: '' };
      const custom = a.custom.trim();
      return { id: q.id, selected: a.selected, ...(custom ? { custom } : {}) };
    });
    if (!sendUserAnswer(question.question_id, payload)) {
      message.error('与服务器的连接已断开，回答没能送达；请恢复连接后重试');
      return;
    }
    // 先本地收起，避免连点两次重复提交；服务端确认后还会广播 resolved，对其它标签页幂等
    clearQuestion();
    message.success('回答已提交', 3);
  };

  const urgent = left != null && left <= 30;

  return (
    <Modal
      open
      width={580}
      closable={false}
      maskClosable={false}
      keyboard={false}
      title={<span>🙋 {question.questions.length > 1
        ? `模型有 ${question.questions.length} 个问题需要你确认`
        : '模型需要你确认'}</span>}
      footer={
        <div className="uq-foot">
          <span className={'uq-countdown' + (urgent ? ' warn' : '')}>
            {left != null
              ? `⏳ 剩余 ${fmtLeft(left)} —— 超时不会被当成同意，模型会按默认方式继续`
              : ''}
          </span>
          <Button type="primary" onClick={submit}>提交回答</Button>
        </div>
      }
    >
      <div className="uq-intro hint-text">
        模型在等你做决定。选择一项（或填写自定义内容）后提交，任务会带着你的回答继续。
      </div>
      {question.questions.map((q, qi) => {
        const a = answers[q.id] || { selected: [], custom: '' };
        const opts = q.options || [];
        return (
          <div key={q.id} className="uq-item">
            {q.header && <div className="uq-header">{q.header}</div>}
            <div className="uq-question">
              {qi + 1}. {q.question}
              {q.multi && <span className="hint-text">（可多选）</span>}
            </div>
            {opts.length > 0 && (
              <div className="uq-opts">
                {opts.map((o) => (q.multi ? (
                  <Checkbox key={o.label} checked={a.selected.includes(o.label)}
                    onChange={(e) => toggle(q.id, o.label, true, e.target.checked)}>
                    {o.label}
                    {o.description && <span className="uq-opt-desc">{o.description}</span>}
                  </Checkbox>
                ) : (
                  <Radio key={o.label} checked={a.selected.includes(o.label)}
                    onChange={() => toggle(q.id, o.label, false, true)}>
                    {o.label}
                    {o.description && <span className="uq-opt-desc">{o.description}</span>}
                  </Radio>
                )))}
              </div>
            )}
            <Input.TextArea
              className="uq-custom"
              autoSize={{ minRows: 1, maxRows: 4 }}
              placeholder={opts.length ? '或补充自定义回答（可选）' : '输入你的回答'}
              value={a.custom}
              onChange={(e) => setCustom(q.id, e.target.value)}
            />
          </div>
        );
      })}
    </Modal>
  );
}
