// Markdown → 安全 HTML 渲染器（无第三方依赖，手写轻量子集）。
//
// ── XSS 防线（与经典版一致，任何新语法都不得绕过）──
//   1. 围栏代码块 / 行内代码先抽成占位符（原始内容留在数组里）；
//   2. 全文 esc() 转义 —— 这一步是防线根本：此后文本里不存在可执行的 HTML；
//   3. 行内格式、块级排版只做「文本 → 我们亲手拼出的标签」的替换；
//   4. 代码块最后以 esc() 后的形式还原；链接仅放行 http(s)，图片放行
//      http(s) 与 data:image/*；HTML 代码块不出页面，只给「安全沙箱预览」按钮。
//
// ── 支持范围（有意只做「够用」的子集，不引 marked / react-markdown）──
//   · 标题 h1~h6（标题内行内格式生效）
//   · 段落 / 单换行 <br> / 空行分段
//   · 引用：连续 > 行合并、空行分段、可嵌套
//   · 列表：无序 / 有序 / 2~4 空格缩进嵌套 / 任务列表 [ ] [x] / 项内多行·代码·引用·表格
//   · 表格：对齐 :--- :--: ---:、行内代码与加粗、单元格内 \| 转义、宽表横向滚动
//   · 围栏代码块：语言标签美化、复制按钮、``` 与 ~~~、4 反引号嵌套、未闭合容错
//   · 行内代码 / 粗体 / 斜体 / 删除线 / ==高亮== / [[键盘键]]
//   · 链接 / 图片（懒加载）/ 裸 http(s) URL 自动识别，均带安全 rel
//   · 分隔线；```html 沙箱预览
//   不做：脚注、定义列表、setext 标题、语法高亮、标题锚点。
//
// ── 流式契约 ──
//   splitStream() 返回「已定稿前缀 + 尾巴」，前缀一定在围栏外、空行处切开，
//   因此对每段独立 renderMarkdown 与整段渲染等价；StreamBubble 只增量解析新长出的
//   那一段，总开销 O(总长度)。

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

// ══════════════════════════════════════════════════════════════
// 1. 代码块 / 行内代码：转义前抽出，最后以转义形式还原
// ══════════════════════════════════════════════════════════════

interface Fenced { lang: string; code: string }

function stripQuoteChain(line: string, levels: number): string {
  let s = line;
  for (let k = 0; k < levels; k++) {
    const m = /^[ \t]*>[ \t]?/.exec(s);
    if (!m) break;
    s = s.slice(m[0].length);
  }
  return s;
}

function indentWidth(s: string): number {
  let w = 0;
  for (let k = 0; k < s.length; k++) {
    if (s[k] === ' ') w++;
    else if (s[k] === '\t') w += 4;
    else break;
  }
  return w;
}

function stripIndent(line: string, width: number): string {
  let k = 0;
  let w = 0;
  while (k < line.length && w < width) {
    if (line[k] === ' ') { w++; k++; }
    else if (line[k] === '\t') { w += 4; k++; }
    else break;
  }
  return line.slice(k);
}

