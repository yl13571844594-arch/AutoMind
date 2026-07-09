const API = '/api';
let running = false;
let ws = null;
let providerData = null;
let currentMode = 'chat';

// ── 版本（Edition）状态：社区版隐藏/锁定商业功能 ──
let EDITION = 'community';
let FEATURES = {};   // 服务端 /api/status 返回的特性开关
const EDITION_LABELS = {community:'社区版', pro:'专业版', enterprise:'企业版'};
// 模式 → 所需特性键（无映射的模式为社区版功能）
const MODE_FEATURE = {multi:'multi_agent', loop:'loop_engine'};
function featureOn(key){ return !key || !!FEATURES[key]; }
function upgradeToast(label){
  toast(`「${label}」是专业版功能 — 当前为${EDITION_LABELS[EDITION]||EDITION}，请安装 automind-pro 并配置许可证`, 'error');
}

// 多用户会话隔离：每个浏览器拥有独立的会话 ID（持久化于 localStorage）
function getSessionId(){
  let s = localStorage.getItem('automind_sid');
  if(!s){ s = 's_' + Math.random().toString(36).slice(2,10) + Date.now().toString(36); localStorage.setItem('automind_sid', s); }
  return s;
}
const SID = getSessionId();
// 对话上下文按工作区隔离：默认工作区后缀为 ''（兼容既有历史）
function chatSid(){ return SID + (window.WS_SUFFIX || ''); }

// ── 实时 Token 成本估算 ──
// 默认单价表（¥/百万 token，按公开定价粗估，可点击成本数字自定义覆盖）
const MODEL_PRICES = [
  ['deepseek-reasoner', 4, 16], ['deepseek', 2, 8],
  ['gpt-4o-mini', 1.1, 4.4], ['gpt-4o', 18, 72], ['gpt-4.1-mini', 3, 12], ['gpt-4.1', 14, 57],
  ['o4-mini', 8, 32], ['o3', 14, 57],
  ['claude-3-5-haiku', 6, 29], ['claude-haiku', 6, 29], ['claude', 22, 108],
  ['kimi', 12, 12], ['moonshot', 12, 12],
  ['glm-4-flash', 0.1, 0.1], ['glm', 5, 5],
  ['qwen-turbo', 0.3, 0.6], ['qwen-plus', 0.8, 2], ['qwen', 2, 6],
  ['doubao', 0.8, 2],
  ['gemini-2.0-flash', 0.8, 3], ['gemini-1.5-flash', 0.6, 2.4], ['gemini', 9, 36],
  ['grok', 22, 108],
  ['ollama', 0, 0], ['llama', 0, 0],
];
function modelPrice(){
  try {
    const ov = JSON.parse(localStorage.getItem('automind_price') || 'null');
    if (ov && (ov.prompt >= 0) && (ov.completion >= 0)) return [ov.prompt, ov.completion, true];
  } catch(e){}
  const m = (window._curModel || '').toLowerCase();
  for (const [key, p, c] of MODEL_PRICES) { if (m.includes(key)) return [p, c, false]; }
  return [null, null, false];
}
function estCost(promptTk, completionTk){
  const [p, c] = modelPrice();
  if (p == null) return null;
  return (promptTk || 0) / 1e6 * p + (completionTk || 0) / 1e6 * c;
}
function fmtCost(v){
  if (v == null) return null;
  return '≈¥' + (v < 0.01 ? v.toFixed(4) : v.toFixed(2));
}
function configPricing(){
  const [p, c, custom] = modelPrice();
  const cur = custom ? '（当前为自定义单价）' : (p != null ? `（当前 ${window._curModel||'模型'} 默认：¥${p}/¥${c}）` : '（当前模型无内置单价）');
  const inp = prompt(`设置 Token 单价用于成本估算 ${cur}\n格式：输入单价/百万tk,输出单价/百万tk（如 2,8）\n留空并确定 = 恢复内置单价表`, custom ? `${p},${c}` : '');
  if (inp === null) return;
  if (!inp.trim()) { localStorage.removeItem('automind_price'); toast('已恢复内置单价表', 'info'); refreshTokens(); return; }
  const m = inp.split(/[,，]/).map(s => parseFloat(s.trim()));
  if (m.length !== 2 || m.some(v => isNaN(v) || v < 0)) return toast('格式错误，示例：2,8', 'error');
  localStorage.setItem('automind_price', JSON.stringify({prompt: m[0], completion: m[1]}));
  toast(`单价已设置：输入¥${m[0]} / 输出¥${m[1]} 每百万token`, 'success');
  refreshTokens();
}

