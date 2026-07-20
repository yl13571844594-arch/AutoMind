// 🔄 检查更新：GitHub Releases 版本检查；桌面版一键静默升级（含下载进度），
// pip/源码模式给出升级命令。
import { App, Button, Modal, Progress, Space, Tag, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import { useUi } from '../../store/ui';

const { Text, Paragraph } = Typography;

const fmtSize = (n: number) => (n > 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + ' MB' : (n / 1024).toFixed(0) + ' KB');

export default function UpdateModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'update';
  const close = useUi((s) => s.closeModal);
  const [info, setInfo] = useState<any>(null);
  const [checking, setChecking] = useState(false);
  const [applyState, setApplyState] = useState<any>(null);
  const pollRef = useRef<number | null>(null);

  const check = async (force = false) => {
    setChecking(true);
    setInfo(await apiGet(`/update/check${force ? '?force=true' : ''}`).catch(() => ({ error: '请求失败' })));
    setChecking(false);
  };
  useEffect(() => { if (open) { check(); setApplyState(null); } }, [open]);
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const applying = applyState && ['downloading', 'verifying', 'installing'].includes(applyState.status);

  const apply = async () => {
    const r = await apiPost('/update/apply');
    if (r.error) { message.error(r.error); return; }
    setApplyState({ status: 'downloading', progress: 0 });
    pollRef.current = window.setInterval(async () => {
      const s = await apiGet('/update/state').catch(() => null);
      if (!s) {
        // 服务已退出 → 安装器接管，应用即将自动重启
        if (pollRef.current) window.clearInterval(pollRef.current);
        setApplyState({ status: 'restarting' });
        return;
      }
      setApplyState(s);
      if (s.status === 'error' && pollRef.current) window.clearInterval(pollRef.current);
    }, 600);
  };

  const desktop = info?.mode === 'desktop';

  return (
    <Modal title="🔄 检查更新" open={open} width={560}
      onCancel={applying ? undefined : close}
      closable={!applying} maskClosable={!applying} footer={null}>
      {!info ? <Paragraph type="secondary">正在检查…</Paragraph> : (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <div>
            当前版本 <Tag>v{info.current}</Tag>
            {info.error ? <Text type="danger">{info.error}</Text>
              : info.available
                ? <>→ 发现新版本 <Tag color="blue">v{info.latest}</Tag>
                  {info.asset_size > 0 && <span className="hint-text">安装包 {fmtSize(info.asset_size)}</span>}</>
                : <Tag color="green">已是最新</Tag>}
            {info.cached && <span className="hint-text" style={{ marginLeft: 6 }}>（缓存结果）</span>}
          </div>

          {info.available && info.notes && (
            <div style={{
              border: '1px solid var(--border)', borderRadius: 10, padding: 12,
              maxHeight: 220, overflowY: 'auto', fontSize: '.85em',
              whiteSpace: 'pre-wrap', color: 'var(--text2)',
            }}>{info.notes}</div>
          )}

          {applyState && (
            applyState.status === 'error'
              ? <Text type="danger">{applyState.error}</Text>
              : applyState.status === 'restarting' || applyState.status === 'installing'
                ? <div style={{ textAlign: 'center' }}>
                  <Progress percent={100} status="active" />
                  <Text strong>✅ 下载完成，正在静默安装 — 应用将自动重启，请稍候…</Text>
                </div>
                : <div>
                  <Progress percent={applyState.progress || 0} status="active" />
                  <span className="hint-text">
                    {applyState.status === 'verifying' ? '正在校验安装包签名…' : '正在下载更新…'}
                  </span>
                </div>
          )}

          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            {!applying && <Button loading={checking} onClick={() => check(true)}>重新检查</Button>}
            {info.release_url && !applying && (
              <Button onClick={() => window.open(info.release_url, '_blank')}>查看发布页 ↗</Button>
            )}
            {info.available && (
              desktop
                ? <Button type="primary" loading={!!applying} onClick={apply}>
                  {applying ? '更新中…' : '⬇ 立即更新（自动重启）'}
                </Button>
                : <Button type="primary" onClick={() => {
                  navigator.clipboard?.writeText('pip install -U "automind-agent[web]"');
                  message.success('升级命令已复制：pip install -U "automind-agent[web]"');
                }}>复制升级命令</Button>
            )}
          </Space>
          {info.available && desktop && !applying && (
            <Paragraph type="secondary" style={{ fontSize: '.76em', margin: 0 }}>
              更新包来自 GitHub Releases，安装前自动校验数字签名；升级过程静默完成，你的配置与数据全部保留。
            </Paragraph>
          )}
        </Space>
      )}
    </Modal>
  );
}
