// v1.11.2 输入框指令与引用：行首 / 或 ./ 弹出指令面板，@ 弹出文件 / 目录 / 对话。
//
// 设计要点：
//   · 触发只看"光标前的那个词"：/ 和 ./ 只在行首生效（正文里的 a/b、./build.sh 不误触），
//     @ 要求前面是空白或行首（邮箱 a@b.com 不误触）；
//   · 键盘优先：↑↓ 选择、Enter / Tab 确认、Esc 关闭；鼠标悬停同步高亮；
//   · 选中后由调用方决定怎么落地（执行动作 / 插入提示词 / 挂 chip / 插入 @路径），
//     本组件只负责"找到候选、选中哪一个"。
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { apiGet } from '../../api/client';

export type SuggestKind = 'slash' | 'mention';

export interface Trigger {
  kind: SuggestKind;
  query: string;
  /** 触发词在输入框里的起止位置（替换用） */
  start: number;
  end: number;
}

export interface SuggestItem {
  key: string;
  group: string;
  icon: string;
  title: string;
  desc?: string;
  /** 右侧小字（如 "Enter 执行" / 文件大小） */
  hint?: string;
  /** 参与匹配的额外文本（别名 / 拼音首字母 / 英文名） */
  keywords?: string;
  disabled?: boolean;
  data?: any;
}

/** 从输入框当前内容与光标位置算出触发词；没有就返回 null。 */
export function detectTrigger(value: string, caret: number): Trigger | null {
  const before = value.slice(0, caret);
  // 光标后紧跟着非空白字符时不弹（用户在词中间编辑）
  if (caret < value.length && !/\s/.test(value[caret])) return null;
  const at = /(^|\s)@([^\s@]{0,120})$/.exec(before);
  if (at) {
    const start = before.length - at[2].length - 1;
    return { kind: 'mention', query: at[2], start, end: caret };
  }
  const sl = /(^|\n)[ \t]*(\.?\/)([^\s/]{0,40})$/.exec(before);
  if (sl) {
    const start = before.length - sl[3].length - sl[2].length;
    return { kind: 'slash', query: sl[3], start, end: caret };
  }
  return null;
}

/** 打分：前缀 > 包含 > 子序列；0 = 不匹配。 */
function score(item: SuggestItem, q: string): number {
  if (!q) return 1;
  const ql = q.toLowerCase();
  const title = item.title.toLowerCase().replace(/^[/@]/, '');
  const hay = `${title} ${(item.keywords || '').toLowerCase()} ${(item.desc || '').toLowerCase()}`;
  if (title.startsWith(ql)) return 100 - title.length / 100;
  if ((item.keywords || '').toLowerCase().split(/\s+/).some((k) => k.startsWith(ql))) return 80;
  if (title.includes(ql)) return 60;
  if (hay.includes(ql)) return 40;
  let i = 0;
  for (const ch of title) if (ch === ql[i]) i += 1;
  return i === ql.length ? 20 : 0;
}

export interface SuggestHandle {
  /** 交给面板处理的按键；返回 true = 已处理（调用方别再发送 / 换行） */
  onKey: (e: React.KeyboardEvent) => boolean;
}

interface Props {
  trigger: Trigger | null;
  /** 指令候选（同步：调用方按当前状态拼好） */
  commands: SuggestItem[];
  /** @ 的静态候选（对话列表等） */
  mentionStatic: SuggestItem[];
  onPick: (item: SuggestItem, trigger: Trigger) => void;
  onClose: () => void;
}

const fmtSize = (n?: number) => n == null ? '' : n < 1024 ? `${n} B`
  : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;

