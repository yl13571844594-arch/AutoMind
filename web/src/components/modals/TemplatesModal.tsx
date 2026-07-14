// 📚 模板库：10 个内置模板 + ⭐ 自定义模板（专业版 custom_templates）。
import { App, Button, Input, Modal, Select, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { TEMPLATES } from '../../lib/templates';
import { EDITION_LABELS, MODE_FEATURE, MODE_LABELS, useApp, type Mode } from '../../store/app';
import { useChat } from '../../store/chat';
import { useUi } from '../../store/ui';

const { Text } = Typography;

export default function TemplatesModal() {
  const { message, modal } = App.useApp();
  const open = useUi((s) => s.modal) === 'templates';
  const close = useUi((s) => s.closeModal);
  const featureOn = useApp((s) => s.featureOn);
  const edition = useApp((s) => s.edition);
  const [customs, setCustoms] = useState<any[]>([]);
  const [form, setForm] = useState({ icon: '⭐', title: '', mode: 'work', prompt: '' });

  const proOn = featureOn('custom_templates');
  const reload = () => {
    if (proOn) apiGet('/templates/custom').then((r) => setCustoms(r.templates || [])).catch(() => setCustoms([]));
  };
  useEffect(() => { if (open) reload(); }, [open, proOn]);

  const useTemplate = async (t: { mode: string; prompt: string }) => {
    close();
    const m = t.mode as Mode;
    if (m !== useApp.getState().mode && useApp.getState().featureOn(MODE_FEATURE[m])) {
      await useApp.getState().setMode(m);
    }
    useChat.getState().setInputDraft(t.prompt);
    // 直接写入输入框（非受控 textarea）
    const ta = document.querySelector<HTMLTextAreaElement>('textarea');
    if (ta) { ta.value = t.prompt; ta.focus(); }
    message.info('模板已填入，补充细节后按 Enter 发送');
  };

  const card = (t: any, extra?: React.ReactNode) => (
    <div key={t.id || t.title} onClick={() => useTemplate(t)} style={{
      display: 'flex', gap: 10, alignItems: 'flex-start', border: '1px solid var(--border)',
      borderRadius: 10, padding: 10, cursor: 'pointer',
    }}>
      <span style={{ fontSize: '1.4em' }}>{t.icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <Text strong>{t.title}</Text>
        <Tag style={{ marginLeft: 6, fontSize: '.68em' }}>{MODE_LABELS[t.mode] || t.mode}</Tag>
        <div className="hint-text" style={{ marginTop: 2 }}>{t.desc || ''}</div>
      </div>
      {extra}
    </div>
  );

  return (
    <Modal title="📚 模板库" open={open} onCancel={close} width={680} footer={null}>
      <Text type="secondary" style={{ fontSize: '.84em' }}>
        选一个模板快速开始 — 点击后自动切换模式并填入任务描述，你只需补充细节再发送。
      </Text>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 12 }}>
        {TEMPLATES.map((t) => card(t))}
      </div>
      <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
        {!proOn ? (
          <>
            <Text strong style={{ fontSize: '.9em' }}>⭐ 我的模板 <span className="hint-text">🔒 专业版</span></Text>
            <div className="hint-text" style={{ marginTop: 6 }}>
              专业版可把常用任务保存为自定义模板（最多 100 个），团队沉淀提示词资产。当前为{EDITION_LABELS[edition]}。
            </div>
          </>
        ) : (
          <>
            <Text strong style={{ fontSize: '.9em' }}>⭐ 我的模板（{customs.length}）</Text>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 8 }}>
              {customs.map((t) => card(t, (
                <Button size="small" danger type="text" onClick={(e) => {
                  e.stopPropagation();
                  modal.confirm({
                    title: '删除该自定义模板？',
                    onOk: async () => { await apiDelete(`/templates/custom/${encodeURIComponent(t.id)}`); message.info('已删除'); reload(); },
                  });
                }}>✕</Button>
              )))}
            </div>
            <Space.Compact style={{ width: '100%', marginTop: 10 }}>
              <Input style={{ width: 60 }} maxLength={4} value={form.icon} onChange={(e) => setForm({ ...form, icon: e.target.value })} />
              <Input style={{ width: 160 }} maxLength={40} placeholder="模板名称" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
              <Select style={{ width: 110 }} value={form.mode} onChange={(v) => setForm({ ...form, mode: v })}
                options={[{ value: 'work', label: '⚙️ 工作' }, { value: 'coding', label: '💻 编程' }, { value: 'chat', label: '💬 对话' }]} />
              <Button type="primary" onClick={async () => {
                if (!form.title.trim() || !form.prompt.trim()) { message.error('模板名称与提示词必填'); return; }
                const r = await apiPost('/templates/custom', form);
                if (r.error) { message.error(r.error); return; }
                message.success(`模板「${form.title}」已保存`);
                setForm({ icon: '⭐', title: '', mode: 'work', prompt: '' });
                reload();
              }}>保存模板</Button>
            </Space.Compact>
            <Input.TextArea style={{ marginTop: 8 }} rows={3} placeholder="模板提示词（任务描述，可含「（把 XX 粘贴到这里）」占位）"
              value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
          </>
        )}
      </div>
    </Modal>
  );
}
