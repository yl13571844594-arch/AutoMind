// ── Model Tab ──
let _apiKeyCache = {};
async function renderModeModels(providers) {
  const data = await (await fetch(`${API}/config/mode-models`)).json();
  const labels = (providers && providers.labels) || {};
  const allP = [...((providers||{}).cloud||[]), ...((providers||{}).local||[]), ...((providers||{}).custom||[])];
  const def = data.default || {};
  const modes = [['chat','💬 对话'],['work','⚙️ 工作'],['coding','💻 编程'],['multi','🤝 协同'],['loop','🔁 循环']];
  return modes.map(([k,lbl])=>{
    const mm = (data.modes||{})[k];
    const useDefault = !mm;
    const prov = mm ? mm.provider : (def.provider||'');
    const model = mm ? mm.model : (def.model||'');
    const popts = allP.map(p=>`<option value="${p}" ${p===prov?'selected':''}>${labels[p]||p}</option>`).join('');
    return `<div class="mm-row" data-mode="${k}">
      <span class="mm-mode">${lbl}</span>
      <select class="mm-prov" ${useDefault?'disabled':''}>${popts}</select>
      <input type="text" class="mm-model" value="${useDefault?'':esc(model)}" placeholder="${esc(def.model||'模型名')}" ${useDefault?'disabled':''}>
      <label class="mm-def" title="跟随默认模型"><input type="checkbox" class="mm-chk" ${useDefault?'checked':''} onchange="toggleModeDefault('${k}', this.checked)"> 默认</label>
      <button class="btn-secondary mm-save" style="padding:5px 10px;font-size:.78em" onclick="saveModeModel('${jsq(k)}')">保存</button>
    </div>`;
  }).join('');
}
function toggleModeDefault(mode, useDefault){
  const row = document.querySelector(`.mm-row[data-mode="${mode}"]`);
  if(!row) return;
  row.querySelector('.mm-prov').disabled = useDefault;
  row.querySelector('.mm-model').disabled = useDefault;
  if(useDefault) saveModeModel(mode, true);
}
async function saveModeModel(mode, clear){
  const row = document.querySelector(`.mm-row[data-mode="${mode}"]`);
  if(clear || row.querySelector('.mm-chk').checked){
    await fetch(`${API}/config/mode-models`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode, clear:true})});
    toast(`${MODE_LABELS[mode]}模式：跟随默认`, 'info');
  } else {
    const provider = row.querySelector('.mm-prov').value;
    const model = row.querySelector('.mm-model').value.trim();
    if(!model) return toast('请输入模型名', 'error');
    const r = await (await fetch(`${API}/config/mode-models`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode, provider, model})})).json();
    if(r.error) return toast(r.error,'error');
    toast(`${MODE_LABELS[mode]}模式 → ${provider}/${model}`, 'success');
  }
  if(mode===currentMode) loadStatus(currentMode);
}
async function renderModelTab() {
  const status = await (await fetch(`${API}/status`)).json();
  const providers = providerData || await (await fetch(`${API}/providers`)).json();
  _apiKeyCache = await (await fetch(`${API}/config/apikeys`)).json();
  const models = await (await fetch(`${API}/models?provider=${status.provider}`)).json();
  const modeModelsHtml = await renderModeModels(providers);
  const groups = [
    ['云端模型', providers.cloud || []],
    ['本地模型', providers.local || []],
    ['自定义', providers.custom || []],
  ];
  const labels = providers.labels || {};
  const opt = groups.filter(([,arr])=>arr.length).map(([g,arr]) =>
    `<optgroup label="${g}">${arr.map(p =>
      `<option value="${p}" ${p===status.provider?'selected':''}>${labels[p]||p}</option>`).join('')}</optgroup>`
  ).join('');
  const modelOpts = models.map(m => `<option value="${m}" ${m===status.model?'selected':''}>${m}</option>`).join('');
  const isCustom = status.provider === 'custom';

  return `
<h2>🖥 模型配置</h2>
<div class="hint">选择提供商与模型，配置即时生效（自动重建连接）。</div>
<label>LLM 提供商</label>
<select id="cfg-provider" onchange="onProviderChange()">${opt}</select>

<label>模型名称</label>
<div style="display:flex;gap:8px">
  <input type="text" id="cfg-model" list="model-list" value="${status.model||''}" placeholder="输入或选择模型名" style="flex:1">
  <button class="btn-secondary" style="padding:0 16px;white-space:nowrap" onclick="addCustomModel()">➕ 添加</button>
</div>
<datalist id="model-list">${modelOpts}</datalist>
<div id="custom-models-chips" style="margin-top:8px"></div>
<div class="hint">可直接输入任意模型名并点击「添加」保存到下拉列表（支持自定义/中转代理提供的模型）。</div>

<div id="custom-base-wrap" style="display:${isCustom?'block':'none'}">
  <label>API 地址 / 中转代理 (api_base)</label>
  <input type="text" id="cfg-apibase" value="${status.api_base||''}" placeholder="https://your-proxy.com/v1">
  <div class="hint">OpenAI 标准接口地址，例如中转站的 <code>https://api.xxx.com/v1</code>。</div>
  <label>API Key（可选，仅用于测试/保存）</label>
  <input type="password" id="cfg-apikey-inline" placeholder="sk-... 留空则用已保存的 Key">
</div>

<label>默认交互模式</label>
<select id="cfg-interaction">
  <option value="chat" ${status.interaction==='chat'?'selected':''}>💬 对话模式</option>
  <option value="work" ${status.interaction==='work'?'selected':''}>⚙️ 工作模式</option>
  <option value="coding" ${status.interaction==='coding'?'selected':''}>💻 编程模式</option>
  <option value="multi" ${status.interaction==='multi'?'selected':''}>🤝 协同模式</option>
  <option value="loop" ${status.interaction==='loop'?'selected':''}>🔁 循环模式</option>
</select>

<div class="mode-models-box">
  <div class="add-box-title" style="margin-bottom:4px">🎛 各模式独立模型</div>
  <div class="hint" style="margin-bottom:8px">为不同任务模式指定不同模型；勾选「默认」则跟随上面的全局模型。需先在「API Keys」配置对应提供商的 Key。</div>
  ${modeModelsHtml}
</div>

<div id="conn-test-result" style="margin-top:12px"></div>
<div class="btn-row">
  <button class="btn-secondary" onclick="closeModal()">取消</button>
  <button class="btn-secondary" id="test-conn-btn" onclick="testConnection()">🔌 测试连接</button>
  <button class="btn-primary" onclick="saveModelSettings()">保存并应用</button>
</div>`;
}

