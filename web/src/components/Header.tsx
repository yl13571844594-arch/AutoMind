// 顶栏：模式切换（3 主模式 + 高级折叠）、审批模式、状态/模型/项目/工作区徽标、
// 模板 / 引导 / 新会话；下方模式提示条（含激活专家 chip + 每日限额提示）。
import { App, Select, Tooltip } from 'antd';
import { useState } from 'react';
import { apiDelete, apiPost } from '../api/client';
import { chatSid, EDITION_LABELS, MODE_FEATURE, MODE_LABELS, useApp, type Mode } from '../store/app';
import { useChat } from '../store/chat';
import { usePanel } from '../store/panel';
import { useUi } from '../store/ui';

const MODE_HINTS: Record<Mode, string> = {
  chat: '💬 对话模式 — 纯多轮对话，不调用工具，响应最快。支持图片输入（视觉模型）。',
  work: '⚙️ 工作模式 — 分层规划 + 工具执行 + 符号验证。会自动建目录、写文件、跑命令完成任务。',
  coding: '💻 编程模式 — ReAct 思考-行动循环，聚焦读写代码、运行命令与测试。',
  multi: '🤝 协同模式 — 多智能体协作：协调者拆解任务，规划/研究/编程/审阅角色分工完成并综合。',
  loop: '🔁 循环模式 — Loop Engineering：自主"行动-观察-修正"闭环，自动迭代直到任务完成或达到停止条件。',
};

