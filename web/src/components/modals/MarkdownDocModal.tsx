// 📄 Markdown 文档视图（v1.11.14）
//
// 背景：Agent 产出的 .md 交付物点开时走 `/api/preview/file` iframe，浏览器按
// text/plain 显示**原始源码** —— 表格糊成竖线、标题全是井号。这里把它换成真正的
// 文档阅读视图：居中 A4 宽度、目录侧栏、代码块带语言栏与复制、顶部工具条可切源码。
//
// 数据来源仍是现有只读接口（不新增后端）：`GET /api/preview/file?path=...` 对 .md
// 返回 text/plain 原文，由 lib/markdown 的 renderMarkdown 渲染（同一套 XSS 防线）。
import { App, Button, Modal, Spin, Tooltip } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiGetText, errText } from '../../api/client';
import { copyText } from '../../lib/clipboard';
import { renderMarkdown } from '../../lib/markdown';
import { useUi } from '../../store/ui';

/** 单次渲染上限：再大只渲染前面一段（源码模式仍可看全文）；避免大文档把页面卡死 */
const MAX_RENDER = 1200 * 1000;
/** 源码模式上限（超过只显示前 2MB，其余请用「新标签打开」看接口原文） */
const MAX_SOURCE = 2000 * 1000;
/** 文档读盘超时：服务端是同步读文件，正常很快；卡住要给出可操作的提示 */
const LOAD_TIMEOUT = 30000;

interface TocItem { level: number; text: string; index: number }

/**
 * 从渲染后的 HTML 里提取目录。
 *
 * 为什么不在 Markdown 源文本上正则：目录必须与**实际渲染出的标题**一一对应
 * （代码块/引用里的 # 不算标题），从渲染结果反查最可靠；index 即
 * `article.querySelectorAll('.md-h')` 的下标，点击时据此定位。
 */
