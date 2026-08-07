// 复制到剪贴板。
//
// 不能只依赖 navigator.clipboard：它要求安全上下文（HTTPS 或 localhost），
// 而本应用常被用户从局域网另一台机器以 http://192.168.x.x:8000 打开 ——
// 那种情况下 navigator.clipboard 直接是 undefined，"复制"点了没反应。
// 故失败时回退到早期的 execCommand('copy') 路径。
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* 落到下面的回退路径 */ }

  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    // 必须留在视口内且可聚焦，否则部分浏览器不执行复制；用定位+透明藏起来
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
    ta.setAttribute('readonly', '');
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
