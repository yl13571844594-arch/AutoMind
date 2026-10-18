// ⏰ 触发器（v1.9.0，专业版，R-203）：列出一份工作流里声明的定时 / webhook 触发器，
// 启停、看下次运行时间、生成 webhook 令牌、立即触发一次。
//
// 触发器的**声明**在 YAML 里（进版本库、可评审）；这里改的只是运行时状态（启停、令牌）。
// 令牌只在生成那一次显示 —— 服务端只存摘要，丢了只能重置。
import { Alert, App, Button, Empty, Modal, Space, Switch, Table, Tag, Tooltip, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost, errText } from '../../api/client';
import { RUN_STATUS_ZH } from '../../store/workflow';

interface TriggerRow {
  key: string; file: string; workflow: string; id: string; kind: 'cron' | 'webhook';
  cron?: string; description?: string; enabled: boolean; active: boolean;
  blocked_reason?: string; next_run?: number | null; retry?: number; retry_delay?: number;
  last_fired?: number; last_status?: string; last_run_id?: string; last_error?: string;
  fires?: number; hook_path?: string; has_token?: boolean; token_hint?: string; line?: number;
  min_interval?: number;
}

const fmt = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : '—');

const LAST_ZH: Record<string, string> = {
  ...RUN_STATUS_ZH, skipped: '已跳过（上一次仍在运行）', failed_to_start: '未能开始',
};

