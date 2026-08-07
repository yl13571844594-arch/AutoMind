import { App as AntApp, ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect } from 'react';
import ConnectionBanner from './components/ConnectionBanner';
import Header from './components/Header';
import ChatPanel from './components/chat/ChatPanel';
import ApprovalModal from './components/modals/ApprovalModal';
import PreviewModal from './components/modals/PreviewModal';
import SettingsModals from './components/modals/SettingsModals';
import TemplatesModal from './components/modals/TemplatesModal';
import TourModal from './components/modals/TourModal';
import UpdateModal from './components/modals/UpdateModal';
import WorkspacesModal from './components/modals/WorkspacesModal';
import RightPanel from './components/right/RightPanel';
import Sidebar from './components/Sidebar';
import AuditView from './components/views/AuditView';
import ExpertsView from './components/views/ExpertsView';
import HistoryView from './components/views/HistoryView';
import KbView from './components/views/KbView';
import ObserveView from './components/views/ObserveView';
import PlanView from './components/views/PlanView';
import RouterView from './components/views/RouterView';
import ScheduleView from './components/views/ScheduleView';
import StatsView from './components/views/StatsView';
import TeamView from './components/views/TeamView';
import ToolsView from './components/views/ToolsView';
import ShortcutsModal from './components/modals/ShortcutsModal';
import { installHotkeys } from './lib/hotkeys';
import { useApp } from './store/app';
import { ANTD_BASE_FONT, initPrefs, usePrefs } from './store/prefs';
import { useUi } from './store/ui';
import { connectWS, sendStop } from './ws';

const VIEWS: Record<string, React.ComponentType> = {
  plan: PlanView, tools: ToolsView, experts: ExpertsView, team: TeamView,
  kb: KbView, stats: StatsView, schedule: ScheduleView, history: HistoryView,
  audit: AuditView, router: RouterView, observe: ObserveView,
};

// 启动后静默检查更新（每会话一次，走服务端 6h 缓存）；有新版给出可点通知
function useUpdateNotify() {
  const { notification } = AntApp.useApp();
  useEffect(() => {
    const t = window.setTimeout(async () => {
      try {
        const r = await (await fetch('/api/update/check')).json();
        if (r.available && !sessionStorage.getItem('automind_update_notified')) {
          sessionStorage.setItem('automind_update_notified', '1');
          notification.info({
            key: 'update', message: `发现新版本 v${r.latest}`,
            description: '点击查看更新内容并一键升级',
            placement: 'bottomRight', duration: 8,
            onClick: () => { notification.destroy('update'); useUi.getState().openModal('update'); },
            style: { cursor: 'pointer' },
          });
        }
      } catch { /* 离线等场景静默 */ }
    }, 3000);
    return () => window.clearTimeout(t);
  }, []);
}

function UpdateNotifier() { useUpdateNotify(); return null; }

export default function App() {
  const theme = useApp((s) => s.theme);
  const view = useApp((s) => s.view);
  const fontScale = usePrefs((s) => s.fontScale);

  useEffect(() => {
    document.body.classList.toggle('light', theme === 'light');
  }, [theme]);

  useEffect(() => {
    initPrefs();                       // 字号/动效偏好尽早生效，避免首屏跳字号
    const app = useApp.getState();
    app.loadStatus();
    app.loadHealth();
    app.refreshExpert();
    connectWS();
    // 首次访问自动弹新手引导
    if (!localStorage.getItem('automind_onboarded')) {
      setTimeout(() => useUi.getState().openModal('tour'), 600);
    }
  }, []);

  // ── 全局快捷键 ──
  useEffect(() => {
    const ui = () => useUi.getState();
    const app = () => useApp.getState();
    const prefs = () => usePrefs.getState();
    const step = (d: number) => prefs().setFontScale(
      +(prefs().fontScale + d).toFixed(2));

    return installHotkeys({
      // 复用顶栏「🔄 新会话」那一份实现（带确认弹窗 + 清服务端历史 + 重置面板），
      // 不在这里另写一份 —— 清会话是破坏性操作，两处实现迟早会走样。
      newSession: () => window.dispatchEvent(new CustomEvent('automind:new-session')),
      templates: () => ui().openModal('templates'),
      settings: () => ui().openModal('general'),
      workspaces: () => ui().openModal('workspaces'),
      help: () => ui().openModal('shortcuts'),
      toggleTheme: () => app().toggleTheme(),
      fontUp: () => step(0.05),
      fontDown: () => step(-0.05),
      fontReset: () => prefs().setFontScale(1),
      focusInput: () => {
        const el = document.querySelector<HTMLTextAreaElement>('.chat-input textarea, #user-input');
        el?.focus();
      },
      stop: () => {
        // Esc 双职责：有弹窗先关弹窗，否则停任务 —— 符合直觉的"退一步"
        if (ui().modal || ui().preview) { ui().closeModal(); ui().closePreview(); return; }
        // 中断走 WebSocket（后端没有 /api/stop 这个路由，走 HTTP 会静默 404）
        if (app().running) sendStop();
      },
      mode1: () => app().setMode('chat'),
      mode2: () => app().setMode('work'),
      mode3: () => app().setMode('coding'),
      viewChat: () => app().setView('chat'),
      viewPlan: () => app().setView('plan'),
      viewTools: () => app().setView('tools'),
      viewHistory: () => app().setView('history'),
    }, {
      // Esc 之外的快捷键在弹窗打开时让位，避免叠加触发
      modalOpen: () => !!(useUi.getState().modal || useUi.getState().preview),
    });
  }, []);

  const ViewComp = view !== 'chat' ? VIEWS[view] : null;

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: theme === 'dark' ? '#7b9fff' : '#4a6fe8',
          borderRadius: 9,
          // antd 用 px token，不随根字号缩放，必须显式跟上，否则调字号时
          // 自有样式变大、antd 组件纹丝不动，界面会割裂
          fontSize: Math.round(ANTD_BASE_FONT * fontScale),
          fontFamily: "'Inter','Segoe UI',system-ui,-apple-system,'Microsoft YaHei',sans-serif",
          ...(theme === 'dark' ? {
            colorBgContainer: '#0e1220', colorBgElevated: '#161c2e',
            colorBorder: '#262f47', colorBorderSecondary: '#1f2740',
            colorBgLayout: '#060913',
          } : {}),
        },
      }}
    >
      <AntApp>
        <UpdateNotifier />
        <div className="app-shell">
          <Sidebar />
          <div className="app-main">
            <Header />
            <ConnectionBanner />
            <div className="app-body">
              {/* 黑屏闪烁的根因修复：此处原本是 `ViewComp ? <View/> : <ChatPanel/>`，
                  每次切视图都会把整个 ChatPanel 卸载再重建。会话一长，这次同步
                  卸载+重建要占掉好几帧，期间浏览器没有内容可画，露出近黑的
                  --bg0 底色 —— 用户看到的就是"黑屏闪几下"。
                  改为**始终挂载**、只切显示：DOM 不重建，滚动位置也不再丢。 */}
              <div className="view-slot" style={{ display: ViewComp ? 'none' : 'flex' }}>
                <ChatPanel />
              </div>
              {ViewComp && (
                <div className="messages view-fade" style={{ flex: 1 }}><ViewComp /></div>
              )}
              <RightPanel />
            </div>
          </div>
        </div>
        <SettingsModals />
        <WorkspacesModal />
        <TemplatesModal />
        <TourModal />
        <UpdateModal />
        <ShortcutsModal />
        <ApprovalModal />
        <PreviewModal />
      </AntApp>
    </ConfigProvider>
  );
}