async function testConnection() {
  const btn = document.getElementById('test-conn-btn');
  const out = document.getElementById('conn-test-result');
  const payload = {
    provider: document.getElementById('cfg-provider').value,
    model: document.getElementById('cfg-model').value.trim(),
  };
  const baseEl = document.getElementById('cfg-apibase');
  if (baseEl) payload.api_base = baseEl.value.trim();
  const keyEl = document.getElementById('cfg-apikey-inline');
  if (keyEl && keyEl.value.trim()) payload.api_key = keyEl.value.trim();
  btn.disabled = true; btn.textContent = '测试中...';
  out.innerHTML = '<div style="font-size:.84em;color:var(--text3)">正在发起测试调用...</div>';
  try {
    const r = await (await fetch(`${API}/config/test`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload),
    })).json();
    if (r.success) {
      out.innerHTML = `<div class="card lt-green" style="margin:0">✅ <b>连接成功</b> · ${r.latency_ms}ms
        <div style="font-size:.8em;color:var(--text2);margin-top:4px">${esc(r.provider)}/${esc(r.model)} · 回复: ${esc(r.reply_sample||'')}</div></div>`;
    } else {
      out.innerHTML = `<div class="card lt-red" style="margin:0">❌ <b>连接失败</b>（${esc(r.stage||'')}）
        <div style="font-size:.82em;color:var(--yellow);margin-top:4px">${esc(r.hint||'')}</div>
        <div style="font-size:.76em;color:var(--text3);margin-top:4px;font-family:var(--mono);word-break:break-all">${esc((r.error||'').slice(0,260))}</div></div>`;
    }
  } catch(e) {
    out.innerHTML = `<div class="card lt-red" style="margin:0">❌ 测试请求失败: ${esc(e.message)}</div>`;
  }
  btn.disabled = false; btn.textContent = '🔌 测试连接';
}
function renderModelChips(prov) {
  const el = document.getElementById('custom-models-chips');
  if (!el) return;
  const list = (_apiKeyCache[prov] || {}).custom_models || [];
  if (!list.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<span style="font-size:.74em;color:var(--text3)">我的模型：</span> ' +
    list.map(m => `<span style="display:inline-flex;align-items:center;gap:4px;background:var(--bg3);border:1px solid var(--border);border-radius:14px;padding:3px 6px 3px 10px;font-size:.78em;margin:2px 3px;cursor:pointer"
      onclick="document.getElementById('cfg-model').value='${jsq(m)}'">${esc(m)}
      <span style="color:var(--red);font-weight:700;padding:0 4px" onclick="event.stopPropagation();removeCustomModel('${jsq(m)}')">✕</span></span>`).join('');
}
async function addCustomModel() {
  const prov = document.getElementById('cfg-provider').value;
  const model = document.getElementById('cfg-model').value.trim();
  if (!model) return toast('请输入模型名', 'error');
  const r = await (await fetch(`${API}/models/add`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ provider: prov, model }),
  })).json();
  document.getElementById('model-list').innerHTML = (r.models||[]).map(m=>`<option>${m}</option>`).join('');
  _apiKeyCache = await (await fetch(`${API}/config/apikeys`)).json();
  renderModelChips(prov);
  toast(`已添加模型 ${model}`, 'success');
}
async function removeCustomModel(model) {
  const prov = document.getElementById('cfg-provider').value;
  await fetch(`${API}/models/remove`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ provider: prov, model }),
  });
  _apiKeyCache = await (await fetch(`${API}/config/apikeys`)).json();
  const models = await (await fetch(`${API}/models?provider=${prov}`)).json();
  document.getElementById('model-list').innerHTML = models.map(m=>`<option>${m}</option>`).join('');
  renderModelChips(prov);
  toast(`已移除 ${model}`, 'info');
}
async function onProviderChange() {
  const prov = document.getElementById('cfg-provider').value;
  document.getElementById('custom-base-wrap').style.display = (prov === 'custom') ? 'block' : 'none';
  try {
    const models = await (await fetch(`${API}/models?provider=${prov}`)).json();
    _apiKeyCache = await (await fetch(`${API}/config/apikeys`)).json();
    const info = _apiKeyCache[prov] || {};
    document.getElementById('cfg-model').value = info.model || (models[0]||'');
    document.getElementById('model-list').innerHTML = models.map(m=>`<option>${m}</option>`).join('');
    const baseEl = document.getElementById('cfg-apibase');
    if (baseEl) baseEl.value = info.api_base || '';
    renderModelChips(prov);
  } catch(e) {}
}
async function saveModelSettings() {
  const cfg = {
    provider: document.getElementById('cfg-provider').value,
    model: document.getElementById('cfg-model').value.trim(),
    interaction: document.getElementById('cfg-interaction').value,
  };
  const baseEl = document.getElementById('cfg-apibase');
  if (baseEl) cfg.api_base = baseEl.value.trim();
  const keyEl = document.getElementById('cfg-apikey-inline');
  if (keyEl && keyEl.value.trim()) cfg.api_key = keyEl.value.trim();
  const data = await (await fetch(`${API}/config`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg),
  })).json();
  currentMode = data.interaction; applyModeUI(currentMode);
  closeModal();
  await loadStatus();
  if (data.llm_ready) {
    toast('配置已更新并就绪', 'success');
  } else if (data.llm_error) {
    toast('配置已保存但连接失败: ' + data.llm_error.slice(0, 100), 'error');
  } else if (data.has_api_key) {
    toast('配置已保存但 LLM 未初始化，请检查 API Key 与 api_base', 'warn');
  } else {
    toast('已保存，请配置该提供商的 API Key', 'info');
  }
}

