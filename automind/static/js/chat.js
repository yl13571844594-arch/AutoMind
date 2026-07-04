<<<<<<< HEAD
// ── Send ──
async function sendMessage() {
  if (running) return;
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  const images = _pendingImages.slice();
  if (!text && !images.length) return;

  input.value = ''; input.style.height = 'auto';
  // 若当前在某个面板视图，先切回该模式的对话区
  if (currentView !== 'chat') { await showConversation(currentMode); }
  appendMessage('user', text, images);
  clearAttachments();
  _taskMode = currentMode;  // 记录任务所属模式
  setRunning(true);

  // 优先走 WebSocket（流式 + 可中断），否则回退到 REST
  if (ws && ws.readyState === WebSocket.OPEN) {
    appendTyping();
    ws.send(JSON.stringify({ action: 'run', task: text, interaction: currentMode, images, session_id: SID }));
    return;
  }
  await sendViaRest(text, images);
}

async function sendViaRest(text, images) {
  const taskMode = _taskMode || currentMode;
  appendTyping();
  try {
    const r = await fetch(`${API}/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ task: text, interaction: taskMode, images, session_id: SID }),
    });
    const data = await r.json();
    removeTyping(); setRunning(false);
    if (data.error) {
      routeResultToMode(taskMode, 'message', { role: 'agent', text: `❌ **错误**: ${data.error}` });
    } else {
      routeResultToMode(taskMode, 'result', data);
      updateStats(data);
      if (data.plan) updatePlanView(data.plan);
      refreshAuditMini(); refreshHtmlFiles(); loadStatus();
    }
  } catch(e) {
    removeTyping(); setRunning(false);
    routeResultToMode(taskMode, 'message', { role: 'agent', text: `❌ 请求失败: ${e.message}` });
  }
}
function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}
function setRunning(on) {
  running = on;
  document.getElementById('send-btn').style.display = on ? 'none' : 'flex';
  document.getElementById('stop-btn').style.display = on ? 'flex' : 'none';
  document.getElementById('user-input').disabled = on;
  updateStatus(on ? 'running' : 'connected');
  if (!on) { captureTranscript(); _taskMode = null; }
}
function stopTask() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'stop' }));
    toast('正在中断任务...', 'info');
  } else {
    setRunning(false); removeTyping();
    toast('已停止等待（后台任务可能仍在运行）', 'info');
  }
}

// ── 图片附件（多模态输入）──
let _pendingImages = [];
function attachImage() { document.getElementById('img-input').click(); }
function onImagesPicked(e) {
  const files = Array.from(e.target.files || []);
  files.forEach(f => {
    if (!f.type.startsWith('image/')) return;
    if (f.size > 8*1024*1024) { toast('图片不能超过 8MB', 'error'); return; }
    const reader = new FileReader();
    reader.onload = ev => { _pendingImages.push(ev.target.result); renderAttachments(); };
    reader.readAsDataURL(f);
  });
  e.target.value = '';
}
function renderAttachments() {
  const strip = document.getElementById('attach-strip');
  strip.innerHTML = _pendingImages.map((u, i) =>
    `<div class="thumb"><img src="${u}"><button class="rm" onclick="removeAttachment(${i})">✕</button></div>`).join('');
}
function removeAttachment(i) { _pendingImages.splice(i, 1); renderAttachments(); }
function clearAttachments() { _pendingImages = []; renderAttachments(); }

// ── 语音输入（Web Speech API）──
let _recognition = null, _recognizing = false;
function toggleVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return toast('当前浏览器不支持语音识别，请使用 Chrome 或 Edge', 'error');
  if (_recognizing) { _recognition && _recognition.stop(); return; }
  _recognition = new SR();
  _recognition.lang = 'zh-CN';
  _recognition.interimResults = true;
  _recognition.continuous = false;
  const input = document.getElementById('user-input');
  const base = input.value;
  _recognition.onstart = () => { _recognizing = true; document.getElementById('mic-btn').classList.add('recording'); toast('正在聆听...', 'info'); };
  _recognition.onerror = (ev) => { toast('语音识别失败: ' + ev.error, 'error'); };
  _recognition.onend = () => { _recognizing = false; document.getElementById('mic-btn').classList.remove('recording'); };
  _recognition.onresult = (ev) => {
    let txt = '';
    for (let i = 0; i < ev.results.length; i++) txt += ev.results[i][0].transcript;
    input.value = (base ? base + ' ' : '') + txt;
    input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  };
  _recognition.start();
}

// ── HTML 预览 ──
let _previewNewTabHtml = '';
function previewHtml(i) {
  const html = (window._htmlBlocks && window._htmlBlocks[i]) || '';
  openPreview(html, '内联 HTML');
}
function previewHtmlData(btn) {
  let html = '';
  try { html = decodeURIComponent(btn.getAttribute('data-hblk') || ''); } catch(_) {}
  openPreview(html, '内联 HTML');
}
function openPreview(html, label) {
  _previewNewTabHtml = html;
  document.getElementById('preview-frame').srcdoc = html;
  document.getElementById('preview-path').textContent = label || '';
  document.getElementById('preview-overlay').classList.add('show');
}
async function previewFile(path) {
  document.getElementById('preview-frame').removeAttribute('srcdoc');
  document.getElementById('preview-frame').src = `${API}/preview/file?path=${encodeURIComponent(path)}`;
  document.getElementById('preview-path').textContent = path;
  _previewNewTabHtml = '';
  document.getElementById('preview-overlay').classList.add('show');
}
function closePreview() {
  document.getElementById('preview-overlay').classList.remove('show');
  const f = document.getElementById('preview-frame');
  f.removeAttribute('srcdoc'); f.src = 'about:blank';
}
function openPreviewNewTab() {
  const f = document.getElementById('preview-frame');
  if (_previewNewTabHtml) {
    const blob = new Blob([_previewNewTabHtml], {type:'text/html'});
    window.open(URL.createObjectURL(blob), '_blank');
  } else if (f.src && f.src !== 'about:blank') {
    window.open(f.src, '_blank');
  }
}

// ── Token 用量 ──
async function refreshTokens() {
  try {
    const t = await (await fetch(`${API}/tokens`)).json();
    document.getElementById('tok-prompt').textContent = (t.prompt||0).toLocaleString();
    document.getElementById('tok-completion').textContent = (t.completion||0).toLocaleString();
    document.getElementById('tok-total').textContent = (t.total||0).toLocaleString();
    document.getElementById('tok-tasks').textContent = t.tasks||0;
  } catch(e) {}
}
async function resetTokens() { await fetch(`${API}/tokens`, {method:'DELETE'}); refreshTokens(); toast('Token 统计已重置', 'info'); }

async function refreshHtmlFiles() {
  try {
    const files = await (await fetch(`${API}/files/html`)).json();
    const el = document.getElementById('html-files');
    if (!el) return;
    if (!files.length) { el.innerHTML = '<em style="color:var(--text3)">项目中暂无 HTML 文件</em>'; return; }
    el.innerHTML = files.slice(0,8).map(f =>
      `<div style="display:flex;align-items:center;gap:6px;padding:4px 0;cursor:pointer" onclick="previewFile('${jsq(f.path)}')"
        onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--text2)'">
        🔍 <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.path)}</span></div>`).join('');
  } catch(e) {}
}

// ── Messages ──
function appendMessage(role, content, images) {
  // 委托统一构造器（buildMessageEl 定义于 core.js），仅负责挂载与滚动
  const msgs = document.getElementById('messages');
  msgs.appendChild(buildMessageEl(role, content, images));
  msgs.scrollTop = msgs.scrollHeight;
}
function appendResult(data) {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent';
  let meta = [];
  if (data.interaction && data.interaction !== 'chat') {
    if (data.steps) meta.push(`${data.steps}步`);
    if (data.backtracks) meta.push(`${data.backtracks}回溯`);
  }
  if (data.tokens) meta.push(`🪙 ${data.tokens}tk (${data.prompt_tokens||0}↑/${data.completion_tokens||0}↓)`);
  if (data.duration_ms) meta.push(`${data.duration_ms}ms`);
  const metaStr = meta.length ? meta.join(' · ') : new Date().toLocaleTimeString();
  let output = data.output || '任务完成';
  div.innerHTML = `
    <div class="avatar">AM</div>
    <div class="col">
      <div class="bubble"><button class="copy-msg" title="复制此条">⧉</button>${formatContent(output)}</div>
      <div class="time">${metaStr}</div>
    </div>`;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
  refreshTokens();
}
function appendTyping() {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent'; div.id = 'typing-msg';
  div.innerHTML = `<div class="avatar">AM</div><div class="col"><div class="bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>`;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
}
function removeTyping() { const el = document.getElementById('typing-msg'); if (el) el.remove(); }

