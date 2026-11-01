// 🔍 HTML 预览（安全沙箱 iframe：不含 allow-same-origin，脚本运行于 null 源）
// 🖼 图片预览（v1.11.19）：图片不能用 iframe —— 服务端此前把 PNG/JPG 当 UTF-8
//    文本读取下发（二进制被解码破坏），弹窗再用 iframe 打开，结果只能是空白/
//    乱码。图片改用 <img> 直接渲染；iframe 只留给 HTML/SVG（它们本身是文档）。
import { Button, Modal } from 'antd';
import { useEffect, useState } from 'react';
import { useUi } from '../../store/ui';

/** 图片扩展名（与后端 _PREVIEW_IMAGE_TYPES 对齐；SVG 是文档，仍走 iframe）。 */
const IMAGE_EXT_RE = /\.(?:png|jpe?g|gif|webp|bmp|ico|avif)(?:$|[?#&])/i;

/** 是否是图片交付物：内联 HTML（如网页截图放大）永远不是图片，仍走 iframe。 */
function isImagePreview(preview: { html?: string; url?: string; label: string }): boolean {
  if (preview.html || !preview.url) return false;
  return IMAGE_EXT_RE.test(preview.url) || IMAGE_EXT_RE.test(preview.label);
}

export default function PreviewModal() {
  const preview = useUi((s) => s.preview);
  const close = useUi((s) => s.closePreview);
  const [imageFailed, setImageFailed] = useState(false);
  // 同一个弹窗会被不同交付物复用：换文件时清掉上一张图的加载失败状态
  useEffect(() => { setImageFailed(false); }, [preview?.url, preview?.html]);
  if (!preview) return null;

  const isImage = isImagePreview(preview);

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
      title={<span>{isImage ? '🖼 图片预览' : '🔍 HTML 预览'} <span className="mono hint-text">{preview.label}</span></span>}
      open onCancel={close} width="86vw" style={{ top: 24 }}
      footer={<><Button onClick={openNewTab}>↗ 新标签打开</Button><Button type="primary" onClick={close}>关闭</Button></>}
    >
      {isImage ? (
        <div
          style={{
            height: '72vh',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            overflow: 'auto',
            border: '1px solid var(--border)',
            borderRadius: 8,
            // 棋盘格底：透明 PNG 也能看清边界，深色主题下不刺眼
            background: 'repeating-conic-gradient(#2c2c2c 0% 25%, #3a3a3a 0% 50%) 50% / 20px 20px',
          }}
        >
          {imageFailed ? (
            <div style={{ textAlign: 'center', padding: 24 }}>
              <div className="hint-text" style={{ marginBottom: 12 }}>
                图片加载失败：可能文件已被移动/删除，或格式不受支持。
              </div>
              <Button onClick={openNewTab}>↗ 新标签打开</Button>
            </div>
          ) : (
            <img
              src={preview.url}
              alt={preview.label}
              referrerPolicy="no-referrer"
              onError={() => setImageFailed(true)}
              style={{
                maxWidth: '100%',
                maxHeight: '72vh',
                objectFit: 'contain',
                display: 'block',
              }}
            />
          )}
        </div>
      ) : (
        <iframe
          className="preview-frame"
          style={{ height: '72vh', border: '1px solid var(--border)' }}
          referrerPolicy="no-referrer"
          sandbox="allow-scripts allow-forms allow-modals"
          {...(preview.html ? { srcDoc: preview.html } : { src: preview.url })}
        />
      )}
    </Modal>
  );
}