// 每个模式独立的会话内容（切换模式互不影响）
let currentView = 'chat';           // 'chat'=对话区，否则为面板名
let modeTranscripts = {};           // mode -> #messages innerHTML
let _taskMode = null;               // 当前正在执行的任务所属模式
let _pendingResults = {};           // mode -> [{type, data, ...}] 在面板视图中到达的任务结果缓存
function _tkey(){ return 'automind_transcripts_' + SID + (window.WS_SUFFIX || ''); }
// localStorage 上限：单模式 300KB、总量 1.2MB，超限保留尾部（最新内容）并逐模式回收
const _TX_PER_MODE = 300 * 1024, _TX_TOTAL = 1200 * 1024;
function loadTranscripts(){ try{ modeTranscripts = JSON.parse(localStorage.getItem(_tkey())||'{}')||{}; }catch(e){ modeTranscripts={}; } }
function _trimTranscripts(){
  // 单模式截断：超限时保留最后 _TX_PER_MODE 字符（最新消息在尾部）
  for(const k in modeTranscripts){
    const v = modeTranscripts[k] || '';
    if(v.length > _TX_PER_MODE) modeTranscripts[k] = v.slice(v.length - _TX_PER_MODE);
  }
  // 总量控制：仍超限则丢弃体积最大的模式，直到达标
  let total = () => Object.values(modeTranscripts).reduce((s,v)=>s+(v?v.length:0),0);
  while(total() > _TX_TOTAL){
    const big = Object.keys(modeTranscripts).sort((a,b)=>(modeTranscripts[b]||'').length-(modeTranscripts[a]||'').length)[0];
    if(!big) break;
    delete modeTranscripts[big];
  }
}
function persistTranscripts(){
  _trimTranscripts();
  try{ localStorage.setItem(_tkey(), JSON.stringify(modeTranscripts)); }
  catch(e){
    // 配额溢出兜底：清掉除当前模式外的所有历史再试一次
    try{
      const cur = modeTranscripts[currentMode];
      modeTranscripts = cur ? {[currentMode]: cur.slice(-_TX_PER_MODE)} : {};
      localStorage.setItem(_tkey(), JSON.stringify(modeTranscripts));
    }catch(_){}
  }
}
function captureTranscript(){
  if(currentView==='chat'){
    // 保存前剥离未完成的流式/执行中间态元素（已 finalize 的元素 ID 会被清掉）
    const msgs = document.getElementById('messages');
    const clone = msgs.cloneNode(true);
    clone.querySelectorAll('#typing-msg, #stream-msg, #exec-msg, #ma-msg, #loop-msg').forEach(e => e.remove());
    modeTranscripts[currentMode] = clone.innerHTML;
    persistTranscripts();
  }
}
async function showConversation(mode){
  currentView = 'chat';
  document.querySelectorAll('#sidebar nav button').forEach(b=>b.classList.toggle('active', b.dataset.view==='chat'));
  if(modeTranscripts[mode]!=null && modeTranscripts[mode]!==''){
    document.getElementById('messages').innerHTML = modeTranscripts[mode];
  } else if(mode==='chat'){
    await restoreChat();
  } else {
    renderWelcome();
  }
  // 刷新该模式缓存的任务结果
  flushPendingResults(mode);
  document.getElementById('messages').scrollTop = 1e9;
}