// 围栏行：可带缩进，也可带引用前缀（> ``` 里的代码块也要能抽出来）。
const FENCE_OPEN_RE = /^([ \t]*(?:(?:>[ \t]?)+[ \t]*)?)(`{3,}|~{3,})(.*)$/;

function isClosingFence(line: string, ch: string, minLen: number): boolean {
  const t = line.trim();
  if (!t || t[0] !== ch) return false;
  let k = 0;
  while (k < t.length && t[k] === ch) k++;
  return k >= minLen && !t.slice(k).trim();
}

function extractFences(src: string, fenced: Fenced[], htmlBlocks: string[]): string {
  const lines = src.split('\n');
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const m = FENCE_OPEN_RE.exec(lines[i]);
    if (!m) { out.push(lines[i]); i++; continue; }
    const prefix = m[1];
    const fence = m[2];
    const info = m[3].trim();
    // 反引号围栏的 info 里不能再含反引号（CommonMark）；不是围栏就按普通行处理
    if (fence[0] === '`' && info.includes('`')) { out.push(lines[i]); i++; continue; }
    const levels = (prefix.match(/>/g) || []).length;
    const lead = levels ? indentWidth(prefix.slice(0, prefix.indexOf('>'))) : indentWidth(prefix);
    const ch = fence[0];
    const minLen = fence.length;
    let j = i + 1;
    const buf: string[] = [];
    let closed = false;
    while (j < lines.length) {
      // 引用里的代码块：内容行先剥掉引用前缀；普通块再剥掉与开栏行相同的缩进
      let l = stripQuoteChain(lines[j], levels);
      if (!levels) l = stripIndent(l, lead);
      if (isClosingFence(l, ch, minLen)) { closed = true; break; }
      buf.push(l);
      j++;
    }
    const code = buf.join('\n').replace(/^\n+/, '').replace(/\s+$/, '');
    const lang = (info.split(/\s+/)[0] || '');
    // 开栏行的引用前缀原样留在占位符上，块级解析后仍是引用里的一个块
    if (/^html?$/i.test(lang)) {
      htmlBlocks.push(code);
      out.push(prefix + `@@HBLK${htmlBlocks.length - 1}@@`);
    } else {
      fenced.push({ lang, code });
      out.push(prefix + `@@CBLK${fenced.length - 1}@@`);
    }
    i = closed ? j + 1 : j;
  }
  return out.join('\n');
}

function extractInlineCode(src: string, inline: string[]): string {
  // 单反引号 / 双反引号（``a ` b``）都支持；围栏块已在前面抽走
  return src.replace(/(`+)([^`\n]+?)\1/g, (_m, _ticks, code) => {
    inline.push(code);
    return `@@IBLK${inline.length - 1}@@`;
  });
}

const LANG_LABELS: Record<string, string> = {
  js: 'JavaScript', javascript: 'JavaScript', mjs: 'JavaScript', cjs: 'JavaScript',
  ts: 'TypeScript', typescript: 'TypeScript', tsx: 'TSX', jsx: 'JSX',
  py: 'Python', python: 'Python', sh: 'Shell', bash: 'Shell', shell: 'Shell', zsh: 'Shell',
  powershell: 'PowerShell', ps1: 'PowerShell', bat: 'Batch', cmd: 'Batch',
  json: 'JSON', jsonc: 'JSON', yaml: 'YAML', yml: 'YAML', toml: 'TOML', ini: 'INI',
  html: 'HTML', htm: 'HTML', xml: 'XML', svg: 'SVG', css: 'CSS', scss: 'SCSS', less: 'LESS',
  md: 'Markdown', markdown: 'Markdown', sql: 'SQL', go: 'Go', rust: 'Rust', rs: 'Rust',
  java: 'Java', kt: 'Kotlin', kotlin: 'Kotlin', c: 'C', h: 'C', cpp: 'C++', cxx: 'C++',
  cs: 'C#', csharp: 'C#', rb: 'Ruby', ruby: 'Ruby', php: 'PHP', swift: 'Swift', lua: 'Lua',
  dart: 'Dart', dockerfile: 'Dockerfile', nginx: 'Nginx', graphql: 'GraphQL', vue: 'Vue',
  svelte: 'Svelte', diff: 'Diff', patch: 'Diff', http: 'HTTP', text: 'Text', txt: 'Text',
  plain: 'Text', plaintext: 'Text',
};
function langLabel(lang: string): string {
  if (!lang) return 'CODE';
  return LANG_LABELS[lang.toLowerCase()] || lang;
}

function renderCodeBlock(b: Fenced): string {
  const raw = b.lang || '';
  const label = langLabel(raw);
  return `<div class="code-block"${raw ? ` data-lang="${esc(raw)}"` : ''}>`
    + '<div class="code-head"><span class="code-lang">' + esc(label) + '</span>'
    + '<button class="copy-code" type="button" title="复制代码">⧉ 复制</button></div>'
    + `<pre><code>${esc(b.code)}</code></pre></div>`;
}

function renderHtmlBlock(code: string): string {
  return `<div class="html-block"><pre><code>${esc(code)}</code></pre>
      <div class="hb-bar"><button class="hb-preview" data-hblk="${esc(encodeURIComponent(code))}">🔍 预览页面</button>
      <span class="hb-hint">在安全沙箱中渲染</span></div></div>`;
}

// ══════════════════════════════════════════════════════════════
// 2. 行内格式（输入已 esc，输出只含我们拼出的标签）
// ══════════════════════════════════════════════════════════════

// esc() 会把这些字符变成实体：裸 URL 自动识别时遇到它们必须收尾，
// 否则会把后面正文里的 &lt; 之类吞进链接里。&amp; 不算（URL 查询串里合法）。
const ENTITY_STOPS = ['&lt;', '&gt;', '&quot;', '&#39;', '&#96;'];