export default function TriggersTab({ file, declared, onOpenRun }: {
  file: string;
  /** 文件里声明了几个触发器（社区版拿不到运行时状态时也能说清"写了什么"） */
  declared: any[];
  onOpenRun: (runId: string) => void;
}) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<TriggerRow[] | null>(null);
  const [meta, setMeta] = useState<{ active?: number; limit?: number | null; min_interval?: number }>({});
  const [locked, setLocked] = useState('');
  const [err, setErr] = useState('');
  const [token, setToken] = useState<{ token: string; url: string } | null>(null);

  const load = useCallback(() => {
    setErr('');
    apiGet(`/workflow/triggers?file=${encodeURIComponent(file)}`)
      .then((d) => {
        // 社区版：服务端返回 403 + feature，客户端把它当数据交回来（不当错误抛）
        if (d?.feature) { setLocked(String(d.error || '')); setRows([]); return; }
        setRows(d.triggers || []);
        setMeta({ active: d.active, limit: d.limit, min_interval: d.min_interval });
        setLocked('');
      })
      .catch((e) => { setErr(errText(e)); setRows([]); });
  }, [file]);
  useEffect(() => { load(); }, [load]);

  const toggle = async (r: TriggerRow, enabled: boolean) => {
    try {
      await apiPost(`/workflow/triggers/${r.key}/toggle`, { enabled });
      message.success(enabled ? '已启用' : '已停用');
      load();
    } catch (e) { message.error(errText(e), 6); }
  };
  const fireNow = async (r: TriggerRow) => {
    try {
      const d = await apiPost(`/workflow/triggers/${r.key}/run`);
      message.success('已触发一次');
      if (d.run_id) onOpenRun(d.run_id);
      load();
    } catch (e) { message.error(errText(e), 6); }
  };
  const rotate = (r: TriggerRow) => Modal.confirm({
    title: r.has_token ? '重置 webhook 令牌？' : '生成 webhook 令牌？',
    content: r.has_token ? '旧令牌立即失效，正在用它调用的外部系统需要换成新令牌。' : '令牌只显示这一次，请立即保存。',
    okText: r.has_token ? '重置' : '生成',
    onOk: async () => {
      try {
        const d = await apiPost(`/workflow/triggers/${r.key}/token`);
        setToken({ token: d.token, url: `${window.location.origin}${d.hook_path}` });
        load();
      } catch (e) { message.error(errText(e), 6); }
    },
  });

  if (locked) {
    return (
      <div>
        <Alert type="info" showIcon message="触发器为专业版功能" description={
          <div style={{ lineHeight: 1.9 }}>
            {locked}
            <div className="hint-text">
              社区版的自动化方式：用外部 cron / CI 调 <code>POST /api/workflow/run</code>。
              {declared.length > 0 && `这份工作流声明了 ${declared.length} 个触发器，社区版在校验阶段就会拦下。`}
            </div>
          </div>} />
      </div>
    );
  }

  return (
    <div>
      <Space style={{ marginBottom: 8 }} wrap>
        <Button size="small" onClick={load}>刷新</Button>
        <span className="hint-text">
          活跃触发器 {meta.active ?? 0}{meta.limit != null ? ` / 上限 ${meta.limit}` : '（不限）'}
          {meta.min_interval ? ` · 定时最小间隔 ${Math.round(meta.min_interval / 60)} 分钟` : ''}
          · 触发器在 YAML 的 triggers 里声明，这里只管启停与令牌
        </span>
      </Space>
      {err && <div className="wf-step-error">{err}</div>}
      {rows && !rows.length && !err && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={
          <span className="hint-text">这份工作流没有声明触发器。在 YAML 顶层加上
            <code>{' triggers: [{cron: "0 9 * * 1-5"}]'}</code> 或 <code>{'[{webhook: true}]'}</code></span>} />
      )}
      {rows && rows.length > 0 && (
        <Table size="small" pagination={false} rowKey="key" dataSource={rows}
          columns={[
            { title: '触发器', width: 220, render: (_: any, r: TriggerRow) => (
              <div>
                <div>{r.kind === 'cron' ? '⏰' : '🔗'} <code>{r.id}</code>
                  {r.line ? <span className="hint-text">（第 {r.line} 行）</span> : null}</div>
                <div className="hint-text mono">{r.kind === 'cron' ? r.cron : 'webhook'}</div>
                {r.description && <div className="hint-text">{r.description}</div>}
                {!!r.retry && <div className="hint-text">失败后自动续跑 {r.retry} 次（间隔 {r.retry_delay}s）</div>}
              </div>) },
            { title: '状态', width: 150, render: (_: any, r: TriggerRow) => (
              <div>
                <Switch size="small" checked={r.enabled} onChange={(v) => toggle(r, v)} />{' '}
                {r.active ? <Tag color="success">生效中</Tag>
                  : <Tooltip title={r.blocked_reason}><Tag color="warning">未生效</Tag></Tooltip>}
                {!r.active && r.blocked_reason && r.blocked_reason !== '已停用' && (
                  <div className="wf-step-error" style={{ fontSize: '.8em' }}>{r.blocked_reason}</div>
                )}
              </div>) },
            { title: '下次 / 最近', render: (_: any, r: TriggerRow) => (
              <div className="hint-text">
                {r.kind === 'cron' && <div>下次：{r.active ? fmt(r.next_run) : '—'}</div>}
                <div>最近：{fmt(r.last_fired)}{r.last_status ? ` · ${LAST_ZH[r.last_status] || r.last_status}` : ''}
                  {r.fires ? ` · 共 ${r.fires} 次` : ''}</div>
                {r.last_error && <div className="wf-step-error">{r.last_error}</div>}
                {r.last_run_id && (
                  <Button size="small" type="link" style={{ padding: 0 }}
                    onClick={() => onOpenRun(r.last_run_id!)}>查看最近一次运行</Button>
                )}
              </div>) },
            { title: '', width: 200, render: (_: any, r: TriggerRow) => (
              <Space direction="vertical" size={2}>
                <Button size="small" disabled={!r.active} onClick={() => fireNow(r)}>▶ 立即触发</Button>
                {r.kind === 'webhook' && (
                  <Tooltip title={r.has_token ? `当前令牌 ${r.token_hint}` : '还没有令牌：外部调用一律 401'}>
                    <Button size="small" onClick={() => rotate(r)}>
                      {r.has_token ? '🔑 重置令牌' : '🔑 生成令牌'}
                    </Button>
                  </Tooltip>
                )}
              </Space>) },
          ]} />
      )}
      <Modal open={!!token} onCancel={() => setToken(null)} footer={null} title="webhook 令牌（只显示这一次）">
        {token && (
          <div style={{ lineHeight: 2 }}>
            <div>调用地址：</div>
            <Typography.Paragraph copyable className="mono">{token.url}</Typography.Paragraph>
            <div>请求头 <code>X-AutoMind-Hook-Token</code>（或 <code>Authorization: Bearer …</code>）：</div>
            <Typography.Paragraph copyable className="mono">{token.token}</Typography.Paragraph>
            <div className="hint-text">
              请求体就是入参（JSON 对象，也可以包一层 {'{"inputs": {...}}'}）；令牌错误或缺失返回 401。
              这个地址不需要平台的访问令牌。
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
