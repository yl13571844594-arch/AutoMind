// 全局快捷键 —— 单一注册表，既驱动实际按键，也驱动帮助弹窗的展示。
// 两者同源，改一处即同步，不会出现"帮助里写着但按了没反应"。

export interface Hotkey {
  /** 组合键描述，mod = Windows/Linux 的 Ctrl、macOS 的 ⌘ */
  combo: string;
  label: string;
  group: '通用' | '模式' | '导航';
  /** 在输入框里也允许触发（仅限带修饰键的组合，纯字母键会打断打字） */
  inInput?: boolean;
  /**
   * 等效别名。v1.4 之前的使用手册就写着 `Ctrl+.` 中断、`Ctrl+L` 新会话，
   * 但这两个键从未真正实现过。既然文档已经这么承诺了，就一并支持，
   * 免得照着手册按的人发现"文档写了却没反应"。
   */
  alias?: string;
}

export const IS_MAC = typeof navigator !== 'undefined'
  && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** 展示用：把 mod 渲染成当前平台的符号。 */
export function prettyCombo(combo: string): string {
  return combo
    .replace(/\bmod\b/g, IS_MAC ? '⌘' : 'Ctrl')
    .replace(/\bshift\b/gi, IS_MAC ? '⇧' : 'Shift')
    .replace(/\balt\b/gi, IS_MAC ? '⌥' : 'Alt')
    .replace(/\+/g, ' + ');
}

export const HOTKEYS: Record<string, Hotkey> = {
  newSession:   { combo: 'mod+N',       label: '开始新会话',       group: '通用', inInput: true, alias: 'mod+L' },
  templates:    { combo: 'mod+K',       label: '打开模板库',       group: '通用', inInput: true },
  settings:     { combo: 'mod+,',       label: '打开通用设置',     group: '通用', inInput: true },
  workspaces:   { combo: 'mod+shift+W', label: '切换工作区',       group: '通用', inInput: true },
  stop:         { combo: 'Esc',         label: '停止当前任务 / 关闭弹窗', group: '通用', inInput: true, alias: 'mod+.' },
  focusInput:   { combo: 'mod+/',       label: '定位到输入框',     group: '通用', inInput: true },
  help:         { combo: '?',           label: '显示快捷键帮助',   group: '通用' },
  toggleTheme:  { combo: 'mod+shift+L', label: '切换深色/浅色',    group: '通用', inInput: true },
  fontUp:       { combo: 'mod+=',       label: '增大字号',         group: '通用', inInput: true },
  fontDown:     { combo: 'mod+-',       label: '减小字号',         group: '通用', inInput: true },
  fontReset:    { combo: 'mod+0',       label: '字号恢复标准',     group: '通用', inInput: true },
  mode1:        { combo: 'mod+1',       label: '对话模式',         group: '模式', inInput: true },
  mode2:        { combo: 'mod+2',       label: '工作模式',         group: '模式', inInput: true },
  mode3:        { combo: 'mod+3',       label: '编程模式',         group: '模式', inInput: true },
  viewChat:     { combo: 'alt+1',       label: '对话工作台',       group: '导航', inInput: true },
  viewPlan:     { combo: 'alt+2',       label: '计划视图',         group: '导航', inInput: true },
  viewTools:    { combo: 'alt+3',       label: '工具面板',         group: '导航', inInput: true },
  viewHistory:  { combo: 'alt+4',       label: '任务历史',         group: '导航', inInput: true },
};

type Handlers = Partial<Record<keyof typeof HOTKEYS, () => void>>;

function isTypingTarget(el: EventTarget | null): boolean {
  const n = el as HTMLElement | null;
  if (!n || !n.tagName) return false;
  const tag = n.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || n.isContentEditable;
}

/** 事件是否匹配某个 combo。mod 按平台映射到 ⌘ / Ctrl。 */
function matches(e: KeyboardEvent, combo: string): boolean {
  const parts = combo.toLowerCase().split('+');
  const key = parts[parts.length - 1];
  const needMod = parts.includes('mod');
  const needShift = parts.includes('shift');
  const needAlt = parts.includes('alt');

  const gotMod = IS_MAC ? e.metaKey : e.ctrlKey;
  if (needMod !== gotMod) return false;
  if (needAlt !== e.altKey) return false;
  // '?' 本身要按 Shift 打出来，故该键不校验 shift
  if (key !== '?' && needShift !== e.shiftKey) return false;

  const k = e.key.toLowerCase();
  if (key === 'esc') return k === 'escape';
  // '=' 与 '+' 同键；数字行的 '-' 与小键盘 'subtract' 都接受
  if (key === '=') return k === '=' || k === '+';
  if (key === '-') return k === '-' || k === '_';
  return k === key;
}

/**
 * 安装全局快捷键。返回卸载函数。
 *
 * 设计取舍：
 *  - 带修饰键的组合在输入框里也生效（Ctrl+N 不会打断打字）；
 *  - 纯字符键（如 `?`）只在非输入态生效，否则用户永远打不出问号；
 *  - 有弹窗打开时只放行 Esc，避免快捷键在弹窗上叠加触发。
 */
export function installHotkeys(handlers: Handlers, opts: { modalOpen: () => boolean }): () => void {
  const onKey = (e: KeyboardEvent) => {
    if (e.isComposing) return;            // 中文输入法组词中，别抢键
    const typing = isTypingTarget(e.target);
    const modal = opts.modalOpen();

    for (const [name, hk] of Object.entries(HOTKEYS)) {
      const fn = handlers[name as keyof typeof HOTKEYS];
      if (!fn) continue;
      if (!matches(e, hk.combo) && !(hk.alias && matches(e, hk.alias))) continue;
      if (typing && !hk.inInput) continue;
      if (modal && name !== 'stop') continue;   // 弹窗打开时只留 Esc
      e.preventDefault();
      e.stopPropagation();
      fn();
      return;
    }
  };
  window.addEventListener('keydown', onKey, true);
  return () => window.removeEventListener('keydown', onKey, true);
}
