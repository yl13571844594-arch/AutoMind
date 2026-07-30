// 界面偏好 —— 纯前端、纯本地（localStorage），不经服务端，切换即时生效。
// 与 app.ts 分开：这些是"这台机器上这个人怎么看着舒服"，和 Agent 运行状态无关。
import { create } from 'zustand';

const KEY_FONT = 'automind_font_scale';
const KEY_NOTIFY = 'automind_notify_done';
const KEY_MOTION = 'automind_reduce_motion';

// 字号档位：上下各留两档。
// 基准必须取 16px —— 全站既没给 html 也没给 body 设 font-size，一直吃的是
// 浏览器默认 16px。这里若填 15，"标准"档就会比升级前小 6%，等于偷改了所有
// 老用户的界面。上限 130% 是实测值：再大侧栏文字换行、右侧面板列宽被挤破。
export const FONT_MIN = 0.85;
export const FONT_MAX = 1.3;
export const FONT_BASE_PX = 16;
// antd 组件用的是 px token，不随根字号走，必须同步缩放（默认 14）
export const ANTD_BASE_FONT = 14;

export const FONT_MARKS: Record<number, string> = {
  0.85: '小', 1: '标准', 1.15: '大', 1.3: '特大',
};

function num(key: string, dflt: number, lo: number, hi: number): number {
  const v = parseFloat(localStorage.getItem(key) || '');
  return Number.isFinite(v) && v >= lo && v <= hi ? v : dflt;
}

function bool(key: string, dflt: boolean): boolean {
  const v = localStorage.getItem(key);
  return v === null ? dflt : v === '1';
}

interface PrefsState {
  fontScale: number;
  notifyOnDone: boolean;
  reduceMotion: boolean;
  setFontScale: (v: number) => void;
  setNotifyOnDone: (v: boolean) => void;
  setReduceMotion: (v: boolean) => void;
}

export const usePrefs = create<PrefsState>((set) => ({
  fontScale: num(KEY_FONT, 1, FONT_MIN, FONT_MAX),
  notifyOnDone: bool(KEY_NOTIFY, true),
  reduceMotion: bool(KEY_MOTION, false),

  setFontScale: (v) => {
    const s = Math.min(FONT_MAX, Math.max(FONT_MIN, v));
    localStorage.setItem(KEY_FONT, String(s));
    applyFontScale(s);
    set({ fontScale: s });
  },
  setNotifyOnDone: (v) => {
    localStorage.setItem(KEY_NOTIFY, v ? '1' : '0');
    set({ notifyOnDone: v });
  },
  setReduceMotion: (v) => {
    localStorage.setItem(KEY_MOTION, v ? '1' : '0');
    document.documentElement.classList.toggle('reduce-motion', v);
    set({ reduceMotion: v });
  },
}));

/** 把字号写到根元素。全站尺寸以 em 为主，改根字号即整体等比缩放。 */
export function applyFontScale(scale: number): void {
  document.documentElement.style.fontSize = (FONT_BASE_PX * scale).toFixed(2) + 'px';
}

/** 首屏尽早调用，避免用户看到"先标准字号、后跳成大字号"的闪跳。 */
export function initPrefs(): void {
  const s = usePrefs.getState();
  applyFontScale(s.fontScale);
  document.documentElement.classList.toggle('reduce-motion', s.reduceMotion);
}
