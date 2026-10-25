// 💬 会话管理面板（v1.11.17）：范围 / 搜索 / 筛选 / 分组 / 排序 + 打开、重命名、置顶、归档、恢复、删除。
//
// 为什么单独做一个面板：侧栏只有 248px，塞不下搜索框加四组控件；而会话一多，
// 平铺列表很难"找到上周那个跑数据清洗的会话"。这里把当前工作区（或全部工作区）的
// 会话摊开管理；侧栏只保留轻量搜索与入口，常用动作仍在会话行的 ⋯ 菜单里。
//
// 数据来源：当前工作区直接用 useConv（与侧栏同一份 store，操作后自动同步）；
// "全部工作区"是只读合并 —— 先 GET /api/workspaces，再按各工作区后缀分别拉
// GET /api/conversations，合并后标上工作区名。未新增任何后端接口。
import {
  App, Button, Empty, Input, Modal, Segmented, Skeleton, Tag, type MenuProps,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost, errText, SID } from '../../api/client';
import {
  computeSessionView, DEFAULT_WORKSPACE_LABEL, FILTER_OPTIONS, GROUP_OPTIONS,
  SORT_OPTIONS, type SessionFilter, type SessionGroupBy, type SessionItem,
  type SessionScope, type SessionSortBy,
} from '../../lib/sessionView';
import { useApp } from '../../store/app';
import { useChat } from '../../store/chat';
import { relTime, useConv, type Conversation } from '../../store/conv';
import { useUi } from '../../store/ui';
import { ErrorPanel } from '../ui/AsyncBoundary';
import SafeDropdown from '../ui/SafeDropdown';
import SafeSelect from '../ui/SafeSelect';

/** 面板设置持久化键：范围 / 筛选 / 分组 / 排序刷新后保留（搜索词是临时的，不存）。 */
const PREFS_KEY = 'automind_sessions_view';

interface ViewPrefs {
  scope: SessionScope;
  filter: SessionFilter;
  groupBy: SessionGroupBy;
  sortBy: SessionSortBy;
}

const DEFAULT_PREFS: ViewPrefs = { scope: 'current', filter: 'all', groupBy: 'none', sortBy: 'updated' };

function pickOption<T extends string>(options: { value: T }[], v: unknown, fallback: T): T {
  return options.some((o) => o.value === v) ? (v as T) : fallback;
}

function loadPrefs(): ViewPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') as Record<string, unknown>;
    const scope: SessionScope = raw.scope === 'all' ? 'all' : 'current';
    let groupBy = pickOption(GROUP_OPTIONS, raw.groupBy, 'none');
    // "按工作区"只在"全部工作区"范围有意义；上次残留时退回不分组
    if (scope === 'current' && groupBy === 'workspace') groupBy = 'none';
    return {
      scope, groupBy,
      filter: pickOption(FILTER_OPTIONS, raw.filter, 'all'),
      sortBy: pickOption(SORT_OPTIONS, raw.sortBy, 'updated'),
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

/**
 * 工作区目录路径 → 会话隔离后缀。
 * 必须与 WorkspacesModal 里的 wsHash 一致：切换工作区时前端用同一个后缀拉会话，
 * 两边算出来不一样就会"切过去看不到会话"。
 */
function wsHash(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h).toString(36).slice(0, 8);
}

function wsSuffixOf(path: string, name: string): string {
  return '_w' + wsHash(path || name);
}

