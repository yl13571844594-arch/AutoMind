// 🤖 机器人（v1.11.6）：飞书 / 钉钉 / 企业微信 / 微信公众号 / 通用 Webhook。
// 在 IM 里给机器人发消息 → Agent 执行任务（工具调用照常走审批）→ 过程与结果回传到同一个聊天窗口。
import {
  Alert, App, Button, Empty, Input, InputNumber, Modal, Radio, Space, Switch, Tabs, Tag, Tooltip,
} from 'antd';
import Select from '../ui/SafeSelect';
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPatch, apiPost, errText } from '../../api/client';
import { copyText } from '../../lib/clipboard';
import { useUi } from '../../store/ui';

interface Field { key: string; label: string; secret: boolean; required: boolean; help: string }
interface ChannelSpec {
  key: string; label: string; icon: string; fields: Field[]; guide: string[];
  stream_guide: string[]; transports: string[]; stream_required: string[];
}
interface ConnStatus { state: string; error: string; since: number; reconnects: number; last_msg: number; retry_in?: number }
interface Bot {
  id: string; name: string; channel: string; enabled: boolean; mode: string; progress: string;
  progress_interval: number; approval_timeout: number; allow_users: string[];
  credentials: Record<string, string>; hook_token: string; public_base_url: string;
  missing?: string[]; running?: number; transport?: string; conn?: ConnStatus;
}
interface LogRow { seq: number; at: string; dir: 'in' | 'out' | 'err'; chat: string; user: string; title: string; text: string }

const MODES = [
  { value: 'work', label: '工作（分层计划 + 工具执行）' },
  { value: 'coding', label: '编程' },
  { value: 'chat', label: '对话（只回答，不调工具）' },
  { value: 'multi', label: '多智能体（专业版）' },
  { value: 'loop', label: '循环工程（专业版）' },
];
const CONN_LABEL: Record<string, string> = {
  connected: '已连接', connecting: '连接中', retrying: '断开，自动重连中', config: '凭据未填写完整',
  stopped: '已停止', idle: '未连接',
};
const PROGRESS = [
  { value: 'brief', label: '简要：计划、失败、审批即时发，工具调用按间隔合并' },
  { value: 'detailed', label: '详细：每一步、每次工具调用都发' },
  { value: 'off', label: '只发结果' },
];