const ComposerSuggest = forwardRef<SuggestHandle, Props>(function ComposerSuggest(
  { trigger, commands, mentionStatic, onPick, onClose }, ref,
) {
  const [files, setFiles] = useState<SuggestItem[]>([]);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const seq = useRef(0);

  // @ 文件候选：防抖拉取（项目大时搜索要几十毫秒，别每敲一个字就打一次）
  const mq = trigger?.kind === 'mention' ? trigger.query : null;
  useEffect(() => {
    if (mq === null) { setFiles([]); return; }
    const my = ++seq.current;
    setLoadingFiles(true);
    const t = window.setTimeout(async () => {
      try {
        const r = await apiGet(`/files/search?q=${encodeURIComponent(mq)}&limit=24`);
        if (my !== seq.current) return;
        setFiles((r.items || []).map((f: any) => ({
          key: 'f:' + f.path, group: f.dir ? '目录' : '文件', icon: f.dir ? '📁' : fileIcon(f.path),
          title: f.path.split('/').pop() || f.path, desc: f.path,
          hint: f.dir ? '目录' : fmtSize(f.size), data: { type: f.dir ? 'dir' : 'file', path: f.path },
        })));
      } catch {
        if (my === seq.current) setFiles([]);
      } finally {
        if (my === seq.current) setLoadingFiles(false);
      }
    }, mq ? 120 : 0);
    return () => window.clearTimeout(t);
  }, [mq]);

  const items = useMemo(() => {
    if (!trigger) return [];
    const q = trigger.query;
    if (trigger.kind === 'slash') {
      const hits = commands.map((it, i) => ({ it, i, s: score(it, q) })).filter((x) => x.s > 0);
      if (!q) return hits.map((x) => x.it).slice(0, 80);
      // 组按"组内最佳匹配"排序、组内按分数排序：保证每个分组标题只出现一次
      const best: Record<string, number> = {};
      const first: Record<string, number> = {};
      for (const h of hits) {
        best[h.it.group] = Math.max(best[h.it.group] || 0, h.s);
        if (!(h.it.group in first)) first[h.it.group] = h.i;
      }
      return hits.sort((a, b) => (a.it.group === b.it.group
        ? b.s - a.s || a.i - b.i
        : best[b.it.group] - best[a.it.group] || first[a.it.group] - first[b.it.group]))
        .map((x) => x.it).slice(0, 60);
    }
    const convs = mentionStatic.map((it) => ({ it, s: score(it, q) })).filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s).map((x) => x.it).slice(0, q ? 6 : 4);
    return [...files.filter((f) => f.group === '文件'), ...convs, ...files.filter((f) => f.group === '目录')];
  }, [trigger, commands, mentionStatic, files]);

  // 候选变了 → 高亮回到第一个可选项
  useEffect(() => {
    setActive(Math.max(0, items.findIndex((i) => !i.disabled)));
  }, [items.length, trigger?.kind, trigger?.query]);

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  const move = (d: number) => {
    if (!items.length) return;
    let i = active;
    for (let n = 0; n < items.length; n += 1) {
      i = (i + d + items.length) % items.length;
      if (!items[i].disabled) break;
    }
    setActive(i);
  };

  useImperativeHandle(ref, () => ({
    onKey: (e) => {
      if (!trigger) return false;
      if (e.key === 'Escape') { onClose(); return true; }
      if (!items.length) return false;
      if (e.key === 'ArrowDown') { move(1); return true; }
      if (e.key === 'ArrowUp') { move(-1); return true; }
      if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') {
        const it = items[active];
        if (it && !it.disabled) { onPick(it, trigger); return true; }
      }
      return false;
    },
  }), [trigger, items, active, onPick, onClose]);

  if (!trigger) return null;
  const empty = !items.length;
  if (empty && trigger.kind === 'slash' && trigger.query) return null;   // 不像指令：安静让路

  let lastGroup = '';
  return (
    <div className={'cs-pop ' + trigger.kind} role="listbox" onMouseDown={(e) => e.preventDefault()}>
      <div className="cs-head">
        <span>{trigger.kind === 'slash' ? '指令' : '引用文件 / 目录 / 对话'}</span>
        <span className="cs-keys"><kbd>↑</kbd><kbd>↓</kbd> 选择 <kbd>Enter</kbd> 确认 <kbd>Esc</kbd> 关闭</span>
      </div>
      <div className="cs-list" ref={listRef}>
        {empty && (
          <div className="cs-empty">
            {trigger.kind === 'mention'
              ? (loadingFiles ? '正在查找…' : `项目里没有找到「${trigger.query}」`)
              : '没有匹配的指令'}
          </div>
        )}
        {items.map((it, i) => {
          const head = it.group !== lastGroup ? it.group : '';
          lastGroup = it.group;
          return (
            <div key={it.key}>
              {head && <div className="cs-group">{head}</div>}
              <div data-idx={i} role="option" aria-selected={i === active}
                aria-disabled={it.disabled || undefined}
                className={'cs-item' + (i === active ? ' active' : '') + (it.disabled ? ' disabled' : '')}
                onMouseEnter={() => !it.disabled && setActive(i)}
                onClick={() => !it.disabled && onPick(it, trigger)}>
                <span className="cs-ico">{it.icon}</span>
                <span className="cs-main">
                  <span className="cs-title">{it.title}</span>
                  {it.desc && <span className="cs-desc">{it.desc}</span>}
                </span>
                {it.hint && <span className="cs-hint">{it.hint}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
});

export default ComposerSuggest;

function fileIcon(path: string): string {
  const ext = (path.split('.').pop() || '').toLowerCase();
  if (['py', 'js', 'ts', 'tsx', 'jsx', 'go', 'rs', 'java', 'c', 'cpp', 'cs', 'rb', 'php', 'kt', 'swift'].includes(ext)) return '🧾';
  if (['md', 'txt', 'rst'].includes(ext)) return '📝';
  if (['json', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'env'].includes(ext)) return '⚙️';
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext)) return '🖼';
  if (['csv', 'xlsx', 'xls'].includes(ext)) return '📊';
  if (['html', 'htm', 'css', 'scss'].includes(ext)) return '🌐';
  return '📄';
}