// ── API Key Tab ──
async function renderApiKeyTab() {
  if (!providerData) providerData = await (await fetch(`${API}/providers`)).json();
  const keys = await (await fetch(`${API}/config/apikeys`)).json();
  const status = await (await fetch(`${API}/status`)).json();
  const labels = providerData.labels || {};
  const order = [...(providerData.cloud||[]), ...(providerData.local||[])];

  let rows = '';
  for (const p of order) {
    const info = keys[p] || {};
    const has = info.has_key;
    const src = info.saved ? '本地' : (info.env ? '环境变量' : '');
    rows += `
<div class="api-key-row">
  <span class="provider-name">${labels[p]||p}</span>
  <span class="key-status ${has?'set':'unset'}">${has?('已配置'+(src?'·'+src:'')):'未配置'}</span>
  <input type="password" id="apikey-${p}" placeholder="${has?'●●●●●● (已设置，留空不改)':'输入 API Key...'}">
  <button class="btn-primary" style="padding:7px 12px;font-size:.8em" onclick="saveApiKey('${jsq(p)}')">保存</button>
  ${info.saved ? `<button class="btn-danger" style="padding:7px 12px;font-size:.8em" onclick="deleteApiKey('${jsq(p)}')">删除</button>`:''}
</div>`;
  }

  // 自定义 OpenAI 标准（中转代理）
  const cinfo = keys['custom'] || {};
  const customBlock = `
<div style="margin-top:18px;padding:14px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius-sm)">
  <b style="font-size:.92em">🔌 自定义 OpenAI 标准接口 / 中转代理</b>
  <div class="hint">适用于任何兼容 OpenAI <code>/v1/chat/completions</code> 的服务或中转站。</div>
  <label>API 地址 (api_base)</label>
  <input type="text" id="custom-apibase" value="${cinfo.api_base||''}" placeholder="https://api.your-proxy.com/v1">
  <label>默认模型</label>
  <input type="text" id="custom-model" value="${cinfo.model||''}" placeholder="gpt-4o">
  <label>API Key</label>
  <input type="password" id="custom-apikey" placeholder="${cinfo.has_key?'●●●●●● (已设置，留空不改)':'sk-...'}">
  <div class="btn-row" style="margin-top:14px">
    ${cinfo.saved?`<button class="btn-danger" style="padding:8px 14px;font-size:.85em" onclick="deleteApiKey('custom')">删除</button>`:''}
    <button class="btn-primary" style="padding:8px 14px;font-size:.85em" onclick="saveCustomProvider()">保存自定义接口</button>
  </div>
</div>`;

  return `
<h2>🔑 API Key 管理</h2>
<div class="hint">Key 仅保存在本地 <code>.automind_config.json</code>，不会上传。当前使用：
  <b>${status.provider}/${status.model}</b> ${status.llm_ready?'✓ 已就绪':'⚠ 未就绪'}</div>
<div style="max-height:300px;overflow-y:auto">${rows}</div>
${customBlock}
<div class="btn-row"><button class="btn-secondary" onclick="closeModal()">关闭</button></div>`;
}
async function saveApiKey(provider) {
  const key = document.getElementById(`apikey-${provider}`).value.trim();
  if (!key) return toast('请输入 API Key', 'error');
  await fetch(`${API}/config/apikeys`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ provider, api_key: key }),
  });
  toast(`${provider} API Key 已保存`, 'success');
  await loadStatus();
  showModal('settings','apikeys');
}
async function deleteApiKey(provider) {
  await fetch(`${API}/config/apikeys`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ provider, api_key: '' }),
  });
  toast(`${provider} 配置已删除`, 'info');
  await loadStatus();
  showModal('settings','apikeys');
}
async function saveCustomProvider() {
  const api_base = document.getElementById('custom-apibase').value.trim();
  const model = document.getElementById('custom-model').value.trim();
  const api_key = document.getElementById('custom-apikey').value.trim();
  if (!api_base) return toast('请填写 API 地址 (api_base)', 'error');
  // 保存 api_base + model
  await fetch(`${API}/config/provider`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ provider:'custom', api_base, model }),
  });
  // 保存 key（若填写）
  if (api_key) {
    await fetch(`${API}/config/apikeys`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ provider:'custom', api_key, api_base, model }),
    });
  }
  toast('自定义接口已保存', 'success');
  await loadStatus();
  showModal('settings','apikeys');
}