function buildToc(html: string): TocItem[] {
  if (!html || typeof DOMParser === 'undefined') return [];
  try {
    const parsed = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html');
    const els = parsed.querySelectorAll('.md-h');
    const out: TocItem[] = [];
    for (let i = 0; i < els.length; i++) {
      const el = els[i];
      const m = /md-h([1-6])/.exec(el.className || '');
      const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text) continue;
      out.push({ level: m ? Number(m[1]) : 2, text, index: i });
    }
    return out;
  } catch {
    return [];
  }
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} 字符`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export default function MarkdownDocModal() {
  const doc = useUi((s) => s.doc);
  const close = useUi((s) => s.closeDoc);
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [content, setContent] = useState('');
  const [mode, setMode] = useState<'doc' | 'source'>('doc');
  const [reloadKey, setReloadKey] = useState(0);
  const articleRef = useRef<HTMLDivElement>(null);

  const path = doc?.path || '';

  // 每次打开 / 重新加载都现读：文件内容随时可能被 Agent 改掉，缓存只会让人看到旧版本
  useEffect(() => {
    if (!doc) return;
    let alive = true;
    setLoading(true);
    setError('');
    setContent('');
    setMode('doc');
    apiGetText(`/preview/file?path=${encodeURIComponent(doc.path)}`, LOAD_TIMEOUT)
      .then((text) => { if (alive) setContent(String(text == null ? '' : text)); })
      .catch((e) => { if (alive) setError(errText(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // 只认 path / nonce：同一路径点两次（nonce 变化）也要重新拉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.path, doc?.nonce, reloadKey]);

  const shown = content.length > MAX_RENDER ? content.slice(0, MAX_RENDER) : content;
  const truncated = content.length > MAX_RENDER;
  const srcShown = content.length > MAX_SOURCE ? content.slice(0, MAX_SOURCE) : content;
  const html = useMemo(() => (mode === 'doc' ? renderMarkdown(shown) : ''), [shown, mode]);
  const toc = useMemo(() => (mode === 'doc' ? buildToc(html) : []), [html, mode]);
  const showToc = toc.length >= 3;

  const copyMarkdown = useCallback(async () => {
    const ok = await copyText(content);
    ok ? message.success('已复制 Markdown 原文') : message.error('复制失败');
  }, [content, message]);

  const copyPath = useCallback(async () => {
    const ok = await copyText(path);
    ok ? message.success('已复制文件路径') : message.error('复制失败');
  }, [path, message]);

  const openRaw = useCallback(() => {
    // 新标签打开接口原文（对 .md 是 text/plain）—— 文档超限时也总能看到全文
    window.open(`/api/preview/file?path=${encodeURIComponent(path)}`, '_blank', 'noopener');
  }, [path]);

  const tocGo = useCallback((index: number) => {
    const root = articleRef.current;
    if (!root) return;
    const els = root.querySelectorAll<HTMLElement>('.md-h');
    const el = els[index];
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  // 代码块「复制」按钮：委托处理（渲染的是 innerHTML，无法挂 React onClick）
  const onPaperClick = useCallback(async (e: React.MouseEvent<HTMLElement>) => {
    const target = e.target as HTMLElement;
    const btn = target.closest('.copy-code');
    if (!btn) return;
    const pre = btn.closest('.code-block')?.querySelector('pre');
    if (!pre) return;
    const ok = await copyText(pre.textContent || '');
    ok ? message.success('已复制代码') : message.error('复制失败');
  }, [message]);

  return (
    <Modal
      open={!!doc}
      onCancel={close}
      width="92vw"
      style={{ top: 16, maxWidth: 1320 }}
      className="mdoc-modal"
      destroyOnHidden
      footer={null}
      title={(
        <span className="mdoc-title">
          📄 文档预览
          <span className="mono hint-text mdoc-title-path" title={path}>{doc?.label || path}</span>
        </span>
      )}
      styles={{ body: { padding: 0 } }}
    >
      <div className="mdoc-toolbar">
        <div className="mdoc-toolbar-main">
          <span className="mdoc-path mono" title={path}>{path}</span>
          {!loading && !error && content && (
            <span className="hint-text mdoc-meta">
              {fmtSize(content.length)}
              {truncated ? ' · 过长，仅渲染前 1.2MB' : ''}
            </span>
          )}
        </div>
        <div className="mdoc-acts">
          <button
            type="button"
            className={'mdoc-tab' + (mode === 'doc' ? ' on' : '')}
            onClick={() => setMode('doc')}
          >📖 文档</button>
          <button
            type="button"
            className={'mdoc-tab' + (mode === 'source' ? ' on' : '')}
            onClick={() => setMode('source')}
          >📝 源码</button>
          <span className="mdoc-sep" />
          <button type="button" className="mdoc-btn" disabled={!content} onClick={copyMarkdown}
            title="复制 Markdown 原文">⧉ 复制</button>
          <button type="button" className="mdoc-btn" onClick={copyPath} title="复制文件路径">⧉ 路径</button>
          <button type="button" className="mdoc-btn" onClick={openRaw}
            title="在新标签页打开接口原文（text/plain）">↗ 新标签</button>
          <button type="button" className="mdoc-btn" disabled={loading}
            onClick={() => setReloadKey((n) => n + 1)} title="重新读取文件">⟳ 刷新</button>
          <Button size="small" type="primary" onClick={close}>关闭</Button>
        </div>
      </div>

      {loading ? (
        <div className="mdoc-state">
          <Spin />
          <div className="hint-text">正在读取文档…</div>
        </div>
      ) : error ? (
        <div className="mdoc-state mdoc-error">
          <div className="mdoc-error-ico">📄</div>
          <div className="mdoc-error-title">无法打开这个 Markdown 文件</div>
          <div className="mdoc-error-msg">{error}</div>
          <div className="mdoc-error-acts">
            <Button size="small" onClick={() => setReloadKey((n) => n + 1)}>重试</Button>
            <Button size="small" onClick={openRaw}>↗ 新标签打开原文</Button>
          </div>
          <div className="hint-text">
            常见原因：文件不存在 / 在项目目录之外 / 超出可预览范围 / 服务端读取失败。
          </div>
        </div>
      ) : !content.trim() ? (
        <div className="mdoc-state"><div className="hint-text">这个文件是空的（0 字节）。</div></div>
      ) : mode === 'source' ? (
        <div className="mdoc-scroll mdoc-scroll-src">
          {content.length > MAX_SOURCE && (
            <div className="mdoc-warn">文档过长，只显示前 2MB；完整原文请点「↗ 新标签」。</div>
          )}
          <pre className="mdoc-src"><code>{srcShown}</code></pre>
        </div>
      ) : (
        <div className="mdoc-body">
          {truncated && (
            <div className="mdoc-warn">
              文档较长（{fmtSize(content.length)}），这里只渲染前 1.2MB；完整原文请切「📝 源码」或「↗ 新标签」。
            </div>
          )}
          {showToc && (
            <aside className="mdoc-toc">
              <div className="mdoc-toc-title">目录</div>
              <div className="mdoc-toc-list">
                {toc.map((t) => (
                  <Tooltip key={t.index} title={t.text} placement="right">
                    <button
                      type="button"
                      className={'mdoc-toc-item lv' + t.level}
                      style={{ paddingLeft: 8 + (t.level - 1) * 12 }}
                      onClick={() => tocGo(t.index)}
                    >{t.text}</button>
                  </Tooltip>
                ))}
              </div>
            </aside>
          )}
          <div className="mdoc-scroll">
            <article
              ref={articleRef}
              className="mdoc-paper md"
              onClick={onPaperClick}
              dangerouslySetInnerHTML={{ __html: html }}
            />
          </div>
        </div>
      )}
    </Modal>
  );
}
