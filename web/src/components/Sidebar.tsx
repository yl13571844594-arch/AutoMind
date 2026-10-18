// 左侧导航（v1.7.6 重做）：
//   品牌 + 折叠 → 当前工作区 → 新建会话 → 会话列表（含已归档）→ 功能导航（可折叠分组）→ 设置。
// 会话列表把"工作区 = 项目"的隔离落到了看得见的地方：每个工作区有自己的一组会话，
// 归档的会话要点进入该工作区的长期记忆，新会话第一轮自动带上。
// 折叠后变为图标栏，给宽屏阅读/代码留出空间。
import { App, Input, Tooltip } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { EDITION_LABELS, useApp, type View } from '../store/app';
import { relTime, useConv, type Conversation } from '../store/conv';
import { useUi } from '../store/ui';
import SafeDropdown from './ui/SafeDropdown';

type NavItem = [View, string, string, string?];

const NAV_WORK: NavItem[] = [
  ['chat', '💬', '对话工作台'],
  ['workflow', '🔀', '工作流'],
  ['plan', '📋', '计划视图'],
  ['tools', '🔧', '工具面板'],
  ['kb', '📚', '知识库'],
  ['experts', '🎓', '专家市场'],
  ['team', '👥', '团队'],
];
const NAV_SYS: NavItem[] = [
  ['observe', '📈', '观测中心'],
  ['stats', '📊', '统计分析'],
  ['router', '🧭', '路由与成本', 'model_router'],
  ['schedule', '⏰', '定时任务', 'scheduler'],
  ['history', '📜', '任务历史'],
  ['audit', '🛡️', '安全审计'],
];

function useStoredFlag(key: string, init: boolean): [boolean, (v: boolean) => void] {
  const [v, setV] = useState<boolean>(() => {
    try { const s = localStorage.getItem(key); return s === null ? init : s === '1'; } catch { return init; }
  });
  return [v, (nv) => { setV(nv); try { localStorage.setItem(key, nv ? '1' : '0'); } catch { /* ignore */ } }];
}

function ConvRow({ c, active, editing, onEdit, onDone }: {
  c: Conversation; active: boolean; editing: boolean;
  onEdit: () => void; onDone: () => void;
}) {
  const { modal } = App.useApp();
  const conv = useConv.getState();
  const [draft, setDraft] = useState(c.title);
  useEffect(() => { if (editing) setDraft(c.title); }, [editing, c.title]);

  const items = c.archived ? [
    { key: 'cont', label: '↪ 接着聊（新会话带上要点）', onClick: () => conv.create({ continueFrom: c }) },
    { key: 'open', label: '👁 打开查看', onClick: () => conv.switchTo(c) },
    { key: 'restore', label: '📤 恢复到会话列表', onClick: () => conv.unarchive(c) },
    ...(c.suffix ? [{ type: 'divider' as const }, {
      key: 'del', danger: true, label: '🗑 删除',
      onClick: () => modal.confirm({
        title: `删除会话「${c.title}」？`, content: '会话内容将被删除且无法恢复；已写入长期记忆的要点保留。',
        okButtonProps: { danger: true }, onOk: () => conv.remove(c),
      }),
    }] : []),
  ] : [
    { key: 'rename', label: '✏️ 重命名', onClick: onEdit },
    { key: 'pin', label: c.pinned ? '📌 取消置顶' : '📌 置顶', onClick: () => conv.togglePin(c) },
    { key: 'cont', label: '↪ 接着聊（新会话带上要点）', onClick: () => conv.create({ continueFrom: c }) },
    { key: 'archive', label: '🗄 归档（要点写入项目记忆）', onClick: () => conv.archive(c) },
    ...(c.suffix ? [{ type: 'divider' as const }, {
      key: 'del', danger: true, label: '🗑 删除',
      onClick: () => modal.confirm({
        title: `删除会话「${c.title}」？`, content: '会话内容将被删除且无法恢复。若想保留要点，请改用「归档」。',
        okButtonProps: { danger: true }, onOk: () => conv.remove(c),
      }),
    }] : []),
  ];

  if (editing) {
    return (
      <div className="conv-row editing">
        <Input size="small" autoFocus value={draft} maxLength={60}
          onChange={(e) => setDraft(e.target.value)}
          onPressEnter={() => { conv.rename(c, draft); onDone(); }}
          onBlur={() => { conv.rename(c, draft); onDone(); }}
          onKeyDown={(e) => { if (e.key === 'Escape') onDone(); }} />
      </div>
    );
  }

  return (
    <div className={'conv-row' + (active ? ' active' : '') + (c.archived ? ' archived' : '')}
      onClick={() => conv.switchTo(c)} onDoubleClick={c.archived ? undefined : onEdit}
      title={c.summary ? `要点：${c.summary}` : c.title}>
      <span className="conv-dot" data-running={c.running ? '1' : undefined} />
      <div className="conv-main">
        <div className="conv-title">{c.pinned && <span className="conv-pin">📌</span>}{c.title}</div>
        <div className="conv-meta">
          {c.turns ? `${c.turns} 轮 · ` : ''}{relTime(c.updated)}
        </div>
      </div>
      {/* v1.11.8：与「＋」「设置」同样走 SafeDropdown —— 部分电脑上 antd 弹层不可见，点「⋯」像没反应 */}
      <SafeDropdown menu={{ items }} placement="bottomRight" overlayClassName="conv-menu">
        <button className="conv-more" onClick={(e) => e.stopPropagation()}
          onDoubleClick={(e) => e.stopPropagation()} aria-label="会话操作">⋯</button>
      </SafeDropdown>
    </div>
  );
}