// ── General Tab ──
async function renderGeneralTab() {
  const f = await (await fetch(`${API}/config/full`)).json();
  return `
<h2>⚙ 通用设置</h2>
<div class="hint">项目目录、采样参数与执行偏好。</div>
<label>项目目录（Agent 文件操作的根目录）</label>
<div style="display:flex;gap:8px">
  <input type="text" id="cfg-project" value="${esc(f.project||'.')}" style="flex:1">
  <button class="btn-secondary" style="padding:0 16px;white-space:nowrap" onclick="openDirPicker()">📁 浏览</button>
  <button class="btn-primary" style="padding:0 16px;white-space:nowrap" onclick="saveProject()">应用</button>
</div>
<div id="dir-picker" style="display:none;margin-top:8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg0);overflow:hidden">
  <div style="display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--border);background:var(--bg2)">
    <button class="btn-secondary" style="padding:4px 10px;font-size:.78em" onclick="dirUp()">⬆ 上级</button>
    <span id="dir-current" style="font-size:.78em;color:var(--text2);font-family:var(--mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
    <button class="btn-primary" style="padding:4px 10px;font-size:.78em" onclick="dirChoose()">✓ 选此目录</button>
  </div>
  <div id="dir-drives" style="padding:6px 10px;border-bottom:1px solid var(--border)"></div>
  <div id="dir-list" style="max-height:200px;overflow-y:auto;padding:4px"></div>
</div>
<label>Temperature：<span id="temp-val">${f.temperature}</span></label>
<input type="range" id="cfg-temp" min="0" max="2" step="0.1" value="${f.temperature}" style="width:100%"
  oninput="document.getElementById('temp-val').textContent=this.value">
<label>最大输出 Token</label>
<input type="number" id="cfg-max-tokens" value="${f.max_tokens}" min="256" max="32768">
<div class="btn-row">
  <button class="btn-secondary" onclick="closeModal()">取消</button>
  <button class="btn-primary" onclick="saveGeneralSettings()">保存采样参数</button>
</div>
<div id="autopilot-box" style="margin-top:18px;border-top:1px solid var(--border);padding-top:14px">
  <b style="font-size:.95em">🔄 自主任务闭环（工作 / 编程模式）</b>
  <div class="hint" style="margin:4px 0 10px">任务完成后自动 多Agent审查 → Loop 语义验收 → 未达标带反馈自动修复；编程模式每次改代码自动语法验证 + 收尾跑测试（TDD）。默认全开，可单独关闭。</div>
  <div id="autopilot-toggles" style="display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;font-size:.86em"></div>
</div>`;
}
const AUTOPILOT_LABELS = {
  auto_review:  ['🧐 多Agent审查', '工作模式完成后由审阅者角色复核（可用只读工具核实，含 MCP）'],
  auto_verify:  ['✅ Loop 验收', '语义判定是否真正完成，未过带反馈自动修复（最多 2 轮）'],
  auto_test:    ['🧪 TDD 测试', '编程模式：每次改 .py 后自动语法验证；收尾自动跑 pytest'],
  parallel_execution: ['⚡ 并行执行', '计划中互不依赖的步骤并发执行（asyncio.gather）'],
  subtask_cache: ['📦 子任务缓存', '同一任务内相同的只读工具调用结果复用'],
};
async function loadAutopilotToggles(){
  const box = document.getElementById('autopilot-toggles');
  if (!box) return;
  try {
    const flags = await (await fetch(`${API}/config/autopilot`)).json();
    box.innerHTML = Object.entries(AUTOPILOT_LABELS).map(([k,[label,tip]]) => `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer" title="${tip}">
        <input type="checkbox" ${flags[k]?'checked':''} onchange="setAutopilot('${k}', this.checked)"
          style="accent-color:var(--accent);width:15px;height:15px">
        <span>${label}</span>
      </label>`).join('');
  } catch(e) { box.innerHTML = '<em style="color:var(--text3)">加载失败</em>'; }
}
async function setAutopilot(flag, enabled){
  await fetch(`${API}/config/autopilot`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({[flag]: enabled})});
  toast(`${AUTOPILOT_LABELS[flag][0]} 已${enabled?'开启':'关闭'}`, enabled?'success':'info');
}

