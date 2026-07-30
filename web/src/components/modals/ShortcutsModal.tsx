// ⌨ 快捷键帮助 —— 内容直接由 HOTKEYS 注册表生成，与实际生效的按键同源，
// 不会出现"帮助里写着、按了却没反应"。
import { Modal, Typography } from 'antd';
import { HOTKEYS, prettyCombo, type Hotkey } from '../../lib/hotkeys';
import { useUi } from '../../store/ui';

const { Text } = Typography;

const GROUPS: Hotkey['group'][] = ['通用', '模式', '导航'];

export default function ShortcutsModal() {
  const open = useUi((s) => s.modal) === 'shortcuts';
  const close = useUi((s) => s.closeModal);
  const all = Object.values(HOTKEYS);

  return (
    <Modal title="⌨ 键盘快捷键" open={open} onCancel={close} footer={null} width={560}>
      {GROUPS.map((g) => {
        const rows = all.filter((h) => h.group === g);
        if (!rows.length) return null;
        return (
          <div key={g} style={{ marginBottom: 18 }}>
            <Text strong style={{ fontSize: '.92em', color: 'var(--text2)' }}>{g}</Text>
            <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
              {rows.map((h) => (
                <div key={h.combo + h.label} style={{
                  display: 'flex', alignItems: 'center', gap: 12,
                  padding: '5px 10px', borderRadius: 8, background: 'var(--bg2)',
                }}>
                  <span style={{ flex: 1, fontSize: '.9em' }}>{h.label}</span>
                  {[h.combo, h.alias].filter(Boolean).map((c, i) => (
                    <span key={c} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      {i > 0 && <span style={{ fontSize: '.76em', color: 'var(--text3)' }}>或</span>}
                      <kbd style={{
                        fontFamily: 'var(--mono)', fontSize: '.82em', whiteSpace: 'nowrap',
                        padding: '3px 9px', borderRadius: 6,
                        border: '1px solid var(--border)', background: 'var(--bg0)',
                        color: 'var(--text2)',
                      }}>{prettyCombo(c as string)}</kbd>
                    </span>
                  ))}
                </div>
              ))}
            </div>
          </div>
        );
      })}
      <Text type="secondary" style={{ fontSize: '.8em' }}>
        在输入框里打字时，带修饰键的组合照常生效；<kbd>?</kbd> 这类单字符快捷键会让位给输入，
        以免打不出问号。中文输入法组词过程中不会被抢键。
      </Text>
    </Modal>
  );
}