export default function BotsModal() {
  const { message, modal } = App.useApp();
  const open = useUi((s) => s.modal) === 'bots';
  const close = useUi((s) => s.closeModal);
  const [channels, setChannels] = useState<ChannelSpec[]>([]);
  const [bots, setBots] = useState<Bot[]>([]);
  const [baseUrl, setBaseUrl] = useState('');
  const [sel, setSel] = useState('');
  const [draft, setDraft] = useState<Bot | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState('');

  const load = useCallback(async (pick?: string) => {
    try {
      const [c, b] = await Promise.all([apiGet('/bots/channels'), apiGet('/bots')]);
      setChannels(c.channels || []);
      setBots(b.bots || []);
      setBaseUrl(b.base_url || location.origin);
      setErr('');
      const id = pick ?? sel;
      const hit = (b.bots || []).find((x: Bot) => x.id === id) || (b.bots || [])[0];
      setSel(hit?.id || '');
      setDraft(hit ? JSON.parse(JSON.stringify(hit)) : null);
    } catch (e) { setErr(errText(e)); }
  }, [sel]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (open) load(); }, [open]);
  // 长连接状态每 3 秒刷新一次（只更新状态，不覆盖正在编辑的表单）
  useEffect(() => {
    if (!open) return;
    const t = setInterval(async () => {
      try {
        const b = await apiGet('/bots');
        const list: Bot[] = b.bots || [];
        setBots((old) => list.map((x) => {
          const prev = old.find((o) => o.id === x.id);
          return prev ? { ...prev, conn: x.conn, running: x.running } : x;
        }));
        setDraft((d) => {
          const hit = d && list.find((x) => x.id === d.id);
          return d && hit ? { ...d, conn: hit.conn } : d;
        });
      } catch { /* 下一轮再试 */ }
    }, 3000);
    return () => clearInterval(t);
  }, [open]);

  const spec = channels.find((c) => c.key === draft?.channel);
  const base = (draft?.public_base_url || baseUrl).replace(/\/$/, '');
  const hookUrl = draft ? `${base}/api/bots/hook/${draft.id}/${draft.hook_token}` : '';
  const localBase = /\/\/(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/.test(base);

  const picker = useRef<{ destroy: () => void } | null>(null);
  const add = (ch: ChannelSpec) => {
    picker.current?.destroy();
    apiPost('/bots', { channel: ch.key, name: `${ch.label}机器人` })
      .then((r) => { message.success(`已添加${ch.label}机器人，填写凭据后保存`); load(r.bot.id); })
      .catch((e) => message.error(errText(e)));
  };
  const pickChannel = () => { picker.current = modal.info({
    title: '添加机器人：选择渠道', icon: null, okText: '取消', width: 520,
    content: (
      <div className="bot-ch-grid">
        {channels.map((c) => (
          <button key={c.key} className="bot-ch" onClick={() => add(c)}>
            <span className="bot-ch-ico">{c.icon}</span><b>{c.label}</b>
          </button>
        ))}
      </div>
    ),
  }); };

  const save = async (patch?: Partial<Bot> & { rotate_token?: boolean }) => {
    if (!draft) return;
    setSaving(true);
    try {
      const body = { ...draft, ...(patch || {}) };
      const r = await apiPatch(`/bots/${draft.id}`, body);
      message.success(patch?.rotate_token ? '已重置回调令牌，请到平台更新回调地址' : '已保存');
      await load(r.bot.id);
    } catch (e) { message.error(errText(e)); } finally { setSaving(false); }
  };
  const remove = () => draft && modal.confirm({
    title: `删除机器人「${draft.name}」？`, content: '平台上配置的回调地址将失效，进行中的任务会被停止。',
    okText: '删除', okButtonProps: { danger: true },
    onOk: async () => {
      try { await apiDelete(`/bots/${draft.id}`); message.success('已删除'); setSel(''); await load(''); }
      catch (e) { message.error(errText(e)); }
    },
  });
  const set = (p: Partial<Bot>) => draft && setDraft({ ...draft, ...p });
  const setCred = (k: string, v: string) => draft && setDraft({ ...draft, credentials: { ...draft.credentials, [k]: v } });
  const strip = (b?: Bot | null) => (b ? JSON.stringify({ ...b, conn: undefined, running: undefined }) : '');
  const dirty = !!draft && strip(draft) !== strip(bots.find((b) => b.id === draft.id));
  const transport = draft?.transport || spec?.transports?.[0] || 'webhook';
  const reconnect = async () => {
    if (!draft) return;
    try { await apiPost(`/bots/${draft.id}/reconnect`, {}); message.info('正在重新连接…'); }
    catch (e) { message.error(errText(e)); }
  };

  return (
    <Modal title="🤖 机器人" open={open} onCancel={close} width={900} footer={null} destroyOnClose>
      <div className="hint-text" style={{ marginBottom: 10 }}>
        在飞书、钉钉、企业微信、微信里给机器人发消息即可下达任务：Agent 照常调用工具执行（危险操作会在聊天里请你批准），
        执行过程与结果回传到同一个聊天窗口。聊天里可用 <code>/stop</code> <code>/status</code> <code>/new</code> <code>/mode</code> <code>/apply</code>。
      </div>
      {err && <Alert type="error" showIcon message="读取失败" description={err} style={{ marginBottom: 10 }} />}
      <div className="bots-wrap">
        <div className="bots-list">
          {bots.map((b) => {
            const ch = channels.find((c) => c.key === b.channel);
            return (
              <div key={b.id} className={'bots-item' + (b.id === sel ? ' active' : '')}
                onClick={() => { setSel(b.id); setDraft(JSON.parse(JSON.stringify(b))); }}>
                <span className="bots-ico">{ch?.icon || '🤖'}</span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="bots-name">{b.name}</div>
                  <div className="hint-text">{ch?.label || b.channel}
                    {!b.enabled && ' · 已停用'}{b.missing?.length ? ' · 待配置' : ''}
                    {b.enabled && b.transport === 'stream' && !b.missing?.length
                      ? ` · ${CONN_LABEL[b.conn?.state || 'idle'] || b.conn?.state}` : ''}
                    {b.running ? ` · ${b.running} 个任务` : ''}</div>
                </div>
                <span className={'sum-dot' + (b.enabled && !b.missing?.length
                  && (b.transport !== 'stream' || b.conn?.state === 'connected') ? ' on' : '')} />
              </div>
            );
          })}
          <Button block type="dashed" onClick={pickChannel} style={{ marginTop: 6 }}>＋ 添加机器人</Button>
        </div>
        <div className="bots-main">
          {!draft ? (
            <Empty description="还没有机器人。点左侧「添加机器人」选择飞书 / 钉钉 / 企业微信 / 微信 / 通用 Webhook" />
          ) : (
            <Tabs size="small" items={[
              {
                key: 'cfg', label: '⚙ 配置',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }} size={10}>
                    <Space wrap>
                      <Input style={{ width: 220 }} value={draft.name} onChange={(e) => set({ name: e.target.value })} addonBefore="名称" />
                      <Tag>{spec?.icon} {spec?.label}</Tag>
                      <span>启用 <Switch size="small" checked={draft.enabled} onChange={(v) => set({ enabled: v })} /></span>
                    </Space>
                    {!!draft.missing?.length && <Alert type="warning" showIcon message={`还需填写：${draft.missing.join('、')}`} />}

                    {(spec?.transports || []).length > 1 && (
                      <div className="bots-box">
                        <b>📡 接收方式</b>
                        <Radio.Group style={{ display: 'block', marginTop: 6 }} value={transport}
                          onChange={(e) => set({ transport: e.target.value })}>
                          <Radio value="stream">长连接（推荐）：本机主动连平台，<b>不需要公网 IP / 域名 / 内网穿透</b></Radio>
                          <Radio value="webhook">HTTP 回调：平台访问本机，需要公网地址</Radio>
                        </Radio.Group>
                      </div>
                    )}

                    {transport === 'stream' ? (
                      <div className="bots-box">
                        <b>🔌 连接状态</b>
                        <div className="bot-conn">
                          <span className={'bot-conn-dot ' + (draft.conn?.state || 'idle')} />
                          <b>{CONN_LABEL[draft.conn?.state || 'idle'] || draft.conn?.state}</b>
                          {draft.conn?.state === 'connected' && draft.conn.since > 0 && (
                            <span className="hint-text">自 {new Date(draft.conn.since * 1000).toLocaleTimeString()}
                              {draft.conn.last_msg ? ` · 最近消息 ${new Date(draft.conn.last_msg * 1000).toLocaleTimeString()}` : ''}</span>
                          )}
                          {draft.conn?.state === 'retrying' && (
                            <span className="hint-text">{draft.conn.retry_in}s 后重试（第 {draft.conn.reconnects} 次）</span>
                          )}
                          <Button size="small" style={{ marginLeft: 'auto' }} onClick={reconnect} disabled={dirty}>↻ 立即重连</Button>
                        </div>
                        {draft.conn?.error && <div className="hint-text" style={{ color: 'var(--red)', marginTop: 4 }}>{draft.conn.error}</div>}
                        <div className="hint-text" style={{ marginTop: 4 }}>
                          本机只要能上网就能收消息：NAT、防火墙、没有公网 IP 的云电脑都不影响；断线自动重连。
                          {dirty && ' 修改后点「保存」即按新配置重连。'}
                        </div>
                      </div>
                    ) : (
                    <div className="bots-box">
                      <b>📮 回调地址</b><span className="hint-text">（填到平台的「事件订阅 / 消息接收地址」）</span>
                      <Space.Compact style={{ width: '100%', marginTop: 6 }}>
                        <Input readOnly className="mono" value={hookUrl} />
                        <Button onClick={() => copyText(hookUrl).then(() => message.success('已复制'))}>⧉ 复制</Button>
                        <Tooltip title="令牌泄露时重置；重置后需到平台更新回调地址">
                          <Button onClick={() => save({ rotate_token: true })}>↻</Button>
                        </Tooltip>
                      </Space.Compact>
                      <Input style={{ marginTop: 6 }} placeholder="公网地址（如 https://xxx.cpolar.cn），平台必须能从公网访问到本机"
                        value={draft.public_base_url} onChange={(e) => set({ public_base_url: e.target.value })} addonBefore="公网地址" />
                      {localBase && (
                        <div className="hint-text" style={{ color: 'var(--yellow)', marginTop: 4 }}>
                          ⚠ 当前是本机地址，IM 平台访问不到。请用 frp / ngrok / cpolar 等把本机 {location.port || '8765'} 端口映射到公网，并把公网地址填在上面。
                        </div>
                      )}
                    </div>
                    )}

                    <div className="bots-box">
                      <b>🔑 平台凭据</b>
                      <div className="bots-fields">
                        {(spec?.fields || []).map((f) => (
                          <label key={f.key}>
                            <span className="bots-cap">{f.label}{(f.required || (transport === 'stream'
                              && spec?.stream_required?.includes(f.key))) && <i style={{ color: 'var(--red)' }}> *</i>}</span>
                            {f.secret
                              ? <Input.Password value={draft.credentials[f.key] || ''} placeholder={f.help}
                                  onChange={(e) => setCred(f.key, e.target.value)} />
                              : <Input value={draft.credentials[f.key] || ''} placeholder={f.help}
                                  onChange={(e) => setCred(f.key, e.target.value)} />}
                          </label>
                        ))}
                      </div>
                    </div>

                    <div className="bots-box">
                      <b>🧭 执行方式</b>
                      <div className="bots-fields">
                        <label><span className="bots-cap">默认模式</span>
                          <Select value={draft.mode} options={MODES} onChange={(v) => set({ mode: v })} /></label>
                        <label><span className="bots-cap">过程回传</span>
                          <Select value={draft.progress} options={PROGRESS} onChange={(v) => set({ progress: v })} /></label>
                        <label><span className="bots-cap">合并间隔（秒）</span>
                          <InputNumber min={3} max={120} value={draft.progress_interval} style={{ width: '100%' }}
                            onChange={(v) => set({ progress_interval: Number(v) || 10 })} /></label>
                        <label><span className="bots-cap">审批等待（秒，超时按拒绝）</span>
                          <InputNumber min={30} max={1800} value={draft.approval_timeout} style={{ width: '100%' }}
                            onChange={(v) => set({ approval_timeout: Number(v) || 300 })} /></label>
                      </div>
                      <label className="bots-full"><span className="bots-cap">授权名单（留空 = 能给机器人发消息的人都能下达任务；填用户 ID / 昵称，逗号或换行分隔）</span>
                        <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} value={(draft.allow_users || []).join('\n')}
                          onChange={(e) => set({ allow_users: e.target.value.split(/[\n,，]/) as any })} />
                      </label>
                    </div>

                    {!!spec && (transport === 'stream' ? spec.stream_guide : spec.guide).length > 0 && (
                      <div className="bots-box">
                        <b>📘 在{spec.label}平台上这样配置{transport === 'stream' ? '（长连接）' : ''}</b>
                        <ol className="bots-guide">
                          {(transport === 'stream' ? spec.stream_guide : spec.guide).map((g) => <li key={g}>{g}</li>)}
                        </ol>
                      </div>
                    )}

                    <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                      <Button danger onClick={remove}>删除机器人</Button>
                      <Button type="primary" loading={saving} disabled={!dirty} onClick={() => save()}>保存</Button>
                    </Space>
                  </Space>
                ),
              },
              { key: 'test', label: '💬 测试对话', children: <TestChat bot={draft} /> },
            ]} />
          )}
        </div>
      </div>
    </Modal>
  );
}