function updateStats(data) {
  document.getElementById('stat-steps').textContent = data.steps || 0;
  document.getElementById('stat-backtracks').textContent = data.backtracks || 0;
  document.getElementById('stat-tokens').textContent = data.tokens || 0;
  document.getElementById('stat-duration').textContent = (data.duration_ms || 0) + 'ms';
}
function updatePlanView(plan) {
  const el = document.getElementById('plan-view');
  if (!plan || !plan.root_goal) { el.innerHTML = '<em style="color:var(--text3)">无计划数据</em>'; return; }
  el.innerHTML = `<div class="plan-tree">${renderGoal(plan.root_goal, '')}</div>`;
}
function renderGoal(g, indent) {
  const icons = { pending:'○', in_progress:'◐', completed:'✓', failed:'✗', blocked:'⊘', reverted:'↺' };
  const cls = { pending:'pending', in_progress:'running', completed:'done', failed:'fail', blocked:'pending', reverted:'fail' };
  let html = `<div class="node ${cls[g.status]||''}">${indent}${icons[g.status]||'?'} ${esc(g.description)}`;
  if (g.action) html += ` <span style="color:var(--text3)">[${esc(g.action)}]</span>`;
  html += '</div>';
  if (g.children) g.children.forEach(c => { html += renderGoal(c, indent + '  '); });
  return html;
}
// HTML 转义 — 覆盖文本上下文与双引号属性上下文（& < > " ' `）
const _ESC_MAP = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'};
function esc(t){ return (t==null?'':String(t)).replace(/[&<>"'`]/g, c => _ESC_MAP[c]); }
// JS 字符串-in-HTML属性 转义 — 用于内联事件处理器里 [fn 单引号参数] 的插值，
// 同时防 JS 串逃逸（\ '）与 HTML 属性逃逸（" < > &），彻底堵住双上下文注入。
function jsq(t){
  return String(t==null?'':t)
    .replace(/\\/g,'\\\\').replace(/'/g,"\\'")
    .replace(/&/g,'&amp;').replace(/"/g,'&quot;')
    .replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\r/g,'').replace(/\n/g,'\\n');
}
// 图片可接受 http(s) 与 data:image/*（内联多模态缩略图）
function isSafeUrl(u){ return /^(https?:\/\/|data:image\/)/i.test(u||''); }
// 链接仅接受 http(s)：禁止 data:/javascript: —— data:image/svg 在 <a> 点击后会执行脚本
function isSafeHref(u){ return /^https?:\/\//i.test(u||''); }
function formatContent(text, resetArrays) {
  // 1) 先抽取所有代码块（避免内容被 esc() 二次转义）
  // 流式渲染时每帧全量替换旧内容 → 旧索引可安全清理；非流式追加消息 → 旧索引需保留
  window._htmlBlocks = window._htmlBlocks || [];
  window._codeBlocks = window._codeBlocks || [];
  if (resetArrays) {
    window._htmlBlocks.length = 0;
    window._codeBlocks.length = 0;
  }
  let t = (text||'');
  // html 专用块
  t = t.replace(/```html\r?\n?([\s\S]*?)```/gi, (_, code) => {
    const i = window._htmlBlocks.push(code.trim()) - 1;
    return `@@HBLK${i}@@`;
  });
  // 其他代码块
  t = t.replace(/```(\w*)\r?\n?([\s\S]*?)```/g, (_, lang, code) => {
    const i = window._codeBlocks.push({ lang: lang || 'code', code: code.trim() }) - 1;
    return `@@CBLK${i}@@`;
  });
  // 行内代码
  t = t.replace(/`([^`]+)`/g, (_, code) => {
    const i = window._codeBlocks.push({ lang: '', code: code }) - 1;
    return `@@IBLK${i}@@`;
  });
  // 2) 转义 HTML 并应用 Markdown（不影响已抽取的块）
  //    url/alt/txt 均取自已 esc() 的文本；href/src 再经 encodeURI + esc 双重防护
  t = esc(t)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
      isSafeUrl(url) ? `<img class="mm" src="${esc(url)}" alt="${esc(alt)}" loading="lazy">` : m)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, url) =>
      isSafeHref(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${txt}</a>` : m)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br>');
  // 3) 还原代码块（原始未转义内容）
  t = t.replace(/@@CBLK(\d+)@@/g, (_, i) => {
    const b = window._codeBlocks[i] || {};
    return `<div class="code-block"><div class="code-head">${b.lang}<button class="copy-code" title="复制代码">⧉ 复制</button></div><pre><code>${esc(b.code)}</code></pre></div>`;
  });
  t = t.replace(/@@IBLK(\d+)@@/g, (_, i) => {
    const b = window._codeBlocks[i] || {};
    return `<code>${esc(b.code)}</code>`;
  });
  // 4) 还原 html 块 + 预览按钮
  //    内容内联到按钮 data 属性（而非常驻全局数组），点击时从 dataset 读取
  t = t.replace(/@@HBLK(\d+)@@/g, (_, i) => {
    const code = window._htmlBlocks[i] || '';
    return `<div class="html-block"><pre><code>${esc(code)}</code></pre>
      <div class="hb-bar"><button class="btn-primary" style="padding:5px 14px;font-size:.8em;border-radius:6px" data-hblk="${esc(encodeURIComponent(code))}" onclick="previewHtmlData(this)">🔍 预览页面</button>
      <span style="font-size:.74em;color:var(--text3)">在安全沙箱中渲染</span></div></div>`;
  });
  // 修复内存泄漏：块内容已内联到 DOM（代码块入 <pre>、html 块入 data 属性），
  // 全局数组不再需要留存 → 每次渲染后清空，避免长会话无限增长。
  window._htmlBlocks.length = 0;
  window._codeBlocks.length = 0;
  return t;
}

// ═══════ Settings Modal ═══════
async function showModal(name, tab) {
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  overlay.classList.add('show');
  if (name === 'settings') {
    if (tab === 'model') {
      content.innerHTML = await renderModelTab();
      const prov = document.getElementById('cfg-provider');
      if (prov) renderModelChips(prov.value);
    }
    else if (tab === 'apikeys') content.innerHTML = await renderApiKeyTab();
    else if (tab === 'general') {
      content.innerHTML = await renderGeneralTab();
      loadAutopilotToggles();  // 自主闭环开关（异步填充）
    }
  }
}
function closeModal() { document.getElementById('settings-modal').classList.remove('show'); }

function tabBar(active) {
  const t = [['model','🖥 模型'],['apikeys','🔑 API Keys'],['general','⚙ 通用']];
  return `<div class="tabs">${t.map(([k,l])=>`<button class="${k===active?'active':''}" onclick="showModal('settings','${k}')">${l}</button>`).join('')}</div>`;
}

=======
// ── Send ──
async function sendMessage() {
  if (running) return;
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  const images = _pendingImages.slice();
  if (!text && !images.length) return;

  input.value = ''; input.style.height = 'auto';
  // 若当前在某个面板视图，先切回该模式的对话区
  if (currentView !== 'chat') { await showConversation(currentMode); }
  appendMessage('user', text, images);
  clearAttachments();
  _taskMode = currentMode;  // 记录任务所属模式
  setRunning(true);

  // 优先走 WebSocket（流式 + 可中断），否则回退到 REST
  if (ws && ws.readyState === WebSocket.OPEN) {
    appendTyping();
    ws.send(JSON.stringify({ action: 'run', task: text, interaction: currentMode, images, session_id: SID }));
    return;
  }
  await sendViaRest(text, images);
}

async function sendViaRest(text, images) {
  const taskMode = _taskMode || currentMode;
  appendTyping();
  try {
    const r = await fetch(`${API}/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ task: text, interaction: taskMode, images, session_id: SID }),
    });
    const data = await r.json();
    removeTyping(); setRunning(false);
    if (data.error) {
      routeResultToMode(taskMode, 'message', { role: 'agent', text: `❌ **错误**: ${data.error}` });
    } else {
      routeResultToMode(taskMode, 'result', data);
      updateStats(data);
      if (data.plan) updatePlanView(data.plan);
      refreshAuditMini(); refreshHtmlFiles(); loadStatus();
    }
  } catch(e) {
    removeTyping(); setRunning(false);
    routeResultToMode(taskMode, 'message', { role: 'agent', text: `❌ 请求失败: ${e.message}` });
  }
}
function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}
function setRunning(on) {
  running = on;
  document.getElementById('send-btn').style.display = on ? 'none' : 'flex';
  document.getElementById('stop-btn').style.display = on ? 'flex' : 'none';
  document.getElementById('user-input').disabled = on;
  updateStatus(on ? 'running' : 'connected');
  if (!on) { captureTranscript(); _taskMode = null; }
}
function stopTask() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'stop' }));
    toast('正在中断任务...', 'info');
  } else {
    setRunning(false); removeTyping();
    toast('已停止等待（后台任务可能仍在运行）', 'info');
  }
}

// ── 图片附件（多模态输入）──
let _pendingImages = [];
function attachImage() { document.getElementById('img-input').click(); }
function onImagesPicked(e) {
  const files = Array.from(e.target.files || []);
  files.forEach(f => {
    if (!f.type.startsWith('image/')) return;
    if (f.size > 8*1024*1024) { toast('图片不能超过 8MB', 'error'); return; }
    const reader = new FileReader();
    reader.onload = ev => { _pendingImages.push(ev.target.result); renderAttachments(); };
    reader.readAsDataURL(f);
  });
  e.target.value = '';
}
function renderAttachments() {
  const strip = document.getElementById('attach-strip');
  strip.innerHTML = _pendingImages.map((u, i) =>
    `<div class="thumb"><img src="${u}"><button class="rm" onclick="removeAttachment(${i})">✕</button></div>`).join('');
}
function removeAttachment(i) { _pendingImages.splice(i, 1); renderAttachments(); }
function clearAttachments() { _pendingImages = []; renderAttachments(); }

// ── 语音输入（Web Speech API）──
let _recognition = null, _recognizing = false;
function toggleVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return toast('当前浏览器不支持语音识别，请使用 Chrome 或 Edge', 'error');
  if (_recognizing) { _recognition && _recognition.stop(); return; }
  _recognition = new SR();
  _recognition.lang = 'zh-CN';
  _recognition.interimResults = true;
  _recognition.continuous = false;
  const input = document.getElementById('user-input');
  const base = input.value;
  _recognition.onstart = () => { _recognizing = true; document.getElementById('mic-btn').classList.add('recording'); toast('正在聆听...', 'info'); };
  _recognition.onerror = (ev) => { toast('语音识别失败: ' + ev.error, 'error'); };
  _recognition.onend = () => { _recognizing = false; document.getElementById('mic-btn').classList.remove('recording'); };
  _recognition.onresult = (ev) => {
    let txt = '';
    for (let i = 0; i < ev.results.length; i++) txt += ev.results[i][0].transcript;
    input.value = (base ? base + ' ' : '') + txt;
    input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  };
  _recognition.start();
}

// ── HTML 预览 ──
let _previewNewTabHtml = '';
function previewHtml(i) {
  const html = (window._htmlBlocks && window._htmlBlocks[i]) || '';
  openPreview(html, '内联 HTML');
}
function previewHtmlData(btn) {
  let html = '';
  try { html = decodeURIComponent(btn.getAttribute('data-hblk') || ''); } catch(_) {}
  openPreview(html, '内联 HTML');
}
function openPreview(html, label) {
  _previewNewTabHtml = html;
  document.getElementById('preview-frame').srcdoc = html;
  document.getElementById('preview-path').textContent = label || '';
  document.getElementById('preview-overlay').classList.add('show');
}
async function previewFile(path) {
  document.getElementById('preview-frame').removeAttribute('srcdoc');
  document.getElementById('preview-frame').src = `${API}/preview/file?path=${encodeURIComponent(path)}`;
  document.getElementById('preview-path').textContent = path;
  _previewNewTabHtml = '';
  document.getElementById('preview-overlay').classList.add('show');
}
function closePreview() {
  document.getElementById('preview-overlay').classList.remove('show');
  const f = document.getElementById('preview-frame');
  f.removeAttribute('srcdoc'); f.src = 'about:blank';
}
function openPreviewNewTab() {
  const f = document.getElementById('preview-frame');
  if (_previewNewTabHtml) {
    const blob = new Blob([_previewNewTabHtml], {type:'text/html'});
    window.open(URL.createObjectURL(blob), '_blank');
  } else if (f.src && f.src !== 'about:blank') {
    window.open(f.src, '_blank');
  }
}

// ── Token 用量 ──
async function refreshTokens() {
  try {
    const t = await (await fetch(`${API}/tokens`)).json();
    document.getElementById('tok-prompt').textContent = (t.prompt||0).toLocaleString();
    document.getElementById('tok-completion').textContent = (t.completion||0).toLocaleString();
    document.getElementById('tok-total').textContent = (t.total||0).toLocaleString();
    document.getElementById('tok-tasks').textContent = t.tasks||0;
  } catch(e) {}
}
async function resetTokens() { await fetch(`${API}/tokens`, {method:'DELETE'}); refreshTokens(); toast('Token 统计已重置', 'info'); }

async function refreshHtmlFiles() {
  try {
    const files = await (await fetch(`${API}/files/html`)).json();
    const el = document.getElementById('html-files');
    if (!el) return;
    if (!files.length) { el.innerHTML = '<em style="color:var(--text3)">项目中暂无 HTML 文件</em>'; return; }
    el.innerHTML = files.slice(0,8).map(f =>
      `<div style="display:flex;align-items:center;gap:6px;padding:4px 0;cursor:pointer" onclick="previewFile('${jsq(f.path)}')"
        onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--text2)'">
        🔍 <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.path)}</span></div>`).join('');
  } catch(e) {}
}

// ── Messages ──
function appendMessage(role, content, images) {
  // 委托统一构造器（buildMessageEl 定义于 core.js），仅负责挂载与滚动
  const msgs = document.getElementById('messages');
  msgs.appendChild(buildMessageEl(role, content, images));
  msgs.scrollTop = msgs.scrollHeight;
}
function appendResult(data) {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent';
  let meta = [];
  if (data.interaction && data.interaction !== 'chat') {
    if (data.steps) meta.push(`${data.steps}步`);
    if (data.backtracks) meta.push(`${data.backtracks}回溯`);
  }
  if (data.tokens) meta.push(`🪙 ${data.tokens}tk (${data.prompt_tokens||0}↑/${data.completion_tokens||0}↓)`);
  if (data.duration_ms) meta.push(`${data.duration_ms}ms`);
  const metaStr = meta.length ? meta.join(' · ') : new Date().toLocaleTimeString();
  let output = data.output || '任务完成';
  div.innerHTML = `
    <div class="avatar">AM</div>
    <div class="col">
      <div class="bubble"><button class="copy-msg" title="复制此条">⧉</button>${formatContent(output)}</div>
      <div class="time">${metaStr}</div>
    </div>`;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
  refreshTokens();
}
function appendTyping() {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent'; div.id = 'typing-msg';
  div.innerHTML = `<div class="avatar">AM</div><div class="col"><div class="bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>`;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
}
function removeTyping() { const el = document.getElementById('typing-msg'); if (el) el.remove(); }

function updateStats(data) {
  document.getElementById('stat-steps').textContent = data.steps || 0;
  document.getElementById('stat-backtracks').textContent = data.backtracks || 0;
  document.getElementById('stat-tokens').textContent = data.tokens || 0;
  document.getElementById('stat-duration').textContent = (data.duration_ms || 0) + 'ms';
}
function updatePlanView(plan) {
  const el = document.getElementById('plan-view');
  if (!plan || !plan.root_goal) { el.innerHTML = '<em style="color:var(--text3)">无计划数据</em>'; return; }
  el.innerHTML = `<div class="plan-tree">${renderGoal(plan.root_goal, '')}</div>`;
}
function renderGoal(g, indent) {
  const icons = { pending:'○', in_progress:'◐', completed:'✓', failed:'✗', blocked:'⊘', reverted:'↺' };
  const cls = { pending:'pending', in_progress:'running', completed:'done', failed:'fail', blocked:'pending', reverted:'fail' };
  let html = `<div class="node ${cls[g.status]||''}">${indent}${icons[g.status]||'?'} ${esc(g.description)}`;
  if (g.action) html += ` <span style="color:var(--text3)">[${esc(g.action)}]</span>`;
  html += '</div>';
  if (g.children) g.children.forEach(c => { html += renderGoal(c, indent + '  '); });
  return html;
}
// HTML 转义 — 覆盖文本上下文与双引号属性上下文（& < > " ' `）
const _ESC_MAP = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'};
function esc(t){ return (t==null?'':String(t)).replace(/[&<>"'`]/g, c => _ESC_MAP[c]); }
// JS 字符串-in-HTML属性 转义 — 用于内联事件处理器里 [fn 单引号参数] 的插值，
// 同时防 JS 串逃逸（\ '）与 HTML 属性逃逸（" < > &），彻底堵住双上下文注入。
function jsq(t){
  return String(t==null?'':t)
    .replace(/\\/g,'\\\\').replace(/'/g,"\\'")
    .replace(/&/g,'&amp;').replace(/"/g,'&quot;')
    .replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\r/g,'').replace(/\n/g,'\\n');
}
// 图片可接受 http(s) 与 data:image/*（内联多模态缩略图）
function isSafeUrl(u){ return /^(https?:\/\/|data:image\/)/i.test(u||''); }
// 链接仅接受 http(s)：禁止 data:/javascript: —— data:image/svg 在 <a> 点击后会执行脚本
function isSafeHref(u){ return /^https?:\/\//i.test(u||''); }
function formatContent(text, resetArrays) {
  // 1) 先抽取所有代码块（避免内容被 esc() 二次转义）
  // 流式渲染时每帧全量替换旧内容 → 旧索引可安全清理；非流式追加消息 → 旧索引需保留
  window._htmlBlocks = window._htmlBlocks || [];
  window._codeBlocks = window._codeBlocks || [];
  if (resetArrays) {
    window._htmlBlocks.length = 0;
    window._codeBlocks.length = 0;
  }
  let t = (text||'');
  // html 专用块
  t = t.replace(/```html\r?\n?([\s\S]*?)```/gi, (_, code) => {
    const i = window._htmlBlocks.push(code.trim()) - 1;
    return `@@HBLK${i}@@`;
  });
  // 其他代码块
  t = t.replace(/```(\w*)\r?\n?([\s\S]*?)```/g, (_, lang, code) => {
    const i = window._codeBlocks.push({ lang: lang || 'code', code: code.trim() }) - 1;
    return `@@CBLK${i}@@`;
  });
  // 行内代码
  t = t.replace(/`([^`]+)`/g, (_, code) => {
    const i = window._codeBlocks.push({ lang: '', code: code }) - 1;
    return `@@IBLK${i}@@`;
  });
  // 2) 转义 HTML 并应用 Markdown（不影响已抽取的块）
  //    url/alt/txt 均取自已 esc() 的文本；href/src 再经 encodeURI + esc 双重防护
  t = esc(t)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
      isSafeUrl(url) ? `<img class="mm" src="${esc(url)}" alt="${esc(alt)}" loading="lazy">` : m)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, url) =>
      isSafeHref(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${txt}</a>` : m)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br>');
  // 3) 还原代码块（原始未转义内容）
  t = t.replace(/@@CBLK(\d+)@@/g, (_, i) => {
    const b = window._codeBlocks[i] || {};
    return `<div class="code-block"><div class="code-head">${b.lang}<button class="copy-code" title="复制代码">⧉ 复制</button></div><pre><code>${esc(b.code)}</code></pre></div>`;
  });
  t = t.replace(/@@IBLK(\d+)@@/g, (_, i) => {
    const b = window._codeBlocks[i] || {};
    return `<code>${esc(b.code)}</code>`;
  });
  // 4) 还原 html 块 + 预览按钮
  //    内容内联到按钮 data 属性（而非常驻全局数组），点击时从 dataset 读取
  t = t.replace(/@@HBLK(\d+)@@/g, (_, i) => {
    const code = window._htmlBlocks[i] || '';
    return `<div class="html-block"><pre><code>${esc(code)}</code></pre>
      <div class="hb-bar"><button class="btn-primary" style="padding:5px 14px;font-size:.8em;border-radius:6px" data-hblk="${esc(encodeURIComponent(code))}" onclick="previewHtmlData(this)">🔍 预览页面</button>
      <span style="font-size:.74em;color:var(--text3)">在安全沙箱中渲染</span></div></div>`;
  });
  // 修复内存泄漏：块内容已内联到 DOM（代码块入 <pre>、html 块入 data 属性），
  // 全局数组不再需要留存 → 每次渲染后清空，避免长会话无限增长。
  window._htmlBlocks.length = 0;
  window._codeBlocks.length = 0;
  return t;
}

// ═══════ Settings Modal ═══════
async function showModal(name, tab) {
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  overlay.classList.add('show');
  if (name === 'settings') {
    if (tab === 'model') {
      content.innerHTML = await renderModelTab();
      const prov = document.getElementById('cfg-provider');
      if (prov) renderModelChips(prov.value);
    }
    else if (tab === 'apikeys') content.innerHTML = await renderApiKeyTab();
    else if (tab === 'general') {
      content.innerHTML = await renderGeneralTab();
      loadAutopilotToggles();  // 自主闭环开关（异步填充）
    }
  }
}
function closeModal() { document.getElementById('settings-modal').classList.remove('show'); }

function tabBar(active) {
  const t = [['model','🖥 模型'],['apikeys','🔑 API Keys'],['general','⚙ 通用']];
  return `<div class="tabs">${t.map(([k,l])=>`<button class="${k===active?'active':''}" onclick="showModal('settings','${k}')">${l}</button>`).join('')}</div>`;
}

>>>>>>> f7b98f9b6ecabf8d800f9c0521948f7f5db79dbc
