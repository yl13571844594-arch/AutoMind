// Markdown → 安全 HTML 渲染器（从经典版 chat.js 移植，XSS 防线不变：
// 全文先 esc() 转义，代码块原文抽出后再以转义形式还原；链接仅放行 http(s)，
// 图片放行 http(s) 与 data:image/*；HTML 代码块渲染为「沙箱预览」按钮）。

const ESC_MAP: Record<string, string> = {
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;',
};
export function esc(t: unknown): string {
  return (t == null ? '' : String(t)).replace(/[&<>"'`]/g, (c) => ESC_MAP[c]);
}
export function isSafeUrl(u: string): boolean {
  return /^(https?:\/\/|data:image\/)/i.test(u || '');
}
function isSafeHref(u: string): boolean {
  return /^https?:\/\//i.test(u || '');
}

const MD_TBL_SEP = /^\s*\|?[\s:|-]+\|?\s*$/;
function mdCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

function renderMdBlocks(text: string): string {
  const lines = String(text).split('\n');
  const out: string[] = [];
  let lastWasBlock = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*\|.+\|\s*$/.test(line) && i + 1 < lines.length
        && MD_TBL_SEP.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      const head = mdCells(line);
      let j = i + 2;
      const rows: string[][] = [];
      while (j < lines.length && /^\s*\|.+\|\s*$/.test(lines[j])) { rows.push(mdCells(lines[j])); j++; }
      out.push('<div class="md-tbl-wrap"><table class="md-table"><thead><tr>'
        + head.map((c) => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
        + rows.map((r) => '<tr>' + head.map((_, k) => `<td>${r[k] != null ? r[k] : ''}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>');
      i = j - 1; lastWasBlock = true; continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push('<div class="md-hr"></div>'); lastWasBlock = true; continue; }
    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if (h) { out.push(`<div class="md-h md-h${h[1].length}">${h[2]}</div>`); lastWasBlock = true; continue; }
    if (/^&gt;\s?/.test(line)) {
      const q: string[] = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) { q.push(lines[i].replace(/^&gt;\s?/, '')); i++; }
      i--;
      out.push(`<div class="md-quote">${q.join('<br>')}</div>`); lastWasBlock = true; continue;
    }
    if (/^\s*[-•]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-•]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-•]\s+/, '')); i++; }
      i--;
      out.push(`<ul class="md-list">${items.map((x) => `<li>${x}</li>`).join('')}</ul>`); lastWasBlock = true; continue;
    }
    if (/^\s*\d+[.、)]\s+/.test(line)) {
      const items: string[] = [];
      const start = parseInt((line.match(/^\s*(\d+)/) || [])[1] || '1', 10) || 1;
      while (i < lines.length && /^\s*\d+[.、)]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+[.、)]\s+/, '')); i++; }
      i--;
      out.push(`<ol class="md-list" start="${start}">${items.map((x) => `<li>${x}</li>`).join('')}</ol>`); lastWasBlock = true; continue;
    }
    if (line === '' && lastWasBlock) { lastWasBlock = false; continue; }
    out.push(line + '<br>');
    lastWasBlock = false;
  }
  return out.join('').replace(/(<br>)+$/, '');
}

/** Markdown → 安全 HTML（含代码块 / HTML 沙箱预览按钮 / 表格 / 列表）。 */
export function renderMarkdown(text: string): string {
  const htmlBlocks: string[] = [];
  const codeBlocks: { lang: string; code: string }[] = [];
  let t = text || '';
  t = t.replace(/```html\r?\n?([\s\S]*?)```/gi, (_, code) => {
    const i = htmlBlocks.push(code.trim()) - 1;
    return `@@HBLK${i}@@`;
  });
  t = t.replace(/```(\w*)\r?\n?([\s\S]*?)```/g, (_, lang, code) => {
    const i = codeBlocks.push({ lang: lang || 'code', code: code.trim() }) - 1;
    return `@@CBLK${i}@@`;
  });
  t = t.replace(/`([^`]+)`/g, (_, code) => {
    const i = codeBlocks.push({ lang: '', code }) - 1;
    return `@@IBLK${i}@@`;
  });
  t = esc(t)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
      isSafeUrl(url) ? `<img class="mm" src="${esc(url)}" alt="${esc(alt)}" loading="lazy">` : m)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, url) =>
      isSafeHref(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="md-link">${txt}</a>` : m)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/~~(.+?)~~/g, '<del>$1</del>')
    .replace(/(^|[^*<\w])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>');
  t = renderMdBlocks(t);
  t = t.replace(/@@CBLK(\d+)@@/g, (_, i) => {
    const b = codeBlocks[Number(i)] || { lang: '', code: '' };
    return `<div class="code-block"><div class="code-head">${b.lang}<button class="copy-code" title="复制代码">⧉ 复制</button></div><pre><code>${esc(b.code)}</code></pre></div>`;
  });
  t = t.replace(/@@IBLK(\d+)@@/g, (_, i) => {
    const b = codeBlocks[Number(i)] || { code: '' };
    return `<code>${esc(b.code)}</code>`;
  });
  t = t.replace(/@@HBLK(\d+)@@/g, (_, i) => {
    const code = htmlBlocks[Number(i)] || '';
    return `<div class="html-block"><pre><code>${esc(code)}</code></pre>
      <div class="hb-bar"><button class="hb-preview" data-hblk="${esc(encodeURIComponent(code))}">🔍 预览页面</button>
      <span class="hb-hint">在安全沙箱中渲染</span></div></div>`;
  });
  return t;
}
