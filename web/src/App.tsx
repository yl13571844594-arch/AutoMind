import { legacyLogicalPropertiesTransformer, StyleProvider } from '@ant-design/cssinjs';
import { App as AntApp, ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect, useState } from 'react';
import { installGlobalApiErrorToast } from './api/client';
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
import WorkflowView from './components/views/WorkflowView';
import RunWorkflowModal from './components/workflow/RunWorkflowModal';
import GoalsModal from './components/modals/GoalsModal';
import LicenseModal from './components/modals/LicenseModal';
import ScheduleQuickModal from './components/modals/ScheduleQuickModal';
import ShortcutsModal from './components/modals/ShortcutsModal';
import UserQuestionModal from './components/modals/UserQuestionModal';
import { ErrorBoundary } from './components/ui/ErrorBoundary';
import { COMPAT, LEGACY_CSS } from './lib/compat';
import { detectRafStall } from './lib/popupGuard';
import { installHotkeys } from './lib/hotkeys';
import { useApp } from './store/app';
import { ANTD_BASE_FONT, initPrefs, usePrefs } from './store/prefs';
import { useUi } from './store/ui';
import { connectWS, sendStop } from './ws';

const VIEWS: Record<string, React.ComponentType> = {
  plan: PlanView, tools: ToolsView, experts: ExpertsView, team: TeamView,
  kb: KbView, stats: StatsView, schedule: ScheduleView, history: HistoryView,
  audit: AuditView, router: RouterView, observe: ObserveView, workflow: WorkflowView,
};

//: 视图中文名 —— 渲染异常时用它告诉用户"是哪个面板出了问题"
const VIEW_NAMES: Record<string, string> = {
  plan: '计划视图', tools: '工具面板', experts: '专家市场', team: '团队协作',
  kb: '知识库', stats: '统计分析', schedule: '定时任务', history: '任务历史', workflow: '工作流',
  audit: '安全审计', router: '路由与成本', observe: '观测中心',
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

/**
 * 写操作失败的兜底提示。
 *
 * `apiPost` / `apiDelete` 此前拿到 500/403 也照样 `return r.json()`，
 * 于是"保存设置""启用插件""删除文档"失败时界面**一声不吭** ——
 * 用户以为改好了。现在它们会抛 ApiError；就地没接住的，在这里统一 toast。
 */
function ApiErrorToaster() {
  const { message } = AntApp.useApp();
  useEffect(() => installGlobalApiErrorToast(
    (text) => message.error({ content: text, duration: 5 })), [message]);
  return null;
}

export default function App() {
  const theme = useApp((s) => s.theme);
  const view = useApp((s) => s.view);
  const fontScale = usePrefs((s) => s.fontScale);
  const reduceMotion = usePrefs((s) => s.reduceMotion);
  // v1.11.8：rAF 停摆的电脑上关闭 antd 动画（见 lib/popupGuard.detectRafStall）。
  // 启动时测一次，之后每次窗口重新可见时再测（远程桌面 / 窗口遮挡恢复后可能又好了也可能又坏了，
  // 只往"关动画"方向切，不自动切回：关动画只是少了淡入，不影响任何功能）
  const [rafStalled, setRafStalled] = useState(false);
  useEffect(() => {
    let alive = true;
    const probe = () => detectRafStall().then((stalled) => {
      if (stalled && alive) {
        // eslint-disable-next-line no-console
        console.warn('[AutoMind] 检测到页面动画帧停摆，已关闭界面动画以保证菜单 / 下拉框正常显示');
        setRafStalled(true);
      }
    });
    probe();
    const onVis = () => { if (document.visibilityState === 'visible') probe(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { alive = false; document.removeEventListener('visibilitychange', onVis); };
  }, []);
  const motionOff = LEGACY_CSS || reduceMotion || rafStalled;

  useEffect(() => {
    // 两处都要切：html.light 负责首绘前的配色，body.light 保持既有选择器兼容
    document.documentElement.classList.toggle('light', theme === 'light');
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
      // v1.7.6：新建会话（不再是"清空当前会话"）—— 由侧栏会话列表处理，
      // 旧内容保留在原会话里，随时切回去看。
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
    // v1.11.3 浏览器兼容：不支持 :where() / 逻辑属性的内核改用 antd 的兼容输出
    // （见 lib/compat.ts）。否则下拉菜单、弹窗的样式整条失效 —— 点「＋」「设置」看不到内容。
    <StyleProvider hashPriority={COMPAT.where ? 'low' : 'high'}
      transformers={COMPAT.logical ? [] : [legacyLogicalPropertiesTransformer]}>
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
          // 旧内核关闭进出场动画：弹层要等 animationend 才算"出场完成"，动画没跑起来时
          // 菜单 / 弹窗会一直停在 opacity:0 —— 看起来就是"点了没反应"
          // v1.11.8：动画帧停摆 / 用户选了减少动效时同样关闭
          ...(motionOff ? { motion: false } : {}),
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
        <ApiErrorToaster />
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
                <ErrorBoundary name="对话工作台"><ChatPanel /></ErrorBoundary>
              </div>
              {ViewComp && (
                <div className="messages view-fade" style={{ flex: 1 }}>
                  {/* 单个面板的渲染异常只毁掉这一块：侧边栏、顶栏、正在跑的任务
                      都还在，用户可以直接切到别的页面继续用。
                      resetKey=view：切走再切回来时错误自动复位。 */}
                  <ErrorBoundary name={VIEW_NAMES[view] || view} resetKey={view}>
                    <ViewComp />
                  </ErrorBoundary>
                </div>
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
        {/* v1.11.11：模型提问弹窗（ask_user_question）。与审批并列挂在根上：
            任务可能在任何视图下运行，弹窗不能依赖 ChatPanel 是否可见。 */}
        <UserQuestionModal />
        <RunWorkflowModal />
        <GoalsModal />
        <LicenseModal />
        <ScheduleQuickModal />
        <PreviewModal />
      </AntApp>
    </ConfigProvider>
    </StyleProvider>
  );
}