function isEntityStop(s: string, i: number): boolean {
  for (let k = 0; k < ENTITY_STOPS.length; k++) {
    if (s.startsWith(ENTITY_STOPS[k], i)) return true;
  }
  return false;
}

/** 把一段纯文本（已转义）里的裸 http(s) URL 变成链接。 */
function linkifyText(s: string): string {
  const re = /https?:\/\//gi;
  let out = '';
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s))) {
    const start = m.index;
    // 前面紧贴字母数字或 /（例如 "xhttps://"、已是别的东西的一部分）则不是独立链接
    if (start > 0 && /[A-Za-z0-9_/]/.test(s[start - 1])) continue;
    let j = start;
    while (j < s.length) {
      const ch = s[j];
      if (/\s/.test(ch) || ch === '<' || ch === '>' || ch === '"' || ch === "'" || ch === '`') break;
      if (ch === '&' && isEntityStop(s, j)) break;
      j++;
    }
    let url = s.slice(start, j);
    // 句末标点不属于 URL；不配对的右括号同理（保留 Wikipedia 那种成对的）
    url = url.replace(/[.,;:!?、。，；：！？*_~]+$/, '');
    if (url.endsWith(')')) {
      const open = (url.match(/\(/g) || []).length;
      const close = (url.match(/\)/g) || []).length;
      if (close > open) url = url.slice(0, -1);
    }
    if (url.length <= 8) continue;   // 只有 scheme 没有内容
    out += s.slice(last, start)
      + `<a class="md-link" href="${url}" target="_blank" rel="noopener noreferrer nofollow">${url}</a>`;
    last = start + url.length;
    re.lastIndex = last;
  }
  out += s.slice(last);
  return out;
}

/** 逐段跳过已有标签（尤其 <a>…</a> 内部文本不重复识别）。 */
function autolink(html: string): string {
  let out = '';
  let i = 0;
  const n = html.length;
  while (i < n) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { out += linkifyText(html.slice(i)); break; }
    if (lt > i) out += linkifyText(html.slice(i, lt));
    const gt = html.indexOf('>', lt);
    if (gt < 0) { out += html.slice(lt); break; }
    const tag = html.slice(lt, gt + 1);
    out += tag;
    i = gt + 1;
    if (/^<a[\s>]/i.test(tag)) {
      const end = html.toLowerCase().indexOf('</a>', i);
      if (end >= 0) { out += html.slice(i, end + 4); i = end + 4; }
    }
  }
  return out;
}

/** 行内格式：输入是**已转义**的文本，返回可插入 DOM 的 HTML。 */
function inlineFormat(text: string): string {
  return autolink(
    text
      // URL 允许一层括号嵌套（Wikipedia 风格链接 / 带括号的图片文件名）
      .replace(/!\[([^\]]*)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)/g, (m, alt, url) => (isSafeUrl(url)
        ? `<img class="mm" src="${url}" alt="${alt}" loading="lazy" decoding="async">`
        : m))
      .replace(/\[([^\]]+)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)/g, (m, label, url) => (isSafeHref(url)
        ? `<a class="md-link" href="${url}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`
        : m))
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/~~(.+?)~~/g, '<del>$1</del>')
      .replace(/==(.+?)==/g, '<mark>$1</mark>')
      .replace(/\[\[([^\[\]\n]{1,40})\]\]/g, '<kbd>$1</kbd>')
      .replace(/(^|[^*<\w])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>'),
  );
}

// ══════════════════════════════════════════════════════════════
// 3. 块级排版（输入是已 esc 的文本；叶节点再走 inlineFormat）
// ══════════════════════════════════════════════════════════════