// ── 后台任务结果路由：确保结果保存到正确的模式 transcript ──
function flushPendingResults(mode) {
  const pending = _pendingResults[mode];
  if (!pending || !pending.length) return;
  const msgs = document.getElementById('messages');
  pending.forEach(item => {
    if (item.type === 'result') appendResultToDOM(msgs, item.data);
    else if (item.type === 'message') appendMessage(item.role, item.text, item.images);
    else if (item.type === 'exec_panel') {
      const div = document.createElement('div'); div.innerHTML = item.html;
      while (div.firstChild) msgs.appendChild(div.firstChild);
    }
  });
  delete _pendingResults[mode];
  captureTranscript();
}

function routeResultToMode(mode, type, payload) {
  if (currentView === 'chat' && currentMode === mode) {
    // 当前查看该模式对话区 → 直接渲染
    const msgs = document.getElementById('messages');
    if (type === 'result') appendResultToDOM(msgs, payload);
    else if (type === 'message') appendMessage(payload.role, payload.text, payload.images);
    else if (type === 'exec_panel') {
      const div = document.createElement('div'); div.innerHTML = payload.html;
      while (div.firstChild) msgs.appendChild(div.firstChild);
    }
    captureTranscript();
  } else {
    // 用户在其它面板/模式 → 缓存结果并更新 transcript
    if (modeTranscripts[mode]) {
      // 有已保存 transcript → 直接写入最终结果，无需额外 pending（避免双重显示）
      const tmp = document.createElement('div');
      tmp.innerHTML = modeTranscripts[mode];
      // 剥离未完成的流式/执行中间态（避免 Q&A 之间残留断板）
      tmp.querySelectorAll('#typing-msg, #stream-msg, #exec-msg, #ma-msg, #loop-msg').forEach(e => e.remove());
      if (type === 'result') appendResultToDOM(tmp, payload);
      else if (type === 'message') appendMessageTo(tmp, payload.role, payload.text);
      else if (type === 'exec_panel') { const d = document.createElement('div'); d.innerHTML = payload.html; while (d.firstChild) tmp.appendChild(d.firstChild); }
      modeTranscripts[mode] = tmp.innerHTML;
      persistTranscripts();
    } else {
      // 无 transcript（尚未进入过该模式）→ 缓存到 pending 队列，等 showConversation 时刷新
      if (!_pendingResults[mode]) _pendingResults[mode] = [];
      _pendingResults[mode].push({ type, data: payload, role: payload.role, text: payload.text, images: payload.images, html: payload.html });
    }
  }
}

function appendResultToDOM(container, data) {
  let meta = [];
  const what = data.interaction || '';
  if (what && what !== 'chat') {
    if (data.steps) meta.push(`${data.steps}步`);
    if (data.backtracks) meta.push(`${data.backtracks}回溯`);
  }
  if (data.tokens) {
    meta.push(`🪙 ${data.tokens}tk (${data.prompt_tokens||0}↑/${data.completion_tokens||0}↓)`);
    const cost = fmtCost(estCost(data.prompt_tokens, data.completion_tokens));
    if (cost) meta.push(cost);
  }
  if (data.duration_ms) meta.push(`${data.duration_ms}ms`);
  const metaStr = meta.length ? meta.join(' · ') : new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'msg agent';
  div.innerHTML = `<div class="avatar">AM</div><div class="col">
    <div class="bubble"><button class="copy-msg" title="复制此条">⧉</button>${formatContent(data.output||'任务完成')}</div>
    <div class="time">${metaStr}</div></div>`;
  container.appendChild(div);
  refreshTokens();
}

