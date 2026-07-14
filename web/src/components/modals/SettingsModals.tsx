// 设置弹窗四件套：🖥 模型配置 / 🔑 API Keys / ⚙ 通用设置 / 🔌 Agent 集成。
import {
  App, Button, Checkbox, Input, InputNumber, Modal, Select, Slider, Space, Tag, Typography,
} from 'antd';
import { useEffect, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import { MODE_LABELS, useApp } from '../../store/app';
import { useUi } from '../../store/ui';

const { Text, Paragraph } = Typography;

// ── 🖥 模型配置 ─────────────────────────────────────────
function ModelModal() {
  const { message } = App.useApp();
  const modal = useUi((s) => s.modal);
  const close = useUi((s) => s.closeModal);
  const open = modal === 'model';
  const [providers, setProviders] = useState<any>(null);
  const [keys, setKeys] = useState<any>({});
  const [models, setModels] = useState<string[]>([]);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [apiBase, setApiBase] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [interaction, setInteraction] = useState('chat');
  const [modeModels, setModeModels] = useState<any>({ default: {}, modes: {} });
  const [testResult, setTestResult] = useState<any>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (!open) return;
    (async () => {
      const [st, prov, k, mm] = await Promise.all([
        apiGet('/status'), apiGet('/providers'), apiGet('/config/apikeys'), apiGet('/config/mode-models'),
      ]);
      setProviders(prov);
      setKeys(k);
      setProvider(st.provider);
      setModel(st.model || '');
      setApiBase(st.api_base || '');
      setInteraction(st.interaction || 'chat');
      setModeModels(mm);
      setModels(await apiGet(`/models?provider=${st.provider}`));
      setTestResult(null);
    })();
  }, [open]);

  const changeProvider = async (p: string) => {
    setProvider(p);
    const ms = await apiGet(`/models?provider=${p}`);
    setModels(ms);
    const info = keys[p] || {};
    setModel(info.model || ms[0] || '');
    setApiBase(info.api_base || '');
  };

  const labels = providers?.labels || {};
  const groups = [
    ['云端模型', providers?.cloud || []],
    ['本地模型', providers?.local || []],
    ['自定义', providers?.custom || []],
  ] as [string, string[]][];
  const allProviders = groups.flatMap(([, arr]) => arr);
  const customModels = (keys[provider] || {}).custom_models || [];

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    const payload: any = { provider, model: model.trim() };
    if (provider === 'custom') payload.api_base = apiBase.trim();
    if (apiKey.trim()) payload.api_key = apiKey.trim();
    const r = await apiPost('/config/test', payload).catch((e) => ({ success: false, error: String(e) }));
    setTestResult(r);
    setTesting(false);
  };

  const save = async () => {
    const cfg: any = { provider, model: model.trim(), interaction };
    if (provider === 'custom') cfg.api_base = apiBase.trim();
    if (apiKey.trim()) cfg.api_key = apiKey.trim();
    const data = await apiPost('/config', cfg);
    close();
    await useApp.getState().loadStatus();
    if (data.llm_ready) message.success('配置已更新并就绪');
    else if (data.llm_error) message.error('配置已保存但连接失败: ' + String(data.llm_error).slice(0, 100));
    else message.info('已保存，请配置该提供商的 API Key');
  };

  const saveModeModel = async (m: string, useDefault: boolean, prov?: string, mdl?: string) => {
    if (useDefault) {
      await apiPost('/config/mode-models', { mode: m, clear: true });
      message.info(`${MODE_LABELS[m]}模式：跟随默认`);
    } else {
      if (!mdl) { message.error('请输入模型名'); return; }
      const r = await apiPost('/config/mode-models', { mode: m, provider: prov, model: mdl });
      if (r.error) { message.error(r.error); return; }
      message.success(`${MODE_LABELS[m]}模式 → ${prov}/${mdl}`);
    }
    setModeModels(await apiGet('/config/mode-models'));
    useApp.getState().loadStatus(useApp.getState().mode);
  };

  return (
    <Modal title="🖥 模型配置" open={open} onCancel={close} width={640} footer={null}>
      <Paragraph type="secondary" style={{ fontSize: '.86em' }}>选择提供商与模型，配置即时生效（自动重建连接）。</Paragraph>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <div>
          <Text strong>LLM 提供商</Text>
          <Select
            style={{ width: '100%', marginTop: 4 }} value={provider} onChange={changeProvider}
            options={groups.filter(([, arr]) => arr.length).map(([g, arr]) => ({
              label: g, options: arr.map((p) => ({ value: p, label: labels[p] || p })),
            }))}
          />
        </div>
        <div>
          <Text strong>模型名称</Text>
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Select
              style={{ flex: 1 }} value={model} onChange={setModel} showSearch
              options={models.map((m) => ({ value: m, label: m }))}
              popupMatchSelectWidth={false}
              filterOption={(inp, opt) => (opt?.value || '').toString().toLowerCase().includes(inp.toLowerCase())}
              onSearch={(v) => v && setModel(v)}
            />
            <Button onClick={async () => {
              if (!model.trim()) { message.error('请输入模型名'); return; }
              const r = await apiPost('/models/add', { provider, model: model.trim() });
              setModels(r.models || []);
              setKeys(await apiGet('/config/apikeys'));
              message.success(`已添加模型 ${model}`);
            }}>➕ 添加</Button>
          </Space.Compact>
          {customModels.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <Text type="secondary" style={{ fontSize: '.76em' }}>我的模型：</Text>
              {customModels.map((m: string) => (
                <Tag key={m} closable style={{ cursor: 'pointer', marginTop: 4 }}
                  onClick={() => setModel(m)}
                  onClose={async (e) => {
                    e.preventDefault();
                    await apiPost('/models/remove', { provider, model: m });
                    setKeys(await apiGet('/config/apikeys'));
                    setModels(await apiGet(`/models?provider=${provider}`));
                    message.info(`已移除 ${m}`);
                  }}>{m}</Tag>
              ))}
            </div>
          )}
        </div>

        {provider === 'custom' && (
          <>
            <div>
              <Text strong>API 地址 / 中转代理 (api_base)</Text>
              <Input style={{ marginTop: 4 }} value={apiBase} onChange={(e) => setApiBase(e.target.value)}
                placeholder="https://your-proxy.com/v1" />
            </div>
            <div>
              <Text strong>API Key（可选，仅用于测试/保存）</Text>
              <Input.Password style={{ marginTop: 4 }} value={apiKey} onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-... 留空则用已保存的 Key" />
            </div>
          </>
        )}

        <div>
          <Text strong>默认交互模式</Text>
          <Select style={{ width: '100%', marginTop: 4 }} value={interaction} onChange={setInteraction}
            options={[
              { value: 'chat', label: '💬 对话模式' }, { value: 'work', label: '⚙️ 工作模式' },
              { value: 'coding', label: '💻 编程模式' }, { value: 'multi', label: '🤝 协同模式' },
              { value: 'loop', label: '🔁 循环模式' },
            ]} />
        </div>

        <div style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
          <Text strong>🎛 各模式独立模型</Text>
          <Paragraph type="secondary" style={{ fontSize: '.78em', margin: '4px 0 8px' }}>
            为不同任务模式指定不同模型；勾选「默认」则跟随上面的全局模型。需先在「API Keys」配置对应提供商的 Key。
          </Paragraph>
          {(['chat', 'work', 'coding', 'multi', 'loop'] as const).map((m) => {
            const mm = modeModels.modes?.[m];
            return <ModeModelRow key={m} mode={m} mm={mm} def={modeModels.default || {}}
              providers={allProviders} labels={labels} onSave={saveModeModel} />;
          })}
        </div>

        {testResult && (
          <div style={{
            border: `1px solid ${testResult.success ? 'var(--green)' : 'var(--red)'}`,
            borderRadius: 10, padding: 10, fontSize: '.84em',
          }}>
            {testResult.success
              ? <>✅ <b>连接成功</b> · {testResult.latency_ms}ms<br />
                <span className="hint-text">{testResult.provider}/{testResult.model} · 回复: {testResult.reply_sample}</span></>
              : <>❌ <b>连接失败</b>（{testResult.stage || ''}）<br />
                <span style={{ color: 'var(--yellow)' }}>{testResult.hint || ''}</span><br />
                <span className="hint-text mono">{String(testResult.error || '').slice(0, 260)}</span></>}
          </div>
        )}
        <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
          <Button onClick={close}>取消</Button>
          <Button loading={testing} onClick={test}>🔌 测试连接</Button>
          <Button type="primary" onClick={save}>保存并应用</Button>
        </Space>
      </Space>
    </Modal>
  );
}

