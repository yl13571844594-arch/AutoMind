// 弹层可见性保护（v1.11.8）—— SafeDropdown / SafeSelect / SafePopconfirm 共用。
//
// antd 的下拉、选择框、确认气泡都是"渲染到 body 末尾 + CSS-in-JS 定位 + 进出场动画"。
// 部分电脑（旧内核、国产浏览器兼容模式、被组策略钉住的 WebView2、显卡加速异常）上
// 任何一环出问题，弹层就"打开了却看不见"：点「⋯」、点下拉框都像没反应。
//
// 统一策略：
//   · 已知兼容性不足（lib/compat 判定）或用户/自检记住过 → 直接用不依赖 antd 弹层的兼容控件；
//   · 否则照常用 antd，打开后 450ms 自检弹层是否真的可见；不可见 → 记住并当场换成兼容控件。
// 排障：地址栏 ?menu=simple 强制兼容控件，?menu=antd 恢复（会记住）。
import { LEGACY_CSS } from './compat';

export const FALLBACK_FLAG = 'automind_menu_fallback';

let forcedCache: boolean | null = null;

export function fallbackForced(): boolean {
  if (forcedCache !== null) return forcedCache;
  let v = LEGACY_CSS;
  try {
    const q = new URLSearchParams(location.search).get('menu');
    if (q === 'simple') { localStorage.setItem(FALLBACK_FLAG, '1'); v = true; }
    else if (q === 'antd') { localStorage.removeItem(FALLBACK_FLAG); v = false; }
    else if (!v) v = localStorage.getItem(FALLBACK_FLAG) === '1';
  } catch { /* 隐私模式等：按默认 */ }
  forcedCache = v;
  return v;
}

const listeners = new Set<() => void>();

/** 自检发现弹层看不见：记住，并通知所有同类控件一起切到兼容模式 */
export function markFallback(what: string): void {
  if (forcedCache) return;
  forcedCache = true;
  try { localStorage.setItem(FALLBACK_FLAG, '1'); } catch { /* ignore */ }
  // eslint-disable-next-line no-console
  console.warn(`[AutoMind] ${what}在当前浏览器中不可见，已切换为兼容控件（?menu=antd 可恢复）`);
  listeners.forEach((fn) => { try { fn(); } catch { /* ignore */ } });
}

export function onFallback(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

const HIDDEN_CLASSES = ['ant-dropdown-hidden', 'ant-select-dropdown-hidden', 'ant-popover-hidden',
  'ant-popconfirm-hidden', 'ant-tooltip-hidden'];

/** 带某个 className 的弹层是否真的被看见：在视口内、有尺寸、不透明、未隐藏。 */
export function popupVisible(cls: string): boolean {
  const els = document.querySelectorAll<HTMLElement>(`.${cls}`);
  for (let i = 0; i < els.length; i += 1) {
    const el = els[i];
    if (HIDDEN_CLASSES.some((h) => el.classList.contains(h))) continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const inView = r.width > 20 && r.height > 10 && r.bottom > 0 && r.right > 0
      && r.top < window.innerHeight && r.left < window.innerWidth;
    if (inView && st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity || '1') > 0.3) {
      return true;
    }
  }
  return false;
}

/** 打开后 450ms 检查：仍处于打开状态却看不见 → 回调 */
export function probeAfterOpen(cls: string, stillOpen: () => boolean, onInvisible: () => void): void {
  window.setTimeout(() => {
    if (stillOpen() && !popupVisible(cls)) onInvisible();
  }, 450);
}

let seq = 0;
export const probeClass = (prefix: string) => `${prefix}-probe-${++seq}`;

/**
 * requestAnimationFrame 是否"停摆"（v1.11.8 实测根因）。
 *
 * antd 的弹层（下拉、选择框、确认气泡、弹窗）出场动画由 rc-motion 驱动，它靠 rAF 推进
 * "prepare → start → active"。部分电脑上页面明明可见，rAF 却一帧都不来（窗口被遮挡 /
 * 显卡加速异常 / 某些 WebView2 与远程桌面环境）—— 弹层永远停在 `*-appear-prepare`、
 * opacity:0，表现就是"点了没反应"。实测复现：可见页面 500ms 内 rAF 0 帧，弹层卡在 prepare。
 *
 * 判据：页面可见时，400ms 内一帧都没有 → 停摆。调用方据此关闭 antd 动画（motion=false），
 * 弹层不再依赖 rAF，直接显示。
 */
export function detectRafStall(windowMs = 400): Promise<boolean> {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame !== 'function') { resolve(true); return; }
    if (document.visibilityState !== 'visible') { resolve(false); return; }   // 后台标签页本来就不刷帧
    let frames = 0;
    let done = false;
    const tick = () => { frames += 1; if (!done) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    window.setTimeout(() => {
      done = true;
      resolve(frames === 0 && document.visibilityState === 'visible');
    }, windowMs);
  });
}
