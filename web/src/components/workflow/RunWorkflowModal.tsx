// ▶ 运行工作流（输入框「＋」菜单入口，v1.8.0 R-109）。
// 选一个工作流 → 按 inputs 声明填入参 → 运行；运行卡片出现在当前会话。
import { App, Button, Modal, Space, Tag } from 'antd';
import Select from '../ui/SafeSelect';
import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost, errText } from '../../api/client';
import { chatSid, useApp } from '../../store/app';
import { useUi } from '../../store/ui';
import InputsForm, { collectInputs, initialDraft, type InputDraft } from './InputsForm';

export default function RunWorkflowModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal === 'runWorkflow');
  const close = useUi((s) => s.closeModal);
  const [list, setList] = useState<any[] | null>(null);
  const [err, setErr] = useState('');
  const [file, setFile] = useState('');
  const [draft, setDraft] = useState<InputDraft>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setErr(''); setList(null);
    apiGet('/workflow/list')
      .then((d) => setList(d.workflows || []))
      .catch((e) => { setErr(errText(e)); setList([]); });
  }, [open]);

  const selected = useMemo(() => (list || []).find((w) => w.file === file), [list, file]);
  useEffect(() => { setDraft(initialDraft(selected?.inputs || {})); }, [selected]);

  const run = async (dry: boolean) => {
    if (!selected) return;
    const [inputs, errors] = collectInputs(selected.inputs || {}, draft);
    if (errors.length) { message.error(errors[0]); return; }
    setBusy(true);
    try {
      const r = await apiPost('/workflow/run', {
        file: selected.file, inputs, dry_run: dry, session_id: chatSid(),
      });
      if (dry) {
        const bad = (r.steps || []).filter((s: any) => s.status !== 'ok');
        if (bad.length) message.warning(`试运行发现问题：${bad[0].id} —— ${bad[0].error}`, 6);
        else message.success(`试运行通过：${r.summary?.total ?? 0} 步都能正确渲染（未执行任何工具）`);
      } else {
        message.success(`工作流「${selected.name}」已开始运行`);
        useApp.getState().setView('chat');
        close();
      }
    } catch (e) {
      message.error(errText(e), 6);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onCancel={close} title="▶ 运行工作流" width={560}
      footer={
        <Space>
          <Button onClick={() => { close(); useApp.getState().setView('workflow'); }}>管理工作流…</Button>
          <Button disabled={!selected?.valid || busy} onClick={() => run(true)}>试运行</Button>
          <Button type="primary" loading={busy}
            disabled={!selected?.valid || !selected?.runnable} onClick={() => run(false)}>运行</Button>
        </Space>
      }>
      {err && <div className="wf-step-error">{err}</div>}
      <Select style={{ width: '100%' }} placeholder={list === null ? '正在读取工作流…' : '选择一个工作流'}
        loading={list === null} value={file || undefined} onChange={setFile}
        notFoundContent="项目的 workflows/ 目录下还没有工作流 —— 去「🔀 工作流」新建一个"
        options={(list || []).map((w) => ({
          value: w.file, disabled: !w.valid,
          label: (
            <span>
              {w.name} <span className="hint-text">{w.file} · {w.steps} 步</span>
              {!w.valid && <Tag color="error" style={{ marginLeft: 6 }}>校验未通过</Tag>}
              {w.valid && !w.runnable && <Tag color="warning" style={{ marginLeft: 6 }}>超出上限</Tag>}
            </span>
          ),
        }))} />
      {selected && (
        <div style={{ marginTop: 12 }}>
          {selected.description && <div className="hint-text" style={{ marginBottom: 8 }}>{selected.description}</div>}
          {!selected.runnable && selected.blocked_reason && (
            <div className="wf-step-error" style={{ marginBottom: 8 }}>{selected.blocked_reason}</div>
          )}
          <InputsForm specs={selected.inputs || {}} draft={draft} onChange={setDraft} />
          <div className="hint-text" style={{ marginTop: 8 }}>
            运行计 1 次任务额度；进度与需要你审批的步骤会出现在当前会话里。
          </div>
        </div>
      )}
    </Modal>
  );
}