function ModeModelRow({ mode, mm, def, providers, labels, onSave }: any) {
  const [useDefault, setUseDefault] = useState(!mm);
  const [prov, setProv] = useState(mm?.provider || def.provider || '');
  const [mdl, setMdl] = useState(mm?.model || '');
  useEffect(() => { setUseDefault(!mm); setProv(mm?.provider || def.provider || ''); setMdl(mm?.model || ''); }, [mm, def]);
  const icons: any = { chat: '💬', work: '⚙️', coding: '💻', multi: '🤝', loop: '🔁' };
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
      <span style={{ width: 64, fontSize: '.82em' }}>{icons[mode]} {MODE_LABELS[mode]}</span>
      <Select size="small" disabled={useDefault} value={prov} onChange={setProv} style={{ width: 110 }}
        options={providers.map((p: string) => ({ value: p, label: labels[p] || p }))} />
      <Input size="small" disabled={useDefault} value={useDefault ? '' : mdl} placeholder={def.model || '模型名'}
        onChange={(e) => setMdl(e.target.value)} style={{ flex: 1 }} />
      <Checkbox checked={useDefault} onChange={(e) => {
        setUseDefault(e.target.checked);
        if (e.target.checked) onSave(mode, true);
      }}>默认</Checkbox>
      <Button size="small" onClick={() => onSave(mode, useDefault, prov, mdl.trim())}>保存</Button>
    </div>
  );
}