// 统一的消息元素构造器 — appendMessage / appendMessageTo 共用，消除 90% 重复。
// （formatContent/esc/isSafeUrl 定义于 chat.js，运行期调用，加载顺序无碍）
function buildMessageEl(role, content, images) {
  const cls = role === 'user' ? 'user' : 'agent';
  const avatar = role === 'user' ? '我' : 'AM';
  const thumbs = (images && images.length)
    ? `<div class="mm-thumbs">${images.filter(isSafeUrl).map(u => `<img src="${esc(u)}" alt="img">`).join('')}</div>` : '';
  const copyBtn = role === 'agent' ? '<button class="copy-msg" title="复制此条">⧉</button>' : '';
  const div = document.createElement('div');
  div.className = `msg ${cls}`;
  div.innerHTML = `<div class="avatar">${avatar}</div><div class="col">`
    + `<div class="bubble">${copyBtn}${thumbs}${content ? formatContent(content) : ''}</div>`
    + `<div class="time">${new Date().toLocaleTimeString()}</div></div>`;
  return div;
}
function appendMessageTo(container, role, text, images) {
  container.appendChild(buildMessageEl(role, text, images));
}

const MODE_HINTS = {
  chat:   `💬 <b>对话模式</b> — 纯多轮对话，不调用工具，响应最快。支持图片输入（视觉模型）。`,
  work:   `⚙️ <b>工作模式</b> — 分层规划 + 工具执行 + 符号验证。会自动建目录、写文件、跑命令完成任务。`,
  coding: `💻 <b>编程模式</b> — ReAct 思考-行动循环，聚焦读写代码、运行命令与测试。`,
  multi:  `🤝 <b>协同模式</b> — 多智能体协作：协调者拆解任务，规划/研究/编程/审阅角色分工完成并综合。`,
  loop:   `🔁 <b>循环模式</b> — Loop Engineering：自主"行动-观察-修正"闭环，自动迭代直到任务完成或达到停止条件。`,
};
const MODE_LABELS = {chat:'对话',work:'工作',coding:'编程',multi:'协同',loop:'循环'};
const MODE_PLACEHOLDER = {
  chat:   '输入消息，Enter 发送，Shift+Enter 换行...',
  work:   '描述你想完成的任务，AutoMind 会自主规划并执行...',
  coding: '描述编程需求（创建/修复/重构/测试），AutoMind 会读写代码并运行...',
  multi:  '描述一个较复杂的任务，多个智能体将分工协作完成...',
  loop:   '描述一个需要反复迭代直到达标的目标，系统将自主循环修正...',
};

// ── Init ──
async function init() {
  loadTranscripts();
  await loadProviders();
  await loadStatus();              // 采用服务端当前交互模式 + 该模式模型
  connectWS();
  await showConversation(currentMode);  // 恢复该模式的会话内容
  refreshAuditMini();
  refreshTokens();
  refreshHtmlFiles();
  refreshChanges();
  loadAppVersion();                // 版本号动态读取（§14.1 单一数据源）
}
async function loadAppVersion() {
  try {
    const h = await (await fetch(`${API}/health`)).json();
    const el = document.getElementById('app-version');
    if (el && h.version) el.textContent = 'v' + h.version;
  } catch(e) {}
}
document.addEventListener('DOMContentLoaded', init);

async function restoreChat() {
  try {
    const h = await (await fetch(`${API}/chat/history?session_id=${encodeURIComponent(chatSid())}`)).json();
    const msgs = (h.messages || []).filter(m => m.role === 'user' || m.role === 'assistant');
    if (!msgs.length) { renderWelcome(); return; }
    document.getElementById('messages').innerHTML = '';
    msgs.forEach(m => appendMessage(m.role === 'user' ? 'user' : 'agent', m.content));
  } catch(e) { renderWelcome(); }
}

