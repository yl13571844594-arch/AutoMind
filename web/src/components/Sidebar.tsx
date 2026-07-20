// 左侧导航：工作区视图 + 系统视图 + 左下角设置菜单（与经典版布局一致）。
import { App, Dropdown } from 'antd';
import { EDITION_LABELS, useApp, type View } from '../store/app';
import { useUi } from '../store/ui';

const NAV_WORK: [View, string, string][] = [
  ['chat', '💬', '对话工作台'],
  ['plan', '📋', '计划视图'],
  ['tools', '🔧', '工具面板'],
  ['kb', '📚', '知识库'],
  ['experts', '🎓', '专家市场'],
  ['team', '👥', '团队'],
];
const NAV_SYS: [View, string, string, string?][] = [
  ['stats', '📊', '统计分析'],
  ['router', '🧭', '路由与成本', 'model_router'],
  ['schedule', '⏰', '定时任务', 'scheduler'],
  ['history', '📜', '任务历史'],
  ['audit', '🛡️', '安全审计'],
];

export default function Sidebar() {
  const { message } = App.useApp();
  const view = useApp((s) => s.view);
  const edition = useApp((s) => s.edition);
  const version = useApp((s) => s.version);
  const theme = useApp((s) => s.theme);
  const featureOn = useApp((s) => s.featureOn);
  const setView = useApp((s) => s.setView);
  const openModal = useUi((s) => s.openModal);

  const editionColor = edition === 'enterprise' ? 'var(--purple)'
    : edition === 'pro' ? 'var(--accent)' : 'var(--text3)';

  const navBtn = ([v, icon, label, feature]: [View, string, string, string?]) => {
    const locked = feature && !featureOn(feature) && v !== 'router';
    return (
      <button key={v} onClick={() => setView(v)}
        className={'nav-btn' + (view === v ? ' active' : '')}>
        <span>{icon}</span><span style={{ flex: 1 }}>{label}</span>
        {locked && <span title="专业版功能">🔒</span>}
      </button>
    );
  };

  const settingsItems = [
    { key: 'grp1', type: 'group' as const, label: '配置' },
    { key: 'model', label: '🖥 模型配置', onClick: () => openModal('model') },
    { key: 'apikeys', label: '🔑 API Keys', onClick: () => openModal('apikeys') },
    { key: 'general', label: '⚙ 通用设置', onClick: () => openModal('general') },
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
    { key: 'tour', label: '🧭 新手引导', onClick: () => openModal('tour') },
    { key: 'manual', label: '📖 使用手册', onClick: () => window.open('/manual', '_blank') },
  ];

  return (
    <aside className="sidebar">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '2px 8px 14px' }}>
        <span style={{
          width: 12, height: 12, borderRadius: '50%', background: 'var(--accent-grad)',
          boxShadow: '0 0 12px var(--accent-glow)',
        }} />
        <b style={{ fontSize: '1.02em' }}>AutoMind</b>
        <small style={{ color: 'var(--text3)' }}>{version ? 'v' + version : ''}</small>
        <small style={{
          marginLeft: 'auto', color: editionColor, border: `1px solid ${editionColor}`,
          borderRadius: 8, padding: '1px 7px', fontSize: '.68em',
        }}>{EDITION_LABELS[edition] || edition}</small>
      </div>

      <div className="hint-text" style={{ padding: '0 12px 4px' }}>工作区</div>
      {NAV_WORK.map((n) => navBtn([n[0], n[1], n[2]]))}
      <div className="hint-text" style={{ padding: '12px 12px 4px' }}>系统</div>
      {NAV_SYS.map(navBtn)}

      <div style={{ flex: 1 }} />
      <Dropdown menu={{ items: settingsItems }} placement="topLeft" trigger={['click']}>
        <button className="settings-btn">
          <span>⚙️</span><span>设置</span><span style={{ marginLeft: 'auto', color: 'var(--text3)' }}>▴</span>
        </button>
      </Dropdown>
      <div className="hint-text" style={{ textAlign: 'center', padding: '10px 0 2px' }}>
        本地运行 · 数据不上传
      </div>
    </aside>
  );
}