// ── 🔑 API Keys ─────────────────────────────────────────
function ApiKeysModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'apikeys';
  const close = useUi((s) => s.closeModal);
  const [providers, setProviders] = useState<any>(null);
  const [keys, setKeys] = useState<any>({});
  const [status, setStatus] = useState<any>(null);
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState({ api_base: '', model: '', api_key: '' });

  const reload = async () => {
    const [p, k, st] = await Promise.all([apiGet('/providers'), apiGet('/config/apikeys'), apiGet('/status')]);
    setProviders(p); setKeys(k); setStatus(st);
    const ci = k['custom'] || {};
    setCustom({ api_base: ci.api_base || '', model: ci.model || '', api_key: '' });
  };
  useEffect(() => { if (open) reload(); }, [open]);

  const saveKey = async (p: string, key: string) => {
    if (!key.trim() && key !== '') { message.error('请输入 API Key'); return; }
    await apiPost('/config/apikeys', { provider: p, api_key: key });
    message.success(key ? `${p} API Key 已保存` : `${p} 配置已删除`);
    setInputs((s) => ({ ...s, [p]: '' }));
    await useApp.getState().loadStatus();
    reload();
  };

  const labels = providers?.labels || {};
  const order = [...(providers?.cloud || []), ...(providers?.local || [])];
  const cinfo = keys['custom'] || {};

  return (
    <Modal title="🔑 API Key 管理" open={open} onCancel={close} width={620} footer={<Button onClick={close}>关闭</Button>}>
      <Paragraph type="secondary" style={{ fontSize: '.84em' }}>
        Key 仅保存在本地 <code>.automind_config.json</code>，不会上传。当前使用：
        <b> {status?.provider}/{status?.model}</b> {status?.llm_ready ? '✓ 已就绪' : '⚠ 未就绪'}
      </Paragraph>
      <div style={{ maxHeight: 320, overflowY: 'auto' }}>
        {order.map((p: string) => {
          const info = keys[p] || {};
          const src = info.saved ? '本地' : info.env ? '环境变量' : '';
          return (
            <div key={p} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
              <span style={{ width: 110, fontSize: '.86em' }}>{labels[p] || p}</span>
              <Tag color={info.has_key ? 'green' : undefined} style={{ width: 92, textAlign: 'center' }}>
                {info.has_key ? `已配置${src ? '·' + src : ''}` : '未配置'}
              </Tag>
              <Input.Password size="small" style={{ flex: 1 }}
                placeholder={info.has_key ? '●●●●●● (已设置，留空不改)' : '输入 API Key...'}
                value={inputs[p] || ''} onChange={(e) => setInputs((s) => ({ ...s, [p]: e.target.value }))} />
              <Button size="small" type="primary" onClick={() => saveKey(p, (inputs[p] || '').trim())}>保存</Button>
              {info.saved && <Button size="small" danger onClick={() => saveKey(p, '')}>删除</Button>}
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 14, padding: 12, border: '1px solid var(--border)', borderRadius: 10 }}>
        <Text strong style={{ fontSize: '.9em' }}>🔌 自定义 OpenAI 标准接口 / 中转代理</Text>
        <Paragraph type="secondary" style={{ fontSize: '.76em', margin: '4px 0 8px' }}>
          适用于任何兼容 OpenAI <code>/v1/chat/completions</code> 的服务或中转站。
        </Paragraph>
        <Space direction="vertical" style={{ width: '100%' }} size={6}>
          <Input size="small" addonBefore="API 地址" value={custom.api_base}
            onChange={(e) => setCustom((s) => ({ ...s, api_base: e.target.value }))} placeholder="https://api.your-proxy.com/v1" />
          <Input size="small" addonBefore="默认模型" value={custom.model}
            onChange={(e) => setCustom((s) => ({ ...s, model: e.target.value }))} placeholder="gpt-4o" />
          <Input.Password size="small" addonBefore="API Key" value={custom.api_key}
            onChange={(e) => setCustom((s) => ({ ...s, api_key: e.target.value }))}
            placeholder={cinfo.has_key ? '●●●●●● (已设置，留空不改)' : 'sk-...'} />
          <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
            {cinfo.saved && <Button size="small" danger onClick={() => saveKey('custom', '')}>删除</Button>}
            <Button size="small" type="primary" onClick={async () => {
              if (!custom.api_base.trim()) { message.error('请填写 API 地址 (api_base)'); return; }
              await apiPost('/config/provider', { provider: 'custom', api_base: custom.api_base.trim(), model: custom.model.trim() });
              if (custom.api_key.trim()) {
                await apiPost('/config/apikeys', {
                  provider: 'custom', api_key: custom.api_key.trim(),
                  api_base: custom.api_base.trim(), model: custom.model.trim(),
                });
              }
              message.success('自定义接口已保存');
              await useApp.getState().loadStatus();
              reload();
            }}>保存自定义接口</Button>
          </Space>
        </Space>
      </div>
    </Modal>
  );
}

// ── ⚙ 通用设置 ──────────────────────────────────────────
const AUTOPILOT_LABELS: Record<string, [string, string]> = {
  auto_review: ['🧐 多Agent审查', '工作模式完成后由审阅者角色复核'],
  auto_verify: ['✅ Loop 验收', '语义判定是否真正完成，未过带反馈自动修复'],
  auto_test: ['🧪 TDD 测试', '编程模式：改 .py 自动语法验证；收尾自动跑 pytest'],
  parallel_execution: ['⚡ 并行执行', '计划中互不依赖的步骤并发执行'],
  subtask_cache: ['📦 子任务缓存', '同一任务内相同的只读工具调用结果复用'],
};

function GeneralModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'general';
  const close = useUi((s) => s.closeModal);
  const [cfg, setCfg] = useState<any>(null);
  const [autopilot, setAutopilot] = useState<any>({});
  const [dirs, setDirs] = useState<any>(null);
  const [showPicker, setShowPicker] = useState(false);

  useEffect(() => {
    if (!open) return;
    apiGet('/config/full').then(setCfg);
    apiGet('/config/autopilot').then(setAutopilot);
    setShowPicker(false);
  }, [open]);

  const browse = async (path: string) => {
    const r = await apiGet(`/fs/list?path=${encodeURIComponent(path)}`);
    if (r.error) { message.error(r.error); return; }
    setDirs(r);
  };

  const saveProject = async (path: string) => {
    const r = await apiPost('/config/project', { path });
    if (r.error) { message.error(r.error); return; }
    message.success('项目目录已设置: ' + r.project);
    useApp.getState().loadStatus();
  };

  if (!cfg) return <Modal open={open} onCancel={close} footer={null} title="⚙ 通用设置" />;

  return (
    <Modal title="⚙ 通用设置" open={open} onCancel={close} width={600} footer={null}>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <div>
          <Text strong>项目目录（Agent 文件操作的根目录）</Text>
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Input value={cfg.project} onChange={(e) => setCfg({ ...cfg, project: e.target.value })} />
            <Button onClick={() => { setShowPicker(!showPicker); if (!showPicker) browse(cfg.project || ''); }}>📁 浏览</Button>
            <Button type="primary" onClick={() => saveProject(cfg.project.trim())}>应用</Button>
          </Space.Compact>
          {showPicker && dirs && (
            <div style={{ border: '1px solid var(--border)', borderRadius: 8, marginTop: 8, overflow: 'hidden' }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '6px 10px', borderBottom: '1px solid var(--border)', background: 'var(--bg2)' }}>
                <Button size="small" onClick={() => browse(dirs.path.replace(/[\\/][^\\/]*$/, '') || dirs.path)}>⬆ 上级</Button>
                <span className="mono hint-text" style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{dirs.path}</span>
                <Button size="small" type="primary" onClick={() => {
                  setCfg({ ...cfg, project: dirs.path }); setShowPicker(false); saveProject(dirs.path);
                }}>✓ 选此目录</Button>
              </div>
              {(dirs.drives || []).length > 0 && (
                <div style={{ padding: '4px 10px', borderBottom: '1px solid var(--border)' }}>
                  {dirs.drives.map((d: string) => <Tag key={d} style={{ cursor: 'pointer' }} onClick={() => browse(d)}>{d}</Tag>)}
                </div>
              )}
              <div style={{ maxHeight: 180, overflowY: 'auto', padding: 4 }}>
                {(dirs.dirs || []).map((d: string) => (
                  <div key={d} className="ft-item" onClick={() => browse((dirs.path + '/' + d).replace(/\\/g, '/'))}>📁 {d}</div>
                ))}
                {!(dirs.dirs || []).length && <div className="hint-text" style={{ padding: 8 }}>（无子目录）</div>}
              </div>
            </div>
          )}
        </div>
        <div>
          <Text strong>Temperature：{cfg.temperature}</Text>
          <Slider min={0} max={2} step={0.1} value={cfg.temperature}
            onChange={(v) => setCfg({ ...cfg, temperature: v })} />
        </div>
        <div>
          <Text strong>最大输出 Token</Text>
          <InputNumber style={{ width: 160, marginLeft: 12 }} min={256} max={32768} value={cfg.max_tokens}
            onChange={(v) => setCfg({ ...cfg, max_tokens: v })} />
        </div>
        <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
          <Button onClick={close}>取消</Button>
          <Button type="primary" onClick={async () => {
            await apiPost('/config', { temperature: cfg.temperature, max_tokens: cfg.max_tokens });
            close(); message.success('设置已保存');
          }}>保存采样参数</Button>
        </Space>
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <Text strong>🔄 自主任务闭环（工作 / 编程模式）</Text>
          <Paragraph type="secondary" style={{ fontSize: '.76em', margin: '4px 0 8px' }}>
            任务完成后自动 多Agent审查 → Loop 语义验收 → 未达标带反馈自动修复。默认全开，可单独关闭。
          </Paragraph>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 14px' }}>
            {Object.entries(AUTOPILOT_LABELS).map(([k, [label, tip]]) => (
              <Checkbox key={k} checked={!!autopilot[k]} title={tip} onChange={async (e) => {
                await apiPost('/config/autopilot', { [k]: e.target.checked });
                setAutopilot({ ...autopilot, [k]: e.target.checked });
                message.info(`${label} 已${e.target.checked ? '开启' : '关闭'}`);
              }}>{label}</Checkbox>
            ))}
          </div>
        </div>
      </Space>
    </Modal>
  );
}

