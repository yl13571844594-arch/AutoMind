import { App as AntApp, ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect } from 'react';
import Header from './components/Header';
import ChatPanel from './components/chat/ChatPanel';
import ApprovalModal from './components/modals/ApprovalModal';
import PreviewModal from './components/modals/PreviewModal';
import SettingsModals from './components/modals/SettingsModals';
import TemplatesModal from './components/modals/TemplatesModal';
import TourModal from './components/modals/TourModal';
import WorkspacesModal from './components/modals/WorkspacesModal';
import RightPanel from './components/right/RightPanel';
import Sidebar from './components/Sidebar';
import AuditView from './components/views/AuditView';
import ExpertsView from './components/views/ExpertsView';
import HistoryView from './components/views/HistoryView';
import KbView from './components/views/KbView';
import PlanView from './components/views/PlanView';
import RouterView from './components/views/RouterView';
import ScheduleView from './components/views/ScheduleView';
import StatsView from './components/views/StatsView';
import TeamView from './components/views/TeamView';
import ToolsView from './components/views/ToolsView';
import { useApp } from './store/app';
import { useUi } from './store/ui';
import { connectWS } from './ws';

const VIEWS: Record<string, React.ComponentType> = {
  plan: PlanView, tools: ToolsView, experts: ExpertsView, team: TeamView,
  kb: KbView, stats: StatsView, schedule: ScheduleView, history: HistoryView,
  audit: AuditView, router: RouterView,
};

export default function App() {
  const theme = useApp((s) => s.theme);
  const view = useApp((s) => s.view);

  useEffect(() => {
    document.body.classList.toggle('light', theme === 'light');
  }, [theme]);

  useEffect(() => {
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

  const ViewComp = view !== 'chat' ? VIEWS[view] : null;

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: theme === 'dark' ? '#7b9fff' : '#4a6fe8',
          borderRadius: 9,
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
        <div className="app-shell">
          <Sidebar />
          <div className="app-main">
            <Header />
            <div className="app-body">
              {ViewComp ? (
                <div className="messages" style={{ flex: 1 }}><ViewComp /></div>
              ) : (
                <ChatPanel />
              )}
              <RightPanel />
            </div>
          </div>
        </div>
        <SettingsModals />
        <WorkspacesModal />
        <TemplatesModal />
        <TourModal />
        <ApprovalModal />
        <PreviewModal />
      </AntApp>
    </ConfigProvider>
  );
}