function fmtDate(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  return d.getFullYear() === now.getFullYear()
    ? `${d.getMonth() + 1}月${d.getDate()}日`
    : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

// ═══════════════════════════════════════════════════════════
// 单行会话
// ═══════════════════════════════════════════════════════════

interface RowProps {
  c: SessionItem;
  active: boolean;
  /** 全部工作区范围下显示所属工作区标签 */
  showWorkspace: boolean;
  editing: boolean;
  onOpen: () => void;
  onStartEdit: () => void;
  onStopEdit: () => void;
  onRename: (title: string) => void;
  onTogglePin: () => void;
  onArchive: () => void;
  onUnarchive: () => void;
  onDelete: () => void;
}

function SessionRow({
  c, active, showWorkspace, editing,
  onOpen, onStartEdit, onStopEdit, onRename, onTogglePin, onArchive, onUnarchive, onDelete,
}: RowProps) {
  const { modal } = App.useApp();
  const [draft, setDraft] = useState(c.title);
  // Escape 取消与失焦提交会同时触发：用标记保证取消后不再顺手把改动写回去
  const [cancelled, setCancelled] = useState(false);
  useEffect(() => {
    if (editing) { setDraft(c.title); setCancelled(false); }
  }, [editing, c.title]);

  const items: MenuProps['items'] = c.archived ? [
    { key: 'open', label: '👁 打开查看', onClick: onOpen },
    { key: 'restore', label: '📤 恢复到会话列表', onClick: onUnarchive },
    ...(c.suffix ? [
      { type: 'divider' as const },
      {
        key: 'del', danger: true, label: '🗑 删除',
        onClick: () => modal.confirm({
          title: `删除会话「${c.title}」？`,
          content: '会话内容将被删除且无法恢复；已写入长期记忆的要点保留。',
          okText: '删除', okButtonProps: { danger: true },
          onOk: onDelete,
        }),
      },
    ] : []),
  ] : [
    { key: 'rename', label: '✏️ 重命名', onClick: onStartEdit },
    { key: 'pin', label: c.pinned ? '📌 取消置顶' : '📌 置顶', onClick: onTogglePin },
    { key: 'archive', label: '🗄 归档（要点写入项目记忆）', onClick: onArchive },
    ...(c.suffix ? [
      { type: 'divider' as const },
      {
        key: 'del', danger: true, label: '🗑 删除',
        onClick: () => modal.confirm({
          title: `删除会话「${c.title}」？`,
          content: '会话内容将被删除且无法恢复。若想保留要点，请改用「归档」。',
          okText: '删除', okButtonProps: { danger: true },
          onOk: onDelete,
        }),
      },
    ] : []),
  ];

  if (editing) {
    return (
      <div className="ssm-row editing">
        <Input size="small" autoFocus value={draft} maxLength={60}
          onChange={(e) => setDraft(e.target.value)}
          onPressEnter={() => {
            if (!cancelled) onRename(draft);
            onStopEdit();
          }}
          onBlur={() => {
            if (!cancelled) onRename(draft);
            onStopEdit();
          }}
          onKeyDown={(e) => { if (e.key === 'Escape') { setCancelled(true); onStopEdit(); } }} />
      </div>
    );
  }

  return (
    <div
      className={'ssm-row' + (active ? ' active' : '') + (c.archived ? ' archived' : '')}>
      <span className="conv-dot" data-running={c.running ? '1' : undefined} />
      {/* 行本身不响应单击：单击即打开会与"双击标题重命名"抢事件（第一次点击就把面板关了）；
          打开统一走右侧「打开」按钮，双击只用于重命名 */}
      <div className="ssm-main" onDoubleClick={c.archived ? undefined : onStartEdit}
        title={c.summary ? `要点：${c.summary}` : c.title}>
        <div className="ssm-title">
          {c.pinned && <span className="conv-pin">📌</span>}
          <span className="ssm-title-text">{c.title || '未命名会话'}</span>
          {c.running && <Tag color="green">运行中</Tag>}
          {c.archived && <Tag>已归档</Tag>}
          {showWorkspace && c.wsName && (
            <Tag color="blue" className="ssm-ws-tag" title={`所属工作区：${c.wsName}`}>🗂 {c.wsName}</Tag>
          )}
        </div>
        <div className="ssm-meta">
          {c.turns ? `${c.turns} 轮 · ` : ''}更新于 {relTime(c.updated) || '—'}
          {c.created ? ` · 创建于 ${fmtDate(c.created)}` : ''}
          {c.summary ? ` · ${c.summary.slice(0, 80)}` : ''}
        </div>
      </div>
      <div className="ssm-acts">
        <Button size="small" type={active ? 'primary' : 'default'} onClick={onOpen}>
          {active ? '当前' : '打开'}
        </Button>
        {/* 与侧栏会话行同样走 SafeDropdown：部分电脑上 antd 弹层不可见 */}
        <SafeDropdown menu={{ items }} placement="bottomRight" overlayClassName="conv-menu">
          <button className="conv-more" aria-label="会话操作"
            onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>⋯</button>
        </SafeDropdown>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// 面板
// ═══════════════════════════════════════════════════════════

export default function SessionsModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'sessions';
  const close = useUi((s) => s.closeModal);

  // 当前工作区：与侧栏共用 store（写操作全部走 store 方法，不另写一份 API 调用）
  const storeItems = useConv((s) => s.items);
  const storeLoading = useConv((s) => s.loading);
  const storeLoaded = useConv((s) => s.loaded);
  const storeError = useConv((s) => s.error);
  const load = useConv((s) => s.load);
  const switchTo = useConv((s) => s.switchTo);
  const rename = useConv((s) => s.rename);
  const togglePin = useConv((s) => s.togglePin);
  const archive = useConv((s) => s.archive);
  const unarchive = useConv((s) => s.unarchive);
  const remove = useConv((s) => s.remove);

  const wsActive = useApp((s) => s.wsActive);
  const wsSuffix = useApp((s) => s.wsSuffix);
  const convSuffix = useApp((s) => s.convSuffix);

  const [prefs, setPrefs] = useState<ViewPrefs>(loadPrefs);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState('');
  const [now, setNow] = useState(() => Date.now());

  // 全部工作区：只读合并数据
  const [allItems, setAllItems] = useState<SessionItem[]>([]);
  const [allLoading, setAllLoading] = useState(false);
  const [allLoaded, setAllLoaded] = useState(false);
  const [allError, setAllError] = useState<Error | null>(null);
  const [allWarn, setAllWarn] = useState('');

  const fetchAll = useCallback(async () => {
    setAllLoading(true);
    setAllError(null);
    setAllWarn('');
    try {
      const d = await apiGet('/workspaces');
      const workspaces: { name?: string; path?: string }[] =
        Array.isArray(d?.workspaces) ? d.workspaces : [];
      // 默认工作区（后缀为空串）不在配置列表里，但也要列出来
      const targets: { label: string; suffix: string }[] = [
        { label: DEFAULT_WORKSPACE_LABEL, suffix: '' },
        ...workspaces
          .filter((w) => !!w.name)
          .map((w) => ({ label: w.name as string, suffix: wsSuffixOf(w.path || '', w.name as string) })),
      ];
      const results = await Promise.allSettled(targets.map(async (t) => {
        const r = await apiGet(
          `/conversations?owner=${encodeURIComponent(SID)}&workspace=${encodeURIComponent(t.suffix)}`);
        const list: Conversation[] = Array.isArray(r?.conversations) ? r.conversations : [];
        return list.map((c): SessionItem => ({ ...c, wsName: t.label, wsSuffix: t.suffix }));
      }));
      const merged: SessionItem[] = [];
      let failed = 0;
      for (const r of results) {
        if (r.status === 'fulfilled') merged.push(...r.value);
        else failed += 1;
      }
      if (failed && failed === results.length) {
        const first = results.find((r) => r.status === 'rejected');
        throw first && first.status === 'rejected' && first.reason instanceof Error
          ? first.reason : new Error('读取各工作区会话失败');
      }
      setAllItems(merged);
      setAllLoaded(true);
      if (failed) setAllWarn(`有 ${failed} 个工作区的会话读取失败，列表可能不完整`);
    } catch (e) {
      setAllError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setAllLoading(false);
    }
  }, []);

  // 设置持久化：刷新后保留范围 / 筛选 / 分组 / 排序
  useEffect(() => {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* 隐私模式忽略 */ }
  }, [prefs]);

  // 打开面板：重置临时状态 + 刷新数据（时间分组按"打开那一刻"算）
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setEditing('');
    setNow(Date.now());
    load();
    if (prefs.scope === 'all') fetchAll();
    // load / fetchAll 是稳定引用；scope 变化时也重新取数
  }, [open, prefs.scope, load, fetchAll]);

  const patch = (p: Partial<ViewPrefs>) => setPrefs((s) => ({ ...s, ...p }));

  const changeScope = (scope: SessionScope) => setPrefs((s) => ({
    ...s,
    scope,
    // 切回"当前工作区"时"按工作区"分组失去意义，顺手退回不分组
    groupBy: scope === 'current' && s.groupBy === 'workspace' ? 'none' : s.groupBy,
  }));

  const items: readonly SessionItem[] = prefs.scope === 'current' ? storeItems : allItems;
  const view = useMemo(() => computeSessionView(items, {
    filter: prefs.filter,
    groupBy: prefs.groupBy,
    sortBy: prefs.sortBy,
    query,
    now,
    currentWorkspace: wsActive || DEFAULT_WORKSPACE_LABEL,
  }), [items, prefs.filter, prefs.groupBy, prefs.sortBy, query, now, wsActive]);

  const firstLoad = prefs.scope === 'current' ? !storeLoaded : !allLoaded;
  const loading = prefs.scope === 'current' ? storeLoading : allLoading;
  const error: Error | null = prefs.scope === 'current'
    ? (storeError ? new Error(storeError) : null)
    : allError;
  const retry = prefs.scope === 'current' ? load : fetchAll;

  // 写操作后：当前工作区由 store 自己刷新；"全部工作区"是合并数据，需要重拉
  const afterMutate = useCallback(async () => {
    if (prefs.scope === 'all') await fetchAll();
  }, [prefs.scope, fetchAll]);

  // SessionItem 是 Conversation 的结构超集（多了工作区标注），store 只按 sid / suffix 操作
  const asConversation = (c: SessionItem) => c as Conversation;

  /**
   * 打开会话。跨工作区时顺序很关键：
   * 1. 先把当前工作区的列表与聊天落盘 —— 切换后 localStorage 里的会话键就变了；
   * 2. 必须走后端 /workspaces/switch：Agent 的目录 / 记忆 / 索引都在服务端，
   *    只改前端状态的话下一条消息仍会跑在旧工作区的目录里；
   * 3. 先 useChat.reload() 让内存消息对齐新工作区的"记忆会话"，再 switchTo
   *    （switchTo 内部还会 persist 一次，顺序反了会把旧工作区的内容写进新工作区的键）。
   */
  const openSession = async (c: SessionItem) => {
    const app = useApp.getState();
    if (app.running) {
      message.warning('有任务正在执行，请先停止再切换会话或工作区');
      return;
    }
    try {
      if (c.wsSuffix !== undefined && c.wsSuffix !== app.wsSuffix) {
        // 默认工作区在列表里显示为"默认工作区"，但接口与 setWorkspace 的名称都是空串
        const apiName = c.wsSuffix ? (c.wsName || '') : '';
        await load();
        useChat.getState().persist();
        useChat.getState().persistSide();
        const r = await apiPost('/workspaces/switch', { name: apiName });
        if (r?.error) throw new Error(String(r.error));
        // 以服务端返回的真实目录算后缀，避免前端配置里的路径与实际不一致
        const suffix = apiName ? '_w' + wsHash(r?.project || apiName) : '';
        useApp.getState().setWorkspace(apiName, suffix);
        useChat.getState().reload();
        useApp.getState().loadStatus();
      }
      if (useApp.getState().running) {
        message.warning('任务刚开始执行，已停留在原会话');
        return;
      }
      if (!switchTo(c.suffix || '')) return;
      close();
    } catch (e) {
      message.error(`打开会话失败：${errText(e)}`);
    }
  };

  const doRename = async (c: SessionItem, title: string) => {
    await rename(asConversation(c), title);
    await afterMutate();
  };
  const doTogglePin = async (c: SessionItem) => {
    await togglePin(asConversation(c));
    await afterMutate();
  };
  const doArchive = async (c: SessionItem) => {
    await archive(asConversation(c));
    await afterMutate();
  };
  const doUnarchive = async (c: SessionItem) => {
    await unarchive(asConversation(c));
    await afterMutate();
  };
  const doDelete = async (c: SessionItem) => {
    await remove(asConversation(c));
    await afterMutate();
  };

  const narrowed = view.visible !== view.total;
  const countText = `${prefs.scope === 'all' ? '全部工作区' : '当前工作区'}共 ${view.total} 个会话`
    + (narrowed ? ` · 匹配 ${view.visible} 个` : '');
  const cleaned = !!(query || prefs.filter !== 'all');

  const showWorkspace = prefs.scope === 'all' && prefs.groupBy !== 'workspace';
  const activeOf = (c: SessionItem) =>
    c.suffix === convSuffix && (prefs.scope === 'current' || !c.wsSuffix || c.wsSuffix === wsSuffix);

  const resetFilters = () => { setQuery(''); patch({ filter: 'all' }); };

  const scopeOptions = [
    { value: 'current' as const, label: `当前工作区${wsActive ? `（${wsActive}）` : ''}` },
    { value: 'all' as const, label: '全部工作区' },
  ];

  return (
    <Modal
      title="💬 会话管理"
      open={open}
      onCancel={close}
      width={860}
      destroyOnClose
      footer={(
        <div className="ssm-foot">
          <span className="hint-text">提示：双击会话标题可重命名；置顶会话始终排在前面；分组 / 排序 / 筛选设置会自动保存。</span>
          <Button onClick={close}>关闭</Button>
        </div>
      )}
    >
      <div className="ssm-toolbar">
        <div className="ssm-row1">
          <Segmented value={prefs.scope} options={scopeOptions}
            onChange={(v) => changeScope(v as SessionScope)} />
          <Input
            className="ssm-search"
            allowClear
            autoFocus
            value={query}
            placeholder="搜索标题 / 要点 / sid，空格分隔多个关键词"
            prefix={<span aria-hidden>🔍</span>}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="ssm-row2">
          <Segmented value={prefs.filter} options={FILTER_OPTIONS}
            onChange={(v) => patch({ filter: v as SessionFilter })} />
          <span className="ssm-field">
            <span className="hint-text">分组</span>
            <SafeSelect<SessionGroupBy>
              value={prefs.groupBy}
              onChange={(v) => patch({ groupBy: v })}
              options={GROUP_OPTIONS.map((o) => ({
                ...o,
                // "按工作区"只在全部工作区范围有意义
                disabled: o.value === 'workspace' && prefs.scope === 'current',
              }))}
              style={{ width: 112 }}
            />
          </span>
          <span className="ssm-field">
            <span className="hint-text">排序</span>
            <SafeSelect<SessionSortBy>
              value={prefs.sortBy}
              onChange={(v) => patch({ sortBy: v })}
              options={SORT_OPTIONS}
              style={{ width: 124 }}
            />
          </span>
          {cleaned && (
            <Button size="small" type="link" onClick={resetFilters}>清除筛选</Button>
          )}
          <span className="ssm-count">{countText}</span>
        </div>
      </div>

      {loading && firstLoad && (
        <div className="ssm-state"><Skeleton active paragraph={{ rows: 8 }} /></div>
      )}
      {!firstLoad && error && (
        <div className="ssm-warn">
          ⚠️ 刷新失败：{errText(error)}，下方为上一次的数据。
          <Button type="link" size="small" onClick={retry}>重试</Button>
        </div>
      )}
      {allWarn && prefs.scope === 'all' && (
        <div className="ssm-warn">⚠️ {allWarn}<Button type="link" size="small" onClick={retry}>重试</Button></div>
      )}

      {firstLoad ? (
        (!loading && error)
          ? <ErrorPanel error={error} onRetry={retry} what="会话列表" />
          : null
      ) : view.visible === 0 ? (
        <div className="ssm-empty">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={view.total === 0
              ? (prefs.scope === 'all' ? '所有工作区都还没有会话' : '当前工作区还没有会话，去对话工作台开始第一个吧')
              : '没有匹配的会话：换个关键词，或放宽筛选条件'}
          >
            {view.total === 0 ? (
              <Button type="primary" size="small" onClick={() => {
                close();
                window.dispatchEvent(new CustomEvent('automind:new-session'));
              }}>＋ 新建会话</Button>
            ) : (
              <Button size="small" onClick={resetFilters}>清除搜索与筛选</Button>
            )}
          </Empty>
        </div>
      ) : (
        <div className="ssm-body">
          {view.sections.map((sec) => (
            <section key={sec.key} className="ssm-group">
              {sec.label && (
                <div className="ssm-group-head">
                  <span>{sec.label}</span>
                  <span className="hint-text">{sec.items.length}</span>
                </div>
              )}
              {sec.items.map((c) => (
                <SessionRow
                  key={c.sid + '@' + (c.wsSuffix || '')}
                  c={c}
                  active={activeOf(c)}
                  showWorkspace={showWorkspace}
                  editing={editing === c.sid}
                  onOpen={() => openSession(c)}
                  onStartEdit={() => setEditing(c.sid)}
                  onStopEdit={() => setEditing('')}
                  onRename={(title) => doRename(c, title)}
                  onTogglePin={() => doTogglePin(c)}
                  onArchive={() => doArchive(c)}
                  onUnarchive={() => doUnarchive(c)}
                  onDelete={() => doDelete(c)}
                />
              ))}
            </section>
          ))}
        </div>
      )}
    </Modal>
  );
}