export default function Sidebar() {
  const { message } = App.useApp();
  const view = useApp((s) => s.view);
  const edition = useApp((s) => s.edition);
  const version = useApp((s) => s.version);
  const theme = useApp((s) => s.theme);
  const wsActive = useApp((s) => s.wsActive);
  const wsSuffix = useApp((s) => s.wsSuffix);
  const convSuffix = useApp((s) => s.convSuffix);
  const running = useApp((s) => s.running);
  const collapsed = useApp((s) => s.sidebarCollapsed);
  const toggleSidebar = useApp((s) => s.toggleSidebar);
  const featureOn = useApp((s) => s.featureOn);
  const setView = useApp((s) => s.setView);
  const openModal = useUi((s) => s.openModal);
  const { items, loaded, error, load, create } = useConv();
  const [editing, setEditing] = useState('');
  const [showArchived, setShowArchived] = useStoredFlag('automind_nav_archived', false);
  const [workOpen, setWorkOpen] = useStoredFlag('automind_nav_work', true);
  // v1.11.1：「系统」分组默认收起 —— 观测 / 统计 / 审计这些是偶尔看一眼的页面，
  // 常驻展开把会话列表挤到很短。换了存储键，让老用户也拿到新的默认值。
  const [sysOpen, setSysOpen] = useStoredFlag('automind_nav_sys_v2', false);

  // 工作区切换 → 换成该工作区的会话列表；任务结束 → 刷新轮数/标题
  useEffect(() => { load(); }, [wsSuffix, load]);
  useEffect(() => { if (!running) load(); }, [running, load]);

  // mod+N（及顶栏快捷入口）= 新建会话
  useEffect(() => {
    const fn = () => create();
    window.addEventListener('automind:new-session', fn);
    return () => window.removeEventListener('automind:new-session', fn);
  }, [create]);

  const active = useMemo(() => items.filter((c) => !c.archived), [items]);
  const archived = useMemo(() => items.filter((c) => c.archived), [items]);

  const editionColor = edition === 'enterprise' ? 'var(--purple)'
    : edition === 'pro' ? 'var(--accent)' : 'var(--text3)';

  const navBtn = ([v, icon, label, feature]: NavItem) => {
    const locked = feature && !featureOn(feature) && v !== 'router';
    const btn = (
      <button key={v} onClick={() => setView(v)} aria-label={label}
        className={'nav-btn' + (view === v ? ' active' : '')}>
        <span className="nav-ico">{icon}</span>
        {!collapsed && <span className="nav-label">{label}</span>}
        {!collapsed && locked && <span title="专业版功能" className="nav-lock">🔒</span>}
      </button>
    );
    return collapsed ? <Tooltip key={v} title={label} placement="right">{btn}</Tooltip> : btn;
  };

  // 收起时仍显示当前所在的系统页，免得"我在哪"看不出来
  const sysVisible = sysOpen ? NAV_SYS : NAV_SYS.filter(([v]) => v === view);
  const sysActive = NAV_SYS.some(([v]) => v === view);

  const settingsItems = [
    {
      key: 'license',
      label: edition === 'community' ? '⭐ 升级专业版 / 企业版' : `⭐ 许可证（${EDITION_LABELS[edition] || edition}）`,
      onClick: () => openModal('license'),
    },
    { type: 'divider' as const },
    { key: 'grp1', type: 'group' as const, label: '配置' },
    { key: 'model', label: '🖥 模型配置', onClick: () => openModal('model') },
    { key: 'apikeys', label: '🔑 API Keys', onClick: () => openModal('apikeys') },
    { key: 'general', label: '⚙ 通用设置', onClick: () => openModal('general') },
    { key: 'bots', label: '🤖 机器人（飞书 / 钉钉 / 企微）', onClick: () => openModal('bots') },
    { key: 'integrations', label: '🔌 Agent 集成', onClick: () => openModal('integrations') },
    { type: 'divider' as const },
    { key: 'grp2', type: 'group' as const, label: '外观与环境' },
    {
      key: 'theme',
      label: theme === 'light' ? '🌙 切换到深色模式' : '☀️ 切换到浅色模式',
      onClick: () => { useApp.getState().toggleTheme(); message.info(theme === 'light' ? '已切换到深色模式' : '已切换到浅色模式'); },
    },
    { key: 'workspaces', label: '🗂 工作区管理', onClick: () => openModal('workspaces') },
    { type: 'divider' as const },
    { key: 'update', label: '🔄 检查更新', onClick: () => openModal('update') },
    { key: 'shortcuts', label: '⌨ 键盘快捷键', onClick: () => openModal('shortcuts') },
    { key: 'tour', label: '🧭 新手引导', onClick: () => openModal('tour') },
    { key: 'manual', label: '📖 使用手册', onClick: () => window.open('/manual', '_blank') },
  ];

  const groupHead = (label: string, open: boolean, toggle: () => void, extra?: React.ReactNode) => (
    <div className="nav-group" onClick={toggle} role="button" aria-expanded={open}>
      <span className={'nav-caret' + (open ? ' open' : '')}>▸</span>
      <span>{label}</span>
      {extra && <span className="nav-group-extra">{extra}</span>}
    </div>
  );

  return (
    <aside className={'sidebar' + (collapsed ? ' collapsed' : '')}>
      <div className="sb-brand">
        <span className="sb-logo" />
        {!collapsed && <>
          <b>AutoMind</b>
          <small className="sb-ver">{version ? 'v' + version : ''}</small>
          <Tooltip title={edition === 'community' ? '升级到专业版 / 企业版' : '查看许可证'} placement="bottom">
            <button className="sb-edition" style={{ color: editionColor, borderColor: editionColor }}
              onClick={() => openModal('license')}>
              {EDITION_LABELS[edition] || edition}{edition === 'community' ? ' ↑' : ''}
            </button>
          </Tooltip>
        </>}
        <Tooltip title={collapsed ? '展开侧栏' : '收起侧栏'} placement="right">
          <button className="sb-collapse" onClick={toggleSidebar} aria-label="折叠侧栏">
            {collapsed ? '»' : '«'}
          </button>
        </Tooltip>
      </div>

      {collapsed ? (
        <div className="sb-rail-actions">
          <Tooltip title={`工作区：${wsActive || '默认'}`} placement="right">
            <button className="nav-btn" onClick={() => openModal('workspaces')} aria-label="工作区"><span className="nav-ico">🗂</span></button>
          </Tooltip>
          <Tooltip title="新建会话（Ctrl+N）" placement="right">
            <button className="nav-btn" onClick={() => create()} aria-label="新建会话"><span className="nav-ico">＋</span></button>
          </Tooltip>
        </div>
      ) : (
        <>
          <button className="ws-chip" onClick={() => openModal('workspaces')} title="切换 / 管理工作区（每个工作区 = 独立目录 + 独立会话与记忆）">
            <span>🗂</span>
            <span className="ws-chip-name">{wsActive || '默认工作区'}</span>
            <span className="ws-chip-caret">⇅</span>
          </button>
          <button className="new-conv-btn" onClick={() => create()} disabled={running}
            title={running ? '任务执行中，结束后可新建' : '新建会话（Ctrl+N）'}>
            <span>＋</span><span>新建会话</span>
          </button>

          <div className="sb-scroll">
            <div className="nav-group static">
              <span>会话</span>
              <span className="nav-group-extra">{active.length || ''}</span>
            </div>
            <div className="conv-list">
              {error && !loaded && (
                <div className="hint-text" style={{ padding: '4px 12px' }}>
                  会话列表加载失败 <a onClick={() => load()}>重试</a>
                </div>
              )}
              {active.map((c) => (
                <ConvRow key={c.sid} c={c} active={c.suffix === convSuffix}
                  editing={editing === c.sid} onEdit={() => setEditing(c.sid)} onDone={() => setEditing('')} />
              ))}
            </div>
            {archived.length > 0 && (
              <>
                {groupHead(`已归档`, showArchived, () => setShowArchived(!showArchived),
                  archived.length)}
                {showArchived && (
                  <div className="conv-list">
                    {archived.map((c) => (
                      <ConvRow key={c.sid} c={c} active={c.suffix === convSuffix}
                        editing={false} onEdit={() => undefined} onDone={() => undefined} />
                    ))}
                  </div>
                )}
              </>
            )}

            <div className="sb-sep" />
            {groupHead('工作台', workOpen, () => setWorkOpen(!workOpen))}
            {workOpen && NAV_WORK.map(navBtn)}
            {groupHead('系统', sysOpen, () => setSysOpen(!sysOpen),
              !sysOpen && <span className="nav-group-hint">{sysActive ? '' : `${NAV_SYS.length} 项`}</span>)}
            {sysVisible.map(navBtn)}
          </div>
        </>
      )}

      {collapsed && (
        <div className="sb-scroll">
          <div className="sb-sep" />
          {NAV_WORK.map(navBtn)}
          <Tooltip title={sysOpen ? '收起系统菜单' : '展开系统菜单（观测 / 统计 / 定时 / 历史 / 审计）'} placement="right">
            <button className={'nav-btn rail-sys-toggle' + (sysOpen ? ' open' : '')}
              onClick={() => setSysOpen(!sysOpen)} aria-label="系统菜单" aria-expanded={sysOpen}>
              <span className="nav-ico">{sysOpen ? '▴' : '⋯'}</span>
            </button>
          </Tooltip>
          {sysVisible.map(navBtn)}
        </div>
      )}

      {/* v1.11.4：SafeDropdown —— 旧内核 / antd 弹层不可见时自动换成兼容菜单，保证点了就有内容 */}
      <SafeDropdown menu={{ items: settingsItems }} placement="topLeft" overlayClassName="settings-menu">
        <button className="settings-btn" aria-label="设置">
          <span>⚙️</span>
          {!collapsed && <><span>设置</span><span style={{ marginLeft: 'auto', color: 'var(--text3)' }}>▴</span></>}
        </button>
      </SafeDropdown>
      {!collapsed && (
        <div className="hint-text" style={{ textAlign: 'center', padding: '8px 0 0' }}>
          本地运行 · 数据不上传
        </div>
      )}
    </aside>
  );
}