async function loadStatus(forMode) {
  try {
    const q = forMode ? `?interaction=${encodeURIComponent(forMode)}` : '';
    const s = await (await fetch(`${API}/status${q}`)).json();
    if (s.edition) { EDITION = s.edition; FEATURES = s.features || {}; applyEditionUI(); }
    if (!forMode) currentMode = s.interaction || 'chat';
    applyModeUI(currentMode);
    const mb = document.getElementById('model-badge');
    window._curModel = s.model || '';   // 供成本估算取单价
    const modeLabel = (s.mode_specific ? `${MODE_LABELS[currentMode]||currentMode}模式专用 · ` : '默认 · ');
    if (s.llm_ready) {
      mb.textContent = `${s.provider}/${s.model}`;
      mb.className = 'badge badge-ok';
      mb.title = modeLabel + '已就绪';
    } else if (s.has_api_key) {
      mb.textContent = `${s.provider}/${s.model} ⚠`;
      mb.className = 'badge badge-warn';
      mb.title = modeLabel + (s.llm_error || 'LLM 未初始化，点击重试') + ' — 点击模型按钮检查配置';
    } else {
      mb.textContent = s.has_api_key ? `${s.provider}/${s.model} ⚠` : `⚠ 未配置`;
      mb.className = 'badge badge-err';
      mb.title = modeLabel + (s.llm_error || '未配置 API Key，点击「🔑 API Keys」配置');
    }
    const pb = document.getElementById('project-badge');
    if (pb && s.project) {
      const parts = s.project.replace(/[\\/]+$/,'').split(/[\\/]/);
      pb.textContent = '📁 ' + (parts[parts.length-1] || s.project);
      pb.title = '项目目录: ' + s.project;
    }
    const ap = document.getElementById('approval-select');
    if (ap && s.approval_mode) ap.value = s.approval_mode;
  } catch(e) { console.error('status:', e); }
}

async function loadProviders() {
  try { providerData = await (await fetch(`${API}/providers`)).json(); } catch(e) {}
}

function renderWelcome() {
  document.getElementById('messages').innerHTML = `
<div class="msg agent">
  <div class="avatar">AM</div>
  <div class="col">
    <div class="bubble">
      <b>👋 欢迎使用 AutoMind 通用自动化 Agent</b><br><br>
      顶部可切换五种模式：<br>
      • 💬 <b>对话</b> — 像聊天一样问答交流（支持图片输入 / 视觉模型）<br>
      • ⚙️ <b>工作</b> — 自主规划并执行任务（建项目、跑命令、改文件）<br>
      • 💻 <b>编程</b> — 聚焦代码：阅读、编写、调试、重构、测试<br>
      • 🤝 <b>协同</b> — 多智能体分工协作（规划/研究/编程/审阅）并综合${featureOn('multi_agent')?'':' <span style="font-size:.82em;color:var(--text3)">🔒专业版</span>'}<br>
      • 🔁 <b>循环</b> — 自主"行动-观察-修正"闭环，迭代到达标为止${featureOn('loop_engine')?'':' <span style="font-size:.82em;color:var(--text3)">🔒专业版</span>'}<br><br>
      <span style="color:var(--text3);font-size:.88em;">
      顶部可设置 <b>审批模式</b>（🙋询问 / ⚡自动 / ✅全批准）；侧边栏有 📊 统计分析、⏰ 定时任务、🔧 工具/技能/MCP、🛡️ 安全审计。<br>
      ⚙ 首次使用请先点击右上角 <b>🔑 API Keys</b> 配置模型。<br>
      支持 OpenAI / Claude / DeepSeek / Kimi / 百炼 / 智谱 / 豆包 / Gemini / Grok / Ollama，
      以及 <b>自定义 OpenAI 标准接口（中转代理）</b>。
      </span>
      <div style="margin-top:12px;border-top:1px dashed var(--border);padding-top:10px">
        <span style="font-size:.85em;color:var(--text2)">🚀 快速开始（点击模板一键填入）：</span>
        <div class="tpl-chips" style="margin-top:8px">
          ${(window.TEMPLATES||[]).slice(0,5).map((t,i)=>
            `<button class="tpl-chip" onclick="useTemplate(${i})">${t.icon} ${esc(t.title)}</button>`).join('')}
          <button class="tpl-chip" onclick="showTemplates()">📚 全部模板…</button>
        </div>
      </div>
    </div>
    <div class="time">现在</div>
  </div>
</div>`;
}

