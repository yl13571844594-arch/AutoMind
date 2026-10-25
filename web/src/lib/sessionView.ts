// 会话视图的纯逻辑（v1.11.17）：搜索 / 筛选 / 分组 / 排序。
//
// 为什么单独成模块：会话管理面板（SessionsModal）与侧栏搜索必须用**同一套**匹配规则，
// 否则同一个关键词在两边筛出不同结果。这里刻意不 import React、不读 store / localStorage，
// 时间边界由 now 参数注入 —— 于是可以直接编译成 JS 用 node 断言验证，不必起浏览器。
//
// 时间戳单位：会话的 created / updated 是 Unix **秒**（后端 time.time()），
// 而 now 用毫秒（Date.now()），两者在这里统一换算，调用方不用记。

/** 会话行渲染需要的字段。Conversation 结构上完全兼容（多出的字段会被忽略）。 */
export interface SessionItem {
  sid: string;
  suffix?: string;
  title: string;
  pinned?: boolean;
  archived: boolean;
  created: number;
  updated: number;
  turns: number;
  summary?: string;
  running?: boolean;
  /** 所属工作区显示名：仅"全部工作区"范围由调用方填写（Conversation 不含该字段） */
  wsName?: string;
  /** 所属工作区后缀（'' = 默认工作区）：仅"全部工作区"范围由调用方填写 */
  wsSuffix?: string;
}

export type SessionScope = 'current' | 'all';
export type SessionFilter = 'all' | 'pinned' | 'running' | 'archived';
export type SessionGroupBy = 'none' | 'time' | 'status' | 'workspace';
export type SessionSortBy = 'updated' | 'created' | 'title' | 'turns';

/** 默认工作区（没有命名）在跨工作区视图里的显示名。 */
export const DEFAULT_WORKSPACE_LABEL = '默认工作区';

export const FILTER_OPTIONS: { value: SessionFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'pinned', label: '📌 置顶' },
  { value: 'running', label: '🟢 运行中' },
  { value: 'archived', label: '🗄 已归档' },
];

export const SORT_OPTIONS: { value: SessionSortBy; label: string }[] = [
  { value: 'updated', label: '最近活动 ↓' },
  { value: 'created', label: '创建时间 ↓' },
  { value: 'title', label: '标题 A→Z' },
  { value: 'turns', label: '轮数 ↓' },
];

export const GROUP_OPTIONS: { value: SessionGroupBy; label: string }[] = [
  { value: 'none', label: '不分组' },
  { value: 'time', label: '按时间' },
  { value: 'status', label: '按状态' },
  { value: 'workspace', label: '按工作区' },
];

// ═══════════════════════════════════════════════════════════
// 搜索
// ═══════════════════════════════════════════════════════════

/**
 * 把搜索词切成关键词：空格分词、转小写、去重。
 * 空串 → []（表示"不搜索"，而不是"搜空串"）。
 */
export function splitKeywords(query: string | undefined): string[] {
  const seen = new Set<string>();
  for (const raw of String(query || '').toLowerCase().split(/\s+/)) {
    const k = raw.trim();
    if (k) seen.add(k);
  }
  return [...seen];
}

/** 搜索命中的文本范围：标题 + 要点摘要 + sid。 */
export function sessionSearchText(c: SessionItem): string {
  return `${c.title || ''}\n${c.summary || ''}\n${c.sid || ''}`.toLowerCase();
}

/**
 * 多关键词**全部命中**才算匹配，且允许分别命中不同字段
 * （例如"部署 周日"：一个在标题、一个在要点摘要里）。
 */
export function sessionMatches(c: SessionItem, queryOrKeywords: string | string[]): boolean {
  const kws = Array.isArray(queryOrKeywords) ? queryOrKeywords : splitKeywords(queryOrKeywords);
  if (!kws.length) return true;
  const text = sessionSearchText(c);
  return kws.every((k) => text.includes(k.toLowerCase()));
}