export default function Header() {
  const { message, modal } = App.useApp();
  const mode = useApp((s) => s.mode);
  const status = useApp((s) => s.status);
  const wsState = useApp((s) => s.wsState);
  const wsActive = useApp((s) => s.wsActive);
  const edition = useApp((s) => s.edition);
  const featureOn = useApp((s) => s.featureOn);
  const activeExpert = useApp((s) => s.activeExpert);
  const openModal = useUi((s) => s.openModal);
  const [showAdv, setShowAdv] = useState(mode === 'multi' || mode === 'loop');

  const switchMode = async (m: Mode) => {
    if (!featureOn(MODE_FEATURE[m])) {
      message.error(`「${MODE_LABELS[m]}」是专业版功能 — 当前为${EDITION_LABELS[edition]}，请安装 automind-pro 并配置许可证`);
      return;
    }
    await useApp.getState().setMode(m);
    message.info(`已切换到${MODE_LABELS[m]}模式`);
  };

  const modeBtn = (m: Mode, icon: string) => {
    const locked = !featureOn(MODE_FEATURE[m]);
    return (
      <button
        key={m}
        onClick={() => switchMode(m)}
        title={MODE_HINTS[m]}
        style={{
          display: 'flex', alignItems: 'center', gap: 5, padding: '6px 13px',
          border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: '.84em',
          background: mode === m ? 'var(--accent-grad)' : 'transparent',
          color: mode === m ? '#fff' : 'var(--text2)', fontWeight: mode === m ? 600 : 400,
        }}
      >
        <span>{icon}</span><span>{MODE_LABELS[m]}</span>{locked && <span>🔒</span>}
      </button>
    );
  };

  const badge = (text: string, cls: 'ok' | 'warn' | 'err' | 'plain', title?: string, onClick?: () => void) => (
    <Tooltip title={title}>
      <span
        onClick={onClick}
        style={{
          padding: '4px 10px', borderRadius: 8, fontSize: '.74em', cursor: onClick ? 'pointer' : 'default',
          border: '1px solid var(--border)', whiteSpace: 'nowrap', maxWidth: 210,
          overflow: 'hidden', textOverflow: 'ellipsis',
          color: cls === 'ok' ? 'var(--green)' : cls === 'warn' ? 'var(--yellow)' : cls === 'err' ? 'var(--red)' : 'var(--text2)',
          background: cls === 'ok' ? 'var(--green-bg)' : cls === 'warn' ? 'var(--yellow-bg)' : cls === 'err' ? 'var(--red-bg)' : 'var(--bg2)',
        }}
      >{text}</span>
    </Tooltip>
  );

  const modelBadge = () => {
    if (!status) return badge('…', 'plain');
    const label = `${status.provider}/${status.model}`;
    const modeLabel = status.mode_specific ? `${MODE_LABELS[mode]}模式专用 · ` : '默认 · ';
    if (status.llm_ready) return badge(label, 'ok', modeLabel + '已就绪', () => openModal('model'));
    if (status.has_api_key) return badge(label + ' ⚠', 'warn', modeLabel + (status.llm_error || 'LLM 未初始化'), () => openModal('model'));
    return badge('⚠ 未配置', 'err', '未配置 API Key，点击配置', () => openModal('apikeys'));
  };

  const projectName = (status?.project || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '—';
  const quota = status?.quota;
  const quotaBadge = quota && quota.daily_limit != null
    ? badge(`📅 ${quota.daily_used}/${quota.daily_limit}`, quota.daily_used >= quota.daily_limit ? 'err' : 'plain',
      `今日任务 ${quota.daily_used}/${quota.daily_limit} 次（社区版限额，专业版不限）`)
    : null;

  const handleClear = () => {
    modal.confirm({
      title: `清空${MODE_LABELS[mode]}模式会话？`,
      content: '仅清空当前模式的会话内容，不影响其它模式。',
      onOk: async () => {
        if (mode === 'chat') {
          await apiDelete(`/chat/history?session_id=${encodeURIComponent(chatSid())}`).catch(() => {});
        }
        useChat.getState().clearMode(mode);
        usePanel.getState().setPlan(null);
        usePanel.getState().setStats({ steps: 0, backtracks: 0, tokens: 0, duration_ms: 0 });
        message.info(`已清空${MODE_LABELS[mode]}模式会话`);
      },
    });
  };

  return (
    <>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
        borderBottom: '1px solid var(--border)', background: 'var(--bg1)', flexWrap: 'wrap',
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 2, background: 'var(--bg2)',
          borderRadius: 10, padding: 3, border: '1px solid var(--border)',
        }}>
          {modeBtn('chat', '💬')}
          {modeBtn('work', '⚙️')}
          {modeBtn('coding', '💻')}
          <span style={{ width: 1, height: 18, background: 'var(--border)', margin: '0 3px' }} />
          {showAdv ? (
            <>{modeBtn('multi', '🤝')}{modeBtn('loop', '🔁')}</>
          ) : (
            <button onClick={() => setShowAdv(true)} title="展开高级模式（协同 / 循环）"
              style={{ border: 'none', background: 'transparent', color: 'var(--text3)', cursor: 'pointer', padding: '6px 10px', fontSize: '.84em' }}>
              ⋯ 高级
            </button>
          )}
        </div>

        <Select
          size="small"
          value={status?.approval_mode || 'auto'}
          style={{ width: 110 }}
          onChange={async (v) => {
            await apiPost('/config/approval', { approval_mode: v });
            useApp.getState().loadStatus();
            message.info(`审批模式：${({ ask: '询问', auto: '自动', approve_all: '全批准' } as any)[v]}`);
          }}
          options={[
            { value: 'ask', label: '🙋 询问' },
            { value: 'auto', label: '⚡ 自动' },
            { value: 'approve_all', label: '✅ 全批准' },
          ]}
        />

        {wsState === 'connected' && badge('● 已连接', 'ok')}
        {wsState === 'running' && badge('◉ 执行中', 'warn')}
        {wsState === 'disconnected' && badge('○ 未连接', 'plain')}
        {modelBadge()}
        {badge('📁 ' + projectName, 'plain', '项目目录: ' + (status?.project || ''), () => openModal('general'))}
        {badge('🗂 ' + (wsActive || '默认'), 'plain', '工作区（独立目录 + 独立上下文）', () => openModal('workspaces'))}
        {quotaBadge}

        <span style={{ flex: 1 }} />
        <button className="tpl-chip" onClick={() => openModal('templates')}>📚 模板</button>
        <button className="tpl-chip" onClick={() => openModal('tour')}>❓ 引导</button>
        <button className="tpl-chip" style={{ borderColor: 'var(--accent)', color: 'var(--accent)' }} onClick={handleClear}>
          🔄 新会话
        </button>
      </div>

      <div style={{
        padding: '7px 18px', fontSize: '.8em', color: 'var(--text2)',
        borderBottom: '1px solid var(--border)', background: 'var(--bg0)',
        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
      }}>
        <span>{MODE_HINTS[mode]}</span>
        {activeExpert && (
          <b style={{ color: 'var(--purple)' }}>
            {activeExpert.icon} 专家模式：{activeExpert.name}
            <a
              style={{ marginLeft: 6, color: 'var(--text3)', cursor: 'pointer' }}
              onClick={async () => {
                await apiPost('/experts/activate', { id: '' });
                useApp.getState().refreshExpert();
                message.info('已取消专家模式');
              }}
            >✕</a>
          </b>
        )}
      </div>
    </>
  );
}
