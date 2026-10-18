// 浏览器能力探测（v1.11.3）—— 决定 antd 样式用哪种兼容写法。
//
// 根因：antd 5 的 CSS-in-JS 默认 `hashPriority: 'low'`，每条组件样式都包在
// `:where(.css-xxx)` 里；还大量使用 `inset-inline-start` 这类逻辑属性。
// 不认识 `:where()` 的内核（Chrome / Edge < 88、Safari < 14、Firefox < 78，
// 以及 360 / QQ / 搜狗等国产浏览器的旧 Chromium 内核、被组策略钉住的旧 WebView2）
// 会把**整条规则丢掉**：下拉菜单弹层失去 `position:absolute` 与 `z-index`，
// 渲染到 <body> 末尾的文档流里，而 body 是 `overflow:hidden` —— 于是点「＋」
// 「设置」什么都看不到，弹窗也一样错乱。
//
// 做法（antd 官方兼容方案）：探测到不支持时改用 `hashPriority: 'high'`（不生成
// `:where`）并启用逻辑属性降级转换器。现代浏览器保持默认，不受任何影响。
//
// 手动覆盖（排障用）：localStorage `automind_css_compat` = 'legacy' | 'modern'，
// 或地址栏 `?compat=legacy`。

function probeSelector(sel: string): boolean {
  try {
    document.querySelector(sel);       // 不认识的选择器会抛 SyntaxError
    return true;
  } catch {
    return false;
  }
}

function probeProperty(prop: string, value: string): boolean {
  try {
    if (typeof CSS !== 'undefined' && typeof CSS.supports === 'function') {
      return CSS.supports(prop, value);
    }
  } catch { /* 落到下面 */ }
  try {
    const el = document.createElement('div');
    (el.style as any)[prop.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    return el.style.getPropertyValue(prop) !== '';
  } catch {
    return false;
  }
}

function override(): 'legacy' | 'modern' | '' {
  try {
    const q = new URLSearchParams(location.search).get('compat');
    if (q === 'legacy' || q === 'modern') {
      localStorage.setItem('automind_css_compat', q);
      return q;
    }
    const v = localStorage.getItem('automind_css_compat');
    return v === 'legacy' || v === 'modern' ? v : '';
  } catch {
    return '';
  }
}

const forced = typeof document !== 'undefined' ? override() : '';

export const COMPAT = {
  /** 支持 `:where()`（antd 默认样式能生效的前提） */
  where: forced === 'legacy' ? false : forced === 'modern' ? true : probeSelector(':where(html)'),
  /** 支持 `inset-inline-start` 等逻辑属性 */
  logical: forced === 'legacy' ? false : forced === 'modern' ? true
    : probeProperty('inset-inline-start', '0px'),
};

/** 需要启用 antd 兼容样式模式 */
export const LEGACY_CSS = !COMPAT.where || !COMPAT.logical;

if (typeof document !== 'undefined' && LEGACY_CSS) {
  // 给自家样式表一个钩子：旧内核下需要的少量兜底样式挂在 html.legacy-css 下
  document.documentElement.classList.add('legacy-css');
}