/** 测试对话：模拟一条来自聊天的消息，看机器人会回什么（真实执行，但回复不发到平台）。 */
function TestChat({ bot }: { bot: Bot }) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<LogRow[]>([]);
  const [text, setText] = useState('');
  const seq = useRef(0);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    seq.current = 0;
    setRows([]);
    (async () => {
      while (alive) {
        try {
          const r = await apiGet(`/bots/${bot.id}/log?after=${seq.current}&wait=25`);
          const add: LogRow[] = r.log || [];
          if (!alive) return;
          if (add.length) {
            seq.current = add[add.length - 1].seq;
            setRows((x) => [...x, ...add].slice(-200));
            requestAnimationFrame(() => box.current?.scrollTo({ top: 1e9 }));
          }
        } catch {
          await new Promise((res) => setTimeout(res, 3000));
        }
      }
    })();
    return () => { alive = false; };
  }, [bot.id]);

  const send = async () => {
    const t = text.trim();
    if (!t) return;
    setText('');
    try { await apiPost(`/bots/${bot.id}/test`, { text: t }); }
    catch (e) { message.error(errText(e)); }
  };

  return (
    <div>
      <div className="hint-text" style={{ marginBottom: 6 }}>
        模拟「有人在聊天里给机器人发了这条消息」：会真实执行任务，回复只显示在这里、不发到平台。
        这里也会显示来自平台的真实收发记录（用于排查回调是否打通）。
      </div>
      <div className="bot-chat" ref={box}>
        {!rows.length && <div className="hint-text" style={{ padding: 12 }}>暂无消息。试试发送「/help」或一个任务。</div>}
        {rows.map((r) => (
          <div key={r.seq} className={'bot-msg ' + r.dir}>
            <div className="bot-msg-meta">{r.at} · {r.dir === 'in' ? (r.user || '用户') : r.dir === 'err' ? '错误' : '机器人'}
              {r.chat && r.chat !== 'console' && <span className="mono"> · {r.chat.slice(0, 16)}</span>}</div>
            <div className="bot-msg-body">{r.title && <b>{r.title}<br /></b>}{r.text}</div>
          </div>
        ))}
      </div>
      <Space.Compact style={{ width: '100%', marginTop: 8 }}>
        <Input value={text} onChange={(e) => setText(e.target.value)} onPressEnter={send}
          placeholder="例如：统计 logs 目录下的错误数量并写成 report.md；或回复「同意」批准操作" />
        <Button type="primary" onClick={send}>发送</Button>
      </Space.Compact>
    </div>
  );
}