// ── Integrations Tab（IDE 面板：Continue.dev / 任意 OpenAI 客户端）──
async function renderIntegrationsTab() {
  let cfg = { base_url: location.origin + '/v1', model: '—', yaml: '', auth_required: false };
  try { cfg = await (await fetch(`${API}/integrations/continue`)).json(); } catch(e) {}
  const curl = `curl ${cfg.base_url}/chat/completions \\
  -H "Content-Type: application/json"${cfg.auth_required ? ' \\\n  -H "Authorization: Bearer <你的令牌>"' : ''} \\
  -d '{"model":"${cfg.model}","messages":[{"role":"user","content":"你好"}]}'`;
  const keyHint = cfg.auth_required ? '你的 AUTOMIND_AUTH_TOKEN 令牌' : '任意填（未开鉴权）';
  return `
<h2>🔌 Agent 集成</h2>
<div class="hint">把 AutoMind 作为「模型提供方」接入 IDE 里的 AI Agent（Continue / Cline 等）或任何
OpenAI 兼容客户端 —— 复用你在这里配好的模型、中转代理与企业网关，Key 只留在本机。</div>

<div class="card" style="margin-top:6px">
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:1.6em">🧩</span>
    <div style="flex:1">
      <b>Continue.dev — VS Code / JetBrains 侧边面板</b>
      <div style="font-size:.8em;color:var(--text3);margin-top:2px">编辑器里聊代码、改代码（chat / edit / apply），模型走 AutoMind（当前：${esc(cfg.model)}）</div>
    </div>
    <a href="https://www.continue.dev" target="_blank" rel="noopener noreferrer" style="font-size:.78em;color:var(--accent)">官网 ↗</a>
  </div>
  <ol class="md-list" style="font-size:.86em;margin-top:10px">
    <li>IDE 扩展市场搜索并安装 <b>Continue</b>（VS Code / JetBrains 均有）。</li>
    <li>打开 Continue 侧边面板 → 右上 ⚙ → <b>Open Config</b>（config.yaml）。</li>
    <li>把下面的配置粘贴进 <code>models:</code> 段，保存即用。</li>
  </ol>
  <div style="position:relative;margin-top:8px">
    <pre id="continue-yaml" style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:var(--mono);font-size:.78em;overflow-x:auto;white-space:pre">${esc(cfg.yaml || '加载失败，请刷新重试')}</pre>
    <button class="btn-primary" style="position:absolute;top:8px;right:8px;padding:4px 12px;font-size:.76em;border-radius:6px"
      onclick="copyText(document.getElementById('continue-yaml').innerText, this)">⧉ 复制配置</button>
  </div>
</div>

<div class="card" style="margin-top:12px">
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:1.6em">🤖</span>
    <div style="flex:1">
      <b>Cline — VS Code 自主编码 Agent</b>
      <div style="font-size:.8em;color:var(--text3);margin-top:2px">能读写文件、跑命令的编辑器内 Agent，用 AutoMind 当它的"大脑"</div>
    </div>
    <a href="https://cline.bot" target="_blank" rel="noopener noreferrer" style="font-size:.78em;color:var(--accent)">官网 ↗</a>
  </div>
  <div style="font-size:.86em;margin-top:10px;line-height:2">
    VS Code 安装 <b>Cline</b> 扩展 → 打开其设置（⚙）依次填入：<br>
    <b>API Provider：</b><code>OpenAI Compatible</code><br>
    <b>Base URL：</b><code>${esc(cfg.base_url)}</code>
    <button class="btn-secondary" style="padding:2px 10px;font-size:.78em;border-radius:6px;margin-left:6px"
      onclick="copyText('${jsq(cfg.base_url)}', this)">⧉</button><br>
    <b>API Key：</b>${esc(keyHint)}　<b>Model ID：</b><code>${esc(cfg.model)}</code>
    <button class="btn-secondary" style="padding:2px 10px;font-size:.78em;border-radius:6px;margin-left:6px"
      onclick="copyText('${jsq(cfg.model)}', this)">⧉</button>
  </div>
</div>

<div class="card" style="margin-top:12px">
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:1.6em">🌐</span>
    <div style="flex:1">
      <b>通用 OpenAI 兼容 API</b>
      <div style="font-size:.8em;color:var(--text3);margin-top:2px">Zed / Open WebUI / 脚本 / SDK…… 任何支持自定义 OpenAI 接口的工具都能接</div>
    </div>
  </div>
  <div style="font-size:.84em;margin-top:8px;line-height:2">
    <b>Base URL：</b><code>${esc(cfg.base_url)}</code>
    <button class="btn-secondary" style="padding:2px 10px;font-size:.78em;border-radius:6px;margin-left:6px"
      onclick="copyText('${jsq(cfg.base_url)}', this)">⧉</button><br>
    <b>端点：</b><code>POST /v1/chat/completions</code>（支持 <code>stream: true</code> SSE 流式）· <code>GET /v1/models</code>
  </div>
  <div style="position:relative;margin-top:8px">
    <pre id="curl-example" style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:var(--mono);font-size:.78em;overflow-x:auto;white-space:pre">${esc(curl)}</pre>
    <button class="btn-secondary" style="position:absolute;top:8px;right:8px;padding:4px 12px;font-size:.76em;border-radius:6px"
      onclick="copyText(document.getElementById('curl-example').innerText, this)">⧉ 复制</button>
  </div>
  <div style="font-size:.76em;color:var(--text3);margin-top:6px">
    说明：经 /v1 接入的外部 Agent 走纯对话语义（不会调用 AutoMind 的本机工具）；token 用量计入右栏统计与成本估算。
    ${cfg.auth_required ? '' : '⚠ 当前未开启访问鉴权（本机使用无碍）；暴露到局域网/公网前请设置 <code>AUTOMIND_AUTH_TOKEN</code>，各客户端的 apiKey 填同一令牌。'}</div>
</div>`;
}