// ═══════════════════════════════════════════════════════════
// 筛选
// ═══════════════════════════════════════════════════════════

/**
 * 筛选口径：
 * - pinned  只看没归档的置顶会话（已归档的置顶标记属于残留，应该去「已归档」里找）；
 * - running 只看正在执行的会话（归档会话不会在执行中）；
 * - archived 只看已归档会话。
 */
export function sessionMatchesFilter(c: SessionItem, filter: SessionFilter): boolean {
  switch (filter) {
    case 'pinned': return !!c.pinned && !c.archived;
    case 'running': return !!c.running;
    case 'archived': return !!c.archived;
    case 'all':
    default: return true;
  }
}

// ═══════════════════════════════════════════════════════════
// 排序
// ═══════════════════════════════════════════════════════════

/**
 * 排序。置顶**始终优先**，其余按所选维度；
 * 同值时依次用"最近活动 → 创建时间 → sid"兜底，保证每次渲染顺序一致
 * （不依赖服务端返回顺序，否则列表会莫名跳动）。
 */
export function sortSessions(items: readonly SessionItem[], sortBy: SessionSortBy): SessionItem[] {
  const cmpText = (a: string, b: string) =>
    a.localeCompare(b, 'zh-Hans-CN', { numeric: true, sensitivity: 'base' });
  return [...items].sort((a, b) => {
    const pin = Number(!!b.pinned) - Number(!!a.pinned);
    if (pin) return pin;
    let d = 0;
    switch (sortBy) {
      case 'created': d = (b.created || 0) - (a.created || 0); break;
      case 'title': d = cmpText(a.title || '', b.title || ''); break;
      case 'turns': d = (b.turns || 0) - (a.turns || 0); break;
      case 'updated':
      default: d = (b.updated || 0) - (a.updated || 0); break;
    }
    if (d) return d;
    d = (b.updated || 0) - (a.updated || 0);
    if (d) return d;
    d = (b.created || 0) - (a.created || 0);
    if (d) return d;
    return cmpText(a.sid || '', b.sid || '');
  });
}

// ═══════════════════════════════════════════════════════════
// 分组
// ═══════════════════════════════════════════════════════════

export type TimeBucket = 'today' | 'yesterday' | 'week' | 'earlier';
export type StatusBucket = 'running' | 'pinned' | 'normal' | 'archived';

const DAY_MS = 86_400_000;

/** 本地日历日的序数（用 UTC 计算，避开夏令时导致的 23/25 小时偏差）。 */
function localDayOrdinal(ms: number): number {
  const d = new Date(ms);
  return Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / DAY_MS);
}

/**
 * 按时间分桶：今天 / 昨天 / 近 7 天 / 更早。
 * now 用毫秒时间戳注入，测试里传固定值即可得到确定的边界。
 * 未来时间（客户端与服务器时钟有偏差）归入"今天"，不让它从列表里消失。
 */
export function sessionTimeBucket(ts: number | undefined, now: number = Date.now()): TimeBucket {
  if (!ts) return 'earlier';
  const diff = localDayOrdinal(now) - localDayOrdinal(ts * 1000);
  if (diff <= 0) return 'today';
  if (diff === 1) return 'yesterday';
  if (diff <= 6) return 'week';
  return 'earlier';
}

const TIME_BUCKETS: { key: TimeBucket; label: string }[] = [
  { key: 'today', label: '今天' },
  { key: 'yesterday', label: '昨天' },
  { key: 'week', label: '近 7 天' },
  { key: 'earlier', label: '更早' },
];

const STATUS_BUCKETS: { key: StatusBucket; label: string }[] = [
  { key: 'running', label: '运行中' },
  { key: 'pinned', label: '置顶' },
  { key: 'normal', label: '普通会话' },
  { key: 'archived', label: '已归档' },
];

/**
 * 状态分桶。已归档优先于其它标记 —— 归档是生命周期状态，
 * 置顶 / 运行中标记在归档后不该再把它留在"在用"的分组里。
 */
