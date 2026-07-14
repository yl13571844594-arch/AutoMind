// 🔍 HTML 预览（安全沙箱 iframe：不含 allow-same-origin，脚本运行于 null 源）
import { Button, Modal } from 'antd';
import { useUi } from '../../store/ui';

export default function PreviewModal() {
  const preview = useUi((s) => s.preview);
  const close = useUi((s) => s.closePreview);
  if (!preview) return null;

  const openNewTab = () => {
    if (preview.html) {
      const blob = new Blob([preview.html], { type: 'text/html' });
      window.open(URL.createObjectURL(blob), '_blank');
    } else if (preview.url) {
      window.open(preview.url, '_blank');
    }
  };

  return (
    <Modal
      title={<span>🔍 HTML 预览 <span className="mono hint-text">{preview.label}</span></span>}
      open onCancel={close} width="86vw" style={{ top: 24 }}
      footer={<><Button onClick={openNewTab}>↗ 新标签打开</Button><Button type="primary" onClick={close}>关闭</Button></>}
    >
      <iframe
        className="preview-frame"
        style={{ height: '72vh', border: '1px solid var(--border)' }}
        referrerPolicy="no-referrer"
        sandbox="allow-scripts allow-forms allow-modals"
        {...(preview.html ? { srcDoc: preview.html } : { src: preview.url })}
      />
    </Modal>
  );
}