const RE_HEADING = /^(#{1,6})[ \t]+(.*)$/;
const RE_HR = /^[ \t]*(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const RE_QUOTE = /^[ \t]*(?:&gt;[ \t]?)(.*)$/;
const RE_PLACEHOLDER = /^[ \t]*@@[CH]BLK\d+@@[ \t]*$/;

interface ListItemMatch {
  ordered: boolean;
  indent: number;
  markerLen: number;
  rest: string;
}

function matchListItem(line: string): ListItemMatch | null {
  let m = /^([ \t]*)([-*+•])[ \t]+(.*)$/.exec(line);
  if (m) {
    return { ordered: false, indent: indentWidth(m[1]), markerLen: 1, rest: m[3] };
  }
  m = /^([ \t]*)(\d{1,9})[.)、][ \t]+(.*)$/.exec(line);
  if (m) {
    return { ordered: true, indent: indentWidth(m[1]), markerLen: m[2].length + 1, rest: m[3] };
  }
  return null;
}

/** 按未转义的 | 切单元格；\| 还原为字面竖线（内容此时已转义，不会引入 HTML）。 */
function mdCells(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = '';
  for (let k = 0; k < s.length; k++) {
    const ch = s[k];
    if (ch === '\\' && s[k + 1] === '|') { cur += '|'; k++; continue; }
    if (ch === '|') { cells.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

function parseAlign(cell: string): 'l' | 'c' | 'r' | '' | null {
  if (!/^:?-+:?$/.test(cell)) return null;
  const left = cell.charAt(0) === ':';
  const right = cell.charAt(cell.length - 1) === ':';
  if (left && right) return 'c';
  if (left) return 'l';
  if (right) return 'r';
  return '';
}

function parseTableAligns(line: string): Array<'l' | 'c' | 'r' | ''> | null {
  if (!line.includes('|')) return null;
  const cells = mdCells(line);
  if (!cells.length) return null;
  const out: Array<'l' | 'c' | 'r' | ''> = [];
  for (let k = 0; k < cells.length; k++) {
    const a = parseAlign(cells[k]);
    if (a === null) return null;
    out.push(a);
  }
  return out;
}

function alignClass(a: 'l' | 'c' | 'r' | ''): string {
  return a ? ` md-al-${a}` : '';
}

function tryTable(lines: string[], i: number): { html: string; next: number } | null {
  const headLine = lines[i];
  if (!headLine.includes('|')) return null;
  if (i + 1 >= lines.length) return null;
  const aligns = parseTableAligns(lines[i + 1]);
  if (!aligns) return null;
  const head = mdCells(headLine);
  if (head.length !== aligns.length) return null;
  const rows: string[][] = [];
  let j = i + 2;
  while (j < lines.length) {
    const l = lines[j];
    if (!l.trim() || !l.includes('|')) break;
    // 表格到这里就该结束了：不要吞掉后面紧跟的块（标题 / 列表 / 引用 / 新表头）
    if (RE_PLACEHOLDER.test(l) || RE_HEADING.test(l) || RE_QUOTE.test(l)
        || matchListItem(l) || parseTableAligns(l)) break;
    rows.push(mdCells(l));
    j++;
  }
  const headHtml = head.map((c, k) =>
    `<th class="md-th${alignClass(aligns[k])}">${inlineFormat(c)}</th>`).join('');
  const bodyHtml = rows.map((r) => '<tr>'
    + head.map((_, k) => `<td class="md-td${alignClass(aligns[k])}">${inlineFormat(r[k] || '')}</td>`).join('')
    + '</tr>').join('');
  return {
    html: '<div class="md-tbl-wrap"><table class="md-table">'
      + `<thead><tr>${headHtml}</tr></thead>`
      + `<tbody>${bodyHtml}</tbody></table></div>`,
    next: j,
  };
}

function isTableStart(lines: string[], idx: number): boolean {
  if (!lines[idx] || !lines[idx].includes('|')) return false;
  if (idx + 1 >= lines.length) return false;
  const aligns = parseTableAligns(lines[idx + 1]);
  if (!aligns) return false;
  return mdCells(lines[idx]).length === aligns.length;
}

function isBlockStart(lines: string[], idx: number): boolean {
  const line = lines[idx];
  if (!line.trim()) return true;
  return RE_PLACEHOLDER.test(line) || RE_HEADING.test(line) || RE_HR.test(line)
    || RE_QUOTE.test(line) || !!matchListItem(line) || isTableStart(lines, idx);
}

function parseQuote(lines: string[], i: number, depth: number): { html: string; next: number } {
  const n = lines.length;
  const buf: string[] = [];
  let j = i;
  while (j < n) {
    const m = RE_QUOTE.exec(lines[j]);
    if (m) { buf.push(m[1]); j++; continue; }
    if (!lines[j].trim()) {
      // 空行：后面若还是引用行，则并入（空行分段）；否则引用结束
      let k = j;
      while (k < n && !lines[k].trim()) k++;
      if (k < n && RE_QUOTE.test(lines[k])) {
        for (let b = j; b < k; b++) buf.push('');
        j = k;
        continue;
      }
    }
    break;
  }
  const inner = buf.length ? parseBlocks(buf, depth + 1) : '';
  return { html: `<blockquote class="md-quote">${inner}</blockquote>`, next: j };
}

function parseList(lines: string[], i: number, depth: number): { html: string; next: number } {
  const n = lines.length;
  const first = matchListItem(lines[i]);
  if (!first) return { html: inlineFormat(lines[i].trim()), next: i + 1 };
  const baseIndent = first.indent;
  const ordered = first.ordered;
  const startNo = ordered ? parseInt(/^[ \t]*(\d{1,9})/.exec(lines[i])![1], 10) : 1;
  const items: string[][] = [];
  let j = i;
  while (j < n) {
    const cur = matchListItem(lines[j]);
    if (!cur || cur.indent !== baseIndent || cur.ordered !== ordered) break;
    const contentIndent = baseIndent + cur.markerLen + 1;
    const itemLines: string[] = [cur.rest];
    j++;
    while (j < n) {
      const l = lines[j];
      if (!l.trim()) {
        // 空行：后面仍属于本项（更深缩进 / 同层下一项）才保留，否则列表结束
        let k = j;
        while (k < n && !lines[k].trim()) k++;
        if (k >= n) { j = k; break; }
        const nm = matchListItem(lines[k]);
        const belongs = (!!nm && nm.indent === baseIndent && nm.ordered === ordered)
          || indentWidth(lines[k]) > baseIndent
          || RE_PLACEHOLDER.test(lines[k]);
        if (belongs) {
          for (let b = j; b < k; b++) itemLines.push('');
          j = k;
          continue;
        }
        break;
      }
      const nm = matchListItem(l);
      if (nm && nm.indent <= baseIndent) break;   // 同层下一项 / 回到外层
      const ind = indentWidth(l);
      if (nm || ind > baseIndent) {
        itemLines.push(stripIndent(l, Math.min(ind, contentIndent)));
        j++;
        continue;
      }
      // 懒续行：紧跟上一行、且不是新块起始，归入本项（CommonMark 行为）
      if (!isBlockStart(lines, j)) {
        itemLines.push(l.trim());
        j++;
        continue;
      }
      break;
    }
    items.push(itemLines);
  }

  let html = '';
  for (let k = 0; k < items.length; k++) {
    const itemLines = items[k];
    let task: boolean | null = null;
    if (!ordered && itemLines.length) {
      const tm = /^\[([ xX])\][ \t]+([\s\S]*)$/.exec(itemLines[0]);
      if (tm) { task = tm[1].toLowerCase() === 'x'; itemLines[0] = tm[2]; }
    }
    let inner = itemLines.join('\n').trim() ? parseBlocks(itemLines, depth + 1) : '';
    if (task !== null) {
      // checkbox 要落在第一段文字的行内（否则块级 div 会把文字挤到下一行）
      const box = `<span class="md-task-box${task ? ' done' : ''}" aria-hidden="true"></span>`;
      const firstP = '<div class="md-p">';
      inner = inner.indexOf(firstP) === 0
        ? firstP + box + inner.slice(firstP.length)
        : box + inner;
    }
    html += `<li class="md-li${task === null ? '' : ' md-task'}">${inner}</li>`;
  }
  const open = ordered
    ? `<ol class="md-list md-ol"${startNo !== 1 ? ` start="${startNo}"` : ''}>`
    : '<ul class="md-list md-ul">';
  return { html: open + html + (ordered ? '</ol>' : '</ul>'), next: j };
}

/**
 * 块级解析。depth 只用于防御病态输入的递归爆炸（几十层嵌套引用）。
 * 输入 text 必须已 esc；输出由占位符、纯文本与下列标签组成。
 */
function parseBlocks(lines: string[], depth: number): string {
  if (depth > 10) {
    // 超深嵌套：不再细分工整结构，整段按纯文本段落输出（内容已转义，安全）
    return lines.filter((l) => l.trim()).map((l) =>
      `<div class="md-p">${inlineFormat(l.trim())}</div>`).join('');
  }
  const out: string[] = [];
  const n = lines.length;
  let i = 0;
  while (i < n) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (RE_PLACEHOLDER.test(line)) { out.push(line.trim()); i++; continue; }
    const tbl = tryTable(lines, i);
    if (tbl) { out.push(tbl.html); i = tbl.next; continue; }
    if (RE_HR.test(line)) { out.push('<div class="md-hr" role="separator"></div>'); i++; continue; }
    const h = RE_HEADING.exec(line);
    if (h) {
      const lv = h[1].length;
      out.push(`<h${lv} class="md-h md-h${lv}">${inlineFormat(h[2].trim())}</h${lv}>`);
      i++;
      continue;
    }
    if (RE_QUOTE.test(line)) {
      const q = parseQuote(lines, i, depth);
      out.push(q.html);
      i = q.next;
      continue;
    }
    if (matchListItem(line)) {
      const l = parseList(lines, i, depth);
      out.push(l.html);
      i = l.next;
      continue;
    }
    // 段落：连续普通行合成一段，行内换行保留为 <br>（空行才是分段边界）
    const para: string[] = [];
    while (i < n) {
      const l = lines[i];
      if (!l.trim()) break;
      if (para.length && isBlockStart(lines, i)) break;
      para.push(l.trim());
      i++;
    }
    if (para.length) {
      out.push(`<div class="md-p">${para.map((l) => inlineFormat(l)).join('<br>')}</div>`);
    }
  }
  return out.join('');
}

// ══════════════════════════════════════════════════════════════
// 4. 对外 API
// ══════════════════════════════════════════════════════════════

const SEP = String.fromCharCode(10, 10);   // 空行 = 块边界

/**
 * 把流式缓冲区切成「已定稿的前缀」和「还在长的尾巴」。
 *
 * 前缀只在**跨过一个块边界**时才增量解析一次（见 StreamBubble 的累积渲染）；
 * 尾巴按纯文本原样显示 —— 转义一下就完事，几乎不花时间。
 *
 * 边界取最后一个"围栏外"的空行：若切在 ``` 中间，前缀会渲染出一个永远闭合不上的
 * 代码块。单次线性扫描（不再对每个候选空行重复数围栏），避免长回答 O(n²)。
 */
export function splitStream(buf: string): [prefix: string, tail: string] {
  // 短回答整体解析就够快，不值得为它引入"两段拼接"的额外复杂度
  if (buf.length < 600) return ['', buf];
  const lines = buf.split('\n');
  let pos = 0;
  let lastCut = -1;
  let open: { ch: string; len: number } | null = null;
  for (let k = 0; k < lines.length; k++) {
    const line = lines[k];
    const m = /^[ \t]*(?:(?:>[ \t]?)+[ \t]*)?(`{3,}|~{3,})(.*)$/.exec(line);
    if (m) {
      const ch = m[1].charAt(0);
      const len = m[1].length;
      const info = m[2];
      if (!open) {
        if (!(ch === '`' && info.includes('`'))) open = { ch, len };
      } else if (ch === open.ch && len >= open.len && !info.trim()) {
        open = null;
      }
    }
    if (!line.trim() && pos > 0 && !open) lastCut = pos;
    pos += line.length + 1;
  }
  if (lastCut <= 0) return ['', buf];
  return [buf.slice(0, lastCut), buf.slice(lastCut)];
}

/** Markdown → 安全 HTML（对话气泡 / 执行轨迹 / 文档视图共用）。 */
export function renderMarkdown(text: string): string {
  const src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
  if (!src) return '';
  const fenced: Fenced[] = [];
  const htmlBlocks: string[] = [];
  const inline: string[] = [];
  // 1) 先抽代码（围栏 → 行内），代码内容不参与任何后续解析
  let t = extractFences(src, fenced, htmlBlocks);
  t = extractInlineCode(t, inline);
  // 2) 全文转义 —— XSS 防线的根，后续新增语法只能在这之后拼标签
  t = esc(t);
  // 3) 块级排版（叶子节点内部做行内格式）
  t = parseBlocks(t.split('\n'), 0);
  // 4) 还原代码块（内容再转义一次；占位符全部消失）
  t = t.replace(/@@CBLK(\d+)@@/g, (_m, i) => {
    const b = fenced[Number(i)];
    return b ? renderCodeBlock(b) : '';
  });
  t = t.replace(/@@IBLK(\d+)@@/g, (_m, i) => {
    const code = inline[Number(i)];
    return code == null ? '' : `<code>${esc(code)}</code>`;
  });
  t = t.replace(/@@HBLK(\d+)@@/g, (_m, i) => renderHtmlBlock(htmlBlocks[Number(i)] || ''));
  return t;
}
