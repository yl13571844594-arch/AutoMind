// ⏰ 从输入框新建定时任务（v1.11.1）。
//
// 此前定时任务只能去左侧「系统 → 定时任务」页面里建 —— 而人通常是在输入框里
// 写完一句"每天早上帮我汇总…"之后才意识到"这该定时跑"。现在输入框旁一个 ⏰，
// 把正在写的内容直接带过来，选个频率就建好。社区版给出升级入口而不是一个死按钮。
import { App, Input, InputNumber, Modal, Segmented } from 'antd';
import Select from '../ui/SafeSelect';
import { useEffect, useState } from 'react';
import { apiPost, errText } from '../../api/client';
import { MODE_FEATURE, MODE_LABELS, useApp, type Mode } from '../../store/app';
import { useUi } from '../../store/ui';

const PRESETS: { label: string; value: number }[] = [
  { label: '每 15 分钟', value: 900 },
  { label: '每小时', value: 3600 },
  { label: '每 6 小时', value: 21600 },
  { label: '每天', value: 86400 },
  { label: '每周', value: 604800 },
];
const UNITS = [
  { label: '分钟', value: 60 }, { label: '小时', value: 3600 }, { label: '天', value: 86400 },
];

export default function ScheduleQuickModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'schedule';
  const draft = useUi((s) => s.scheduleDraft);
  const close = useUi((s) => s.closeModal);
  const mode = useApp((s) => s.mode);
  const featureOn = useApp((s) => s.featureOn);
  const locked = !featureOn('scheduler');

  const [task, setTask] = useState('');
  const [name, setName] = useState('');
  const [interaction, setInteraction] = useState<Mode>('chat');
  const [preset, setPreset] = useState<number | 'custom'>(86400);
  const [num, setNum] = useState(30);
  const [unit, setUnit] = useState(60);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setTask(draft); setName(''); setInteraction(mode === 'chat' ? 'work' : mode);
    setPreset(86400); setBusy(false);
  }, [open]);

  const interval = preset === 'custom' ? Math.max(1, num || 1) * unit : preset;

  const submit = async () => {
    if (!task.trim()) { message.error('请填写要定时执行的任务内容'); return; }
    if (interval < 60) { message.error('间隔不能少于 1 分钟'); return; }
    setBusy(true);
    try {
      await apiPost('/schedule', { name: name.trim(), task: task.trim(), interaction, interval });
      close();
      message.success({
        content: <span>定时任务已创建 · <a onClick={() => useApp.getState().setView('schedule')}>查看全部</a></span>,
        duration: 5,
      });
    } catch (e) {
      message.error(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onCancel={close} width={520} destroyOnClose
      title="⏰ 新建定时任务"
      okText="创建" cancelText="取消" onOk={submit} confirmLoading={busy}
      okButtonProps={{ disabled: locked }}
      footer={locked ? null : undefined}>
      {locked ? (
        <div className="sq-locked">
          <div className="sq-locked-ico">🔒</div>
          <b>定时任务是专业版功能</b>
          <p className="hint-text">按固定间隔自动执行任意任务，后台调度并记录每次结果。
            激活许可证后立即可用，无需重启。</p>
          <button className="lic-btn primary" onClick={() => useUi.getState().openModal('license')}>
            ⭐ 升级 / 激活许可证
          </button>
        </div>
      ) : (
        <div className="sq-form">
          <label>任务内容</label>
          <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} value={task} autoFocus
            placeholder="例如：汇总昨天新增的工单，按优先级列出并写入 reports/daily.md"
            onChange={(e) => setTask(e.target.value)} />
          <label>执行频率</label>
          <Segmented block value={preset} onChange={(v) => setPreset(v as any)}
            options={[...PRESETS, { label: '自定义', value: 'custom' }]} />
          {preset === 'custom' && (
            <div className="sq-custom">
              每 <InputNumber min={1} max={9999} value={num} onChange={(v) => setNum(Number(v) || 1)} />
              <Select value={unit} onChange={setUnit} options={UNITS} style={{ width: 90 }} />
              执行一次
            </div>
          )}
          <div className="sq-row">
            <div>
              <label>执行模式</label>
              <Select value={interaction} onChange={setInteraction} style={{ width: '100%' }}
                options={(['chat', 'work', 'coding', 'multi', 'loop'] as Mode[]).map((m) => ({
                  value: m, disabled: !featureOn(MODE_FEATURE[m]),
                  label: `${MODE_LABELS[m]}${featureOn(MODE_FEATURE[m]) ? '' : ' 🔒'}`,
                }))} />
            </div>
            <div>
              <label>名称（可选）</label>
              <Input value={name} maxLength={40} placeholder={task.slice(0, 20) || '自动取任务开头'}
                onChange={(e) => setName(e.target.value)} />
            </div>
          </div>
          <p className="hint-text">首次执行在创建后一个间隔；可在「定时任务」页面立即运行、暂停或删除。</p>
        </div>
      )}
    </Modal>
  );
}