// ── 🔌 Agent 集成 ───────────────────────────────────────
function IntegrationsModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'integrations';
  const close = useUi((s) => s.closeModal);
  const [cfg, setCfg] = useState<any>(null);

  useEffect(() => {
    if (open) apiGet('/integrations/continue').then(setCfg).catch(() => setCfg({
      base_url: location.origin + '/v1', model: '—', yaml: '', auth_required: false,
    }));
  }, [open]);

  const copy = (t: string) => navigator.clipboard?.writeText(t).then(() => message.success('已复制'));
  const curl = cfg ? `curl ${cfg.base_url}/chat/completions \\
  -H "Content-Type: application/json"${cfg.auth_required ? ' \\\n  -H "Authorization: Bearer <你的令牌>"' : ''} \\
  -d '{"model":"${cfg.model}","messages":[{"role":"user","content":"你好"}]}'` : '';

  return (
    <Modal title="🔌 Agent 集成" open={open} onCancel={close} width={640} footer={<Button onClick={close}>关闭</Button>}>
      <Paragraph type="secondary" style={{ fontSize: '.84em' }}>
        把 AutoMind 作为「模型提供方」接入 IDE 里的 AI Agent（Continue / Cline 等）或任何 OpenAI 兼容客户端 —— 复用你配好的模型、中转代理与企业网关，Key 只留在本机。
      </Paragraph>
      {cfg && (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <div style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
            <b>🧩 Continue.dev — VS Code / JetBrains 侧边面板</b>
            <Paragraph type="secondary" style={{ fontSize: '.78em', margin: '4px 0' }}>
              安装 Continue 扩展 → 侧边面板 ⚙ → Open Config，把下面的配置粘贴进 models: 段。
            </Paragraph>
            <div style={{ position: 'relative' }}>
              <pre className="mono" style={{ background: 'var(--bg0)', border: '1px solid var(--border)', borderRadius: 8, padding: 10, fontSize: '.76em', overflowX: 'auto' }}>{cfg.yaml || '加载失败'}</pre>
              <Button size="small" type="primary" style={{ position: 'absolute', top: 6, right: 6 }} onClick={() => copy(cfg.yaml)}>⧉ 复制</Button>
            </div>
          </div>
          <div style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12, fontSize: '.86em', lineHeight: 2 }}>
            <b>🤖 Cline — VS Code 自主编码 Agent</b><br />
            API Provider：<code>OpenAI Compatible</code><br />
            Base URL：<code>{cfg.base_url}</code> <Button size="small" onClick={() => copy(cfg.base_url)}>⧉</Button><br />
            API Key：{cfg.auth_required ? '你的 AUTOMIND_AUTH_TOKEN 令牌' : '任意填（未开鉴权）'}　Model ID：<code>{cfg.model}</code> <Button size="small" onClick={() => copy(cfg.model)}>⧉</Button>
          </div>
          <div style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
            <b>🌐 通用 OpenAI 兼容 API</b>
            <div style={{ fontSize: '.84em', margin: '6px 0' }}>
              端点：<code>POST /v1/chat/completions</code>（支持 stream SSE）· <code>GET /v1/models</code>
            </div>
            <div style={{ position: 'relative' }}>
              <pre className="mono" style={{ background: 'var(--bg0)', border: '1px solid var(--border)', borderRadius: 8, padding: 10, fontSize: '.76em', overflowX: 'auto' }}>{curl}</pre>
              <Button size="small" style={{ position: 'absolute', top: 6, right: 6 }} onClick={() => copy(curl)}>⧉ 复制</Button>
            </div>
            {!cfg.auth_required && (
              <div className="hint-text" style={{ marginTop: 6 }}>
                ⚠ 当前未开启访问鉴权（本机使用无碍）；暴露到局域网/公网前请设置 AUTOMIND_AUTH_TOKEN。
              </div>
            )}
          </div>
        </Space>
      )}
    </Modal>
  );
}

export default function SettingsModals() {
  return (<><ModelModal /><ApiKeysModal /><GeneralModal /><IntegrationsModal /></>);
}