// ── Mode switching ──
async function setMode(mode) {
  // 商业功能门控：社区版点击协同/循环给出升级提示，不切换
  if (!featureOn(MODE_FEATURE[mode])) { upgradeToast(MODE_LABELS[mode]||mode); return; }
  if (mode === currentMode && currentView === 'chat') return;
  captureTranscript();             // 保存当前模式的会话内容
  currentMode = mode;
  applyModeUI(mode);
  await showConversation(mode);     // 恢复目标模式的会话内容
  loadStatus(mode);                 // 刷新为该模式的模型显示
  try {
    await fetch(`${API}/config`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ interaction: mode }),
    });
  } catch(e) {}
  toast(`已切换到${MODE_LABELS[mode]||mode}模式`, 'info');
}
async function setApproval(mode) {
  try {
    await fetch(`${API}/config/approval`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_mode: mode})});
    toast(`审批模式：${({ask:'询问',auto:'自动',approve_all:'全批准'})[mode]}`, 'info');
  } catch(e) {}
}
function applyModeUI(mode) {
  document.querySelectorAll('#mode-switch button').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === mode));
  document.getElementById('mode-hint').innerHTML = MODE_HINTS[mode] || '';
  document.getElementById('user-input').placeholder = MODE_PLACEHOLDER[mode] || '';
  // 高级模式（协同/循环）激活时自动展开高级组，避免"选中却被折叠"
  if (mode === 'multi' || mode === 'loop') toggleAdvancedModes(true);
}
// 按版本锁定商业功能入口：模式按钮（协同/循环）+ 侧边栏（定时任务）加 🔒 标注
function applyEditionUI() {
  document.querySelectorAll('#mode-switch button[data-mode]').forEach(b => {
    const need = MODE_FEATURE[b.dataset.mode];
    const locked = !featureOn(need);
    b.classList.toggle('locked', locked);
    if (locked && !b.querySelector('.lock-ico')) {
      const s = document.createElement('span'); s.className = 'lock-ico'; s.textContent = '🔒';
      b.appendChild(s);
      b.title = (b.title || '') + '（专业版功能）';
    } else if (!locked) {
      const ico = b.querySelector('.lock-ico'); if (ico) ico.remove();
    }
  });
  const schBtn = document.querySelector('#sidebar nav button[data-view="schedule"]');
  if (schBtn) {
    const locked = !featureOn('scheduler');
    schBtn.classList.toggle('locked', locked);
    if (locked && !schBtn.querySelector('.lock-ico')) {
      const s = document.createElement('span'); s.className = 'lock-ico'; s.textContent = '🔒';
      schBtn.appendChild(s);
    } else if (!locked) {
      const ico = schBtn.querySelector('.lock-ico'); if (ico) ico.remove();
    }
  }
  const eb = document.getElementById('edition-badge');
  if (eb) {
    eb.textContent = EDITION_LABELS[EDITION] || EDITION;
    eb.className = 'edition-badge ' + EDITION;
    eb.title = EDITION === 'community'
      ? '社区版（开源免费）— 协同/循环/定时任务/高级统计为专业版功能'
      : `已激活 AutoMind ${EDITION_LABELS[EDITION]}`;
  }
}

// 折叠/展开高级模式组：3 主模式常驻，协同/循环收进"⋯ 高级"以免新用户被 5 个按钮吓到
function toggleAdvancedModes(force) {
  const adv = document.getElementById('mode-advanced');
  const btn = document.getElementById('mode-more-btn');
  if (!adv || !btn) return;
  const show = (force !== undefined) ? force : !adv.classList.contains('show');
  adv.classList.toggle('show', show);
  btn.classList.toggle('on', show);
}