// ── Directory picker ──
let _dirPath = '';
async function openDirPicker() {
  const box = document.getElementById('dir-picker');
  if (box.style.display === 'block') { box.style.display = 'none'; return; }
  box.style.display = 'block';
  await browseDir(document.getElementById('cfg-project').value || '');
}
async function browseDir(path) {
  try {
    const r = await (await fetch(`${API}/fs/list?path=${encodeURIComponent(path)}`)).json();
    if (r.error) { toast(r.error, 'error'); return; }
    _dirPath = r.path;
    document.getElementById('dir-current').textContent = r.path;
    document.getElementById('dir-current').title = r.path;
    const drives = document.getElementById('dir-drives');
    drives.innerHTML = (r.drives||[]).length
      ? '<span style="font-size:.74em;color:var(--text3)">磁盘：</span> ' + r.drives.map(d =>
        `<button class="btn-secondary" style="padding:2px 8px;font-size:.76em;margin:2px" onclick="browseDir('${jsq(d)}')">${esc(d)}</button>`).join('')
      : '';
    const list = document.getElementById('dir-list');
    list.innerHTML = (r.dirs||[]).length
      ? r.dirs.map(d => `<div style="padding:6px 10px;border-radius:6px;cursor:pointer;font-size:.84em;display:flex;align-items:center;gap:6px"
          onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background='none'"
          onclick="browseDir('${jsq((_dirPath+'/'+d).replace(/\\/g,'/'))}')">📁 ${esc(d)}</div>`).join('')
      : '<div style="padding:10px;color:var(--text3);font-size:.82em">（无子目录）</div>';
  } catch(e) { toast('浏览失败: '+e.message, 'error'); }
}
function dirUp() {
  const cur = _dirPath;
  const parent = cur.replace(/[\\/][^\\/]*$/, '') || cur;
  browseDir(parent);
}
function dirChoose() {
  document.getElementById('cfg-project').value = _dirPath;
  document.getElementById('dir-picker').style.display = 'none';
  saveProject();
}
async function saveProject() {
  const path = document.getElementById('cfg-project').value.trim();
  const r = await (await fetch(`${API}/config/project`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ path }),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast('项目目录已设置: ' + r.project, 'success');
  loadStatus();
}
async function saveGeneralSettings() {
  await fetch(`${API}/config`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      temperature: parseFloat(document.getElementById('cfg-temp').value),
      max_tokens: parseInt(document.getElementById('cfg-max-tokens').value),
    }),
  });
  closeModal(); toast('设置已保存', 'success');
}

// ═══════ Sidebar Views ═══════
document.querySelectorAll('#sidebar nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    const view = btn.dataset.view;
    // 离开对话区前先保存当前模式的会话内容
    captureTranscript();
    document.querySelectorAll('#sidebar nav button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    if (view === 'chat') { showConversation(currentMode); return; }
    currentView = view;
    if (view === 'tools') loadToolsView();
    else if (view === 'history') loadHistoryView();
    else if (view === 'plan') loadPlanView();
    else if (view === 'audit') loadAuditView();
    else if (view === 'stats') loadStatsView();
    else if (view === 'schedule') loadScheduleView();
  });
});