export function sessionStatusBucket(c: SessionItem): StatusBucket {
  if (c.archived) return 'archived';
  if (c.running) return 'running';
  if (c.pinned) return 'pinned';
  return 'normal';
}

export interface SessionSection {
  key: string;
  /** 分组标题；'none' 分组返回空串（面板据此不渲染标题） */
  label: string;
  items: SessionItem[];
}

export interface GroupOptions {
  /** 毫秒时间戳，默认 Date.now()；"按时间"分组与今天/昨天边界都用它 */
  now?: number;
  /** 当前工作区显示名（"按工作区"分组时排在最前）；默认 DEFAULT_WORKSPACE_LABEL */
  currentWorkspace?: string;
}

/** 分组。空分组不返回；'none' 返回单段（label 为空）。 */
export function groupSessions(
  items: readonly SessionItem[], groupBy: SessionGroupBy, opts: GroupOptions = {},
): SessionSection[] {
  if (!items.length) return [];
  const now = opts.now ?? Date.now();

  if (groupBy === 'time') {
    return TIME_BUCKETS
      .map(({ key, label }) => ({
        key: 'time:' + key, label,
        items: items.filter((c) => sessionTimeBucket(c.updated, now) === key),
      }))
      .filter((s) => s.items.length > 0);
  }

  if (groupBy === 'status') {
    return STATUS_BUCKETS
      .map(({ key, label }) => ({
        key: 'status:' + key, label,
        items: items.filter((c) => sessionStatusBucket(c) === key),
      }))
      .filter((s) => s.items.length > 0);
  }

  if (groupBy === 'workspace') {
    const current = opts.currentWorkspace || DEFAULT_WORKSPACE_LABEL;
    const byWs = new Map<string, SessionItem[]>();
    for (const c of items) {
      const name = c.wsName || current;
      const list = byWs.get(name);
      if (list) list.push(c); else byWs.set(name, [c]);
    }
    // 当前工作区排最前（用户最可能在这里找东西），其余按名称排
    const names = [...byWs.keys()].sort((a, b) => {
      if (a === current) return -1;
      if (b === current) return 1;
      return a.localeCompare(b, 'zh-Hans-CN');
    });
    return names.map((name) => ({ key: 'ws:' + name, label: name, items: byWs.get(name) ?? [] }));
  }

  // 'none'：不分组，sections 只是"一段"，方便调用方统一渲染
  return [{ key: 'all', label: '', items: [...items] }];
}

// ═══════════════════════════════════════════════════════════
// 组合入口
// ═══════════════════════════════════════════════════════════

export interface SessionViewOptions {
  filter: SessionFilter;
  groupBy: SessionGroupBy;
  sortBy: SessionSortBy;
  query?: string;
  now?: number;
  currentWorkspace?: string;
}

export interface SessionViewResult {
  sections: SessionSection[];
  /** 传入的会话总数（含已归档）—— 用来区分"没有会话"与"筛选后没有匹配" */
  total: number;
  /** 筛选 + 搜索后可见条数 */
  visible: number;
  keywords: string[];
}

/**
 * 会话视图的主入口：先筛（搜索 / 维度）再排再分组。
 * 顺序固定为"筛 → 排 → 分"，所以同一组内的顺序与"不分组"时一致。
 */
export function computeSessionView(
  items: readonly SessionItem[], opts: SessionViewOptions,
): SessionViewResult {
  const keywords = splitKeywords(opts.query);
  const visible = sortSessions(
    items.filter((c) => sessionMatchesFilter(c, opts.filter) && sessionMatches(c, keywords)),
    opts.sortBy,
  );
  return {
    sections: groupSessions(visible, opts.groupBy, {
      now: opts.now,
      currentWorkspace: opts.currentWorkspace,
    }),
    total: items.length,
    visible: visible.length,
    keywords,
  };
}
