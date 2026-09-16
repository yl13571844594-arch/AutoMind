// ── Send ──
async function sendMessage() {
  // 有任务在跑 → 这句话是「补充要求」，插进正在跑的那一轮；
  // 没有 → 正常新任务。用户不必先按停止再重发。
  if (running) return interjectMessage();
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
  window._lastTask = { text, mode: currentMode };  // 供「任务中断继续」使用
  setRunning(true);

  // 优先走 WebSocket（流式 + 可中断），否则回退到 REST
  if (ws && ws.readyState === WebSocket.OPEN) {
    appendTyping();
    ws.send(JSON.stringify({ action: 'run', task: text, interaction: currentMode, images, session_id: chatSid() }));
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
      body: JSON.stringify({ task: text, interaction: taskMode, images, session_id: chatSid() }),
    });
    const data = await r.json();
    removeTyping(); setRunning(false);
    if (data.error) {
      routeResultToMode(taskMode, 'message', { role: 'agent', text: `❌ **错误**: ${data.error}` });
      offerResume(taskMode, '出错');
    } else {
      routeResultToMode(taskMode, 'result', data);
      updateStats(data);
      if (data.plan) updatePlanView(data.plan);
      refreshAuditMini(); refreshHtmlFiles(); refreshChanges(); loadStatus();
      window._lastTask = null;
    }
  } catch(e) {
    removeTyping(); setRunning(false);
    routeResultToMode(taskMode, 'message', { role: 'agent', text: `❌ 请求失败: ${e.message}` });
  }
}

// ═══════ 中途插入补充要求（interject）═══════
// 语义：任务执行期间用户补一句话，服务端把它排进**当前这一轮**，绝不取消任务。
// 因此这里只负责「把话送出去 + 如实标记这句话的命运」，不碰任何取消逻辑。
// 插入气泡走的是独立路径（不走 sendMessage），所以每个请求登记一条 pending 记录，
// 用 ref 回填状态：靠文本匹配会在用户连发两条相同内容时改错气泡。
// 请求带 ref，服务端在每条回执里原样回显（interjection_applied 是逐 item 带 ref），
// 于是定位是**确定查表**；只有回执没带 ref（老服务端/降级）才退回文本 FIFO 匹配。
const _interjectPending = new Map();   // ref → { el, mode, sid, text, seq, timer }
const _interjectTimers = new Set();
const _INTERJECT_ACK_MS = 15000;       // 静默兜底：超时未回执就不再假装「处理中」
let _interjectSeq = 0;                 // 同毫秒插入的排序依据（见 _takeInterject）
// 生成请求对账标识。mode/session/文本 前缀纯为排障时好认（拼完不回解析），
// 真正保证唯一的是时间戳 + 单调序号 —— 同一毫秒连发两条也各有各的 ref。
function _newInterjectKey(mode, sid, text) {
  return mode + '::' + sid + '::' + Date.now() + '::' + (_interjectSeq++) + '::' + text;
}
// 让气泡上那个小标记显示当前真实状态。
// text 是显式文案（各状态固定一句，直接写死比再套一层映射表清楚）；
// title 是鼠标悬停说明，可能来自服务端 reason，只走 textContent/属性赋值，不进 innerHTML。
function _markInterject(el, cls, text, title) {
  const tag = el && el.querySelector('.interject-tag');
  if (!tag) return;
  tag.className = 'interject-tag ' + cls;
  tag.textContent = text;
  if (title) tag.title = title;
}
async function interjectMessage() {
  if (!running) return sendMessage();   // 任务刚好结束 → 退回普通发送
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  if (!text) return;
  // 会话与模式在**发送这一刻**定死：回执回来时用户可能已经切了会话，
  // 那时再读 chatSid()/currentMode 就会去改另一个会话里的气泡
  const sid = chatSid(), mode = currentMode;
  const ref = _newInterjectKey(mode, sid, text);   // 兼作 pending 表的键与请求里的对账标识
  const el = appendMessage('user', text, [], `<span class="interject-tag it-pending">补充 · 待纳入</span>`);
  const rec = { el, mode, sid, text, seq: _interjectSeq, timer: null };
  _interjectPending.set(ref, rec);
  input.value = ''; input.style.height = 'auto';
  // 清空后顺手把「继续补充」的提示写回去（模式可能刚切过，这里以当前态为准）
  input.placeholder = syncInputAffordance();

  // 任务进行中插入的是「补充要求」，不是新对话记录 → 不更新 _lastTask
  //（否则「任务中断继续」会去续跑这句半截话）
  const send = () => ws.send(JSON.stringify({
    action: 'interject', text, session_id: sid, interaction: mode, ref,
  }));
  if (ws && ws.readyState === WebSocket.OPEN) send();
  else {  // 无 WS 时服务端收不到这条 —— 如实说，别让用户以为插进去了
    _dropInterjectLocal(ref, '连接已断开，这次补充没有送出去（任务未受影响）');
    return;
  }
  // 兜底：回执可能一直不来（后端版本不支持 interject、连接中途断掉）。
  // 那就把标记留在「待纳入」会一直骗人，所以超时后改成明确的未确认态。
  rec.timer = setTimeout(() => {
    _interjectTimers.delete(rec.timer);
    if (!_interjectPending.has(ref)) return;
    _interjectPending.delete(ref);
    _markInterject(rec.el, 'it-dropped', '补充 · 未确认', '长时间未收到服务端回执，无法确认这条补充是否被纳入本轮');
    toast('这条补充长时间没有收到服务端回执，无法确认是否已纳入本轮', 'error');
  }, _INTERJECT_ACK_MS);
  _interjectTimers.add(rec.timer);
}
// 本地判定失败：清记录、改标记、给出可见反馈（调用方决定文案与 toast）
function _dropInterjectLocal(key, reason) {
  const rec = _interjectPending.get(key);
  if (!rec) return null;
  _interjectPending.delete(key);
  if (rec.timer) { clearTimeout(rec.timer); _interjectTimers.delete(rec.timer); }
  _markInterject(rec.el, 'it-dropped', '补充 · 未纳入', reason || '');
  return rec;
}
// 回执定位。两条分支：
//   ① ref 命中 → 直接查表定位（唯一且确定，同文连发也不串位）；
//   ② 回执没带 ref（老服务端/降级）→ 退回按「发出时记下的 mode/session + 文本」
//      找**最早**的那条，同毫秒的先后由键里的单调序号兜住。
// 服务端对拿不到的 ref 会填空串，所以 "" 一律当作"没带"。
function _takeInterject(mode, sid, text, ref) {
  // 带了 ref 就只认 ref：查不到（重复回执 / 已被终态兜底清掉）直接返回 null，
  // 不再退化成文本匹配 —— 那会把回执安到另一条无辜的气泡上
  const key = ref ? (_interjectPending.has(ref) ? ref : null)
                  : _takeInterjectKey(mode, sid, text);
  if (!key) return null;
  const rec = _interjectPending.get(key);
  _interjectPending.delete(key);
  if (rec && rec.timer) { clearTimeout(rec.timer); _interjectTimers.delete(rec.timer); }
  return rec || null;
}
// 降级路径：按 mode+session+文本 找最早的一条，返回其键
function _takeInterjectKey(mode, sid, text) {
  const suffix = '::' + text;
  const keys = [];
  _interjectPending.forEach((_, k) => {
    if (k.startsWith(mode + '::' + sid + '::') && k.endsWith(suffix)) keys.push(k);
  });
  if (!keys.length) return null;
  // 先比时间戳，同毫秒再比递增序号 —— 否则同一毫秒发出的两条谁先谁后不确定，
  // 回执就可能落到后一条气泡上
  const rank = k => { const p = k.split('::'); return [parseInt(p[2], 10) || 0, parseInt(p[3], 10) || 0]; };
  keys.sort((a, b) => { const ra = rank(a), rb = rank(b); return ra[0] - rb[0] || ra[1] - rb[1]; });
  return keys[0];
}
// 终态兜底：task_cancelled / task_error / task_complete / chat_done 到达时，
// 把本会话本模式里**仍未确认**的插入气泡显式降级为「未纳入本轮」。
// 为什么必须有：用户点「停止」时服务端的 interjection_dropped 可能来不及发出
//（发送通道先被取消，server.py 里那条回执是 best-effort，它自己也注明要靠前端兜底），
// 少了这一步气泡会永远停在「补充 · 待纳入」—— 用户一直以为自己那句话在生效，
// 正是本功能最坏的失败形态。
// sid 用终态事件自带的 session_id（而不是 chatSid()）：用户中途切了会话时，
// 后者已经变了，拿它过滤会把这些气泡漏下来。
function settleInterjections(reason, sid) {
  const target = sid || chatSid();
  const stale = [];
  _interjectPending.forEach((rec, ref) => {
    // 只收本会话的；模式上只在能确定任务模式时才收窄（多模式并行跑时不误伤）
    if (rec.sid !== target) return;
    if (_taskMode && rec.mode !== _taskMode) return;
    stale.push([ref, rec]);
  });
  stale.forEach(([ref, rec]) => {
    _interjectPending.delete(ref);
    if (rec.timer) { clearTimeout(rec.timer); _interjectTimers.delete(rec.timer); }
    _markInterject(rec.el, 'it-dropped', '补充 · 未纳入', reason);
    // 每条各给一次 toast：要求「含前 40 字」，多条合并成一句就说不清是哪条没进去
    const short = rec.text.slice(0, 40) + (rec.text.length > 40 ? '…' : '');
    toast(`补充未纳入本轮：${reason}${short ? ' ｜ ' + short : ''}`, 'error');
  });
  return stale.length;
}
// 服务端 5 种下行回执的统一入口。事件里带 session_id/mode 就用它（支持多标签页，
// 另一个窗口插入的话不会改错本窗口的气泡），缺省退回当前会话与模式。
// 定位优先用 ref（服务端原样回显），没有 ref 才退回文本 FIFO。
function interjectionRef(data) {
  // 服务端对拿不到的 ref 填空串，等同于"没带"
  return String((data && (data.ref || data.client_ref)) || '');
}
function echoInterjection(kind, data) {
  const text = data.text == null ? '' : String(data.text);
  const sid = data.session_id || chatSid();
  const mode = data.mode || _taskMode || currentMode;
  const ref = interjectionRef(data);
  const short = text.slice(0, 40) + (text.length > 40 ? '…' : '');

  // applied 是"一批"：每条 item 各带自己的 seq/text/applied_at/ref，
  // 必须逐条回填，否则同一次合并里的两条会只有一条被标记
  if (kind === 'applied' && Array.isArray(data.items) && data.items.length) {
    let hit = 0;
    data.items.forEach(it => {
      const rec = _takeInterject(mode, sid, it && it.text != null ? String(it.text) : '',
                                 interjectionRef(it));
      if (!rec) return;
      hit++;
      _markInterject(rec.el, 'it-applied', '补充 · 已纳入本轮',
        '模型已读到这条补充' + (it.applied_at || data.at ? '（' + (it.applied_at || data.at) + '）' : ''));
    });
    toast(hit > 1 ? `已纳入本轮的补充：${hit} 条（模型已经读到）` : '补充已纳入本轮，模型已经读到', 'success');
    return;
  }

  const rec = _takeInterject(mode, sid, text, ref);
  switch (kind) {
    case 'received':
      if (rec) _markInterject(rec.el, 'it-pending', '补充 · 待纳入',
        '已收下，正在排入本轮（还没交给模型）');
      toast('已收下补充，将纳入本轮（正在执行的那一步不会被打断）', 'info');
      break;
    case 'applied':
      // 理论上都被上面的 items 分支接走了；这里是老服务端只回顶层 text 的降级
      if (rec) _markInterject(rec.el, 'it-applied', '补充 · 已纳入本轮',
        '模型已读到这条补充' + (data.at ? '（' + data.at + '）' : ''));
      toast('补充已纳入本轮，模型已经读到', 'success');
      break;
    case 'dropped': {
      // 这句没进本轮 —— 必须显式说清，用户才不会误以为它生效了
      const reason = data.reason || '任务在这条补充被处理前就结束了';
      if (rec) _markInterject(rec.el, 'it-dropped', '补充 · 未纳入', reason);
      toast(`补充未纳入本轮：${reason}${short ? ' ｜ ' + short : ''}`, 'error');
      break;
    }
    case 'promoted':
      // 服务端态度：当时没有任务在跑，已直接当新任务开跑（随后会有 task_start）。
      // 这里必须同步进执行态：否则「插入/停止」按钮还是普通发送态，
      // 之后 task_start～task_complete 的渲染会对不上。
      _taskMode = mode;
      window._lastTask = { text, mode };
      setRunning(true);
      if (rec) _markInterject(rec.el, 'it-applied', '补充 · 已作为新任务',
        '当时没有正在执行的任务，已直接开始');
      toast('当时没有正在执行的任务，已把这条补充作为新任务直接开始', 'info');
      break;
    case 'rejected':
      if (rec) _markInterject(rec.el, 'it-dropped', '补充 · 已被拒绝', data.reason || '');
      toast('插入补充被拒：' + (data.reason || '服务端未接受') + (short ? ' ｜ ' + short : ''), 'error');
      break;
  }
}
function handleKeyDown(e) {
  // 执行中 Ctrl+Enter 走「插入补充」：键盘上也能一边等回答一边补要求，
  // 不必先去够鼠标点按钮
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && running) {
    e.preventDefault(); interjectMessage(); return;
  }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}
function setRunning(on) {
  running = on;
  document.getElementById('send-btn').style.display = on ? 'none' : 'flex';
  document.getElementById('stop-btn').style.display = on ? 'flex' : 'none';
  // 执行期间不再禁用输入框（此前 disabled 把录入整段锁死）：
  // 用户要能边看回答边把补充要求打进插入队列，也要能继续用 Enter 正常发送
  syncInputAffordance();
  updateStatus(on ? 'running' : 'connected');
  if (!on) { captureTranscript(); _taskMode = null; }
}
// 按执行态切换输入区的外观：送出「插入补充」按钮 + 改提示文案。
// 单独成函数，是为了让「模式切换」和「刚插入一条」也能刷新提示 ——
// 否则切换模式后输入框里还挂着上一个模式的提示。
// 返回执行态的提示文案（调用方用完还能回填）。
function syncInputAffordance() {
  const btn = document.getElementById('interject-btn');
  if (btn) btn.style.display = running ? 'flex' : 'none';
  const input = document.getElementById('user-input');
  const hint = running
    ? 'AI 正在回答 — 输入补充要求，Enter 或点「⤴ 插入补充」加入本轮（Shift+Enter 换行）'
    : (MODE_PLACEHOLDER[currentMode || 'chat'] || '输入消息，Enter 发送，Shift+Enter 换行...');
  if (input) input.placeholder = hint;
  return hint;
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
    // 实时成本估算（按当前模型单价，可点击自定义）
    const costEl = document.getElementById('tok-cost');
    if (costEl) {
      const cost = fmtCost(estCost(t.prompt, t.completion));
      costEl.textContent = cost || '设置单价';
      costEl.style.color = cost ? 'var(--yellow)' : 'var(--text3)';
    }
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
function appendMessage(role, content, images, tag) {
  // 委托统一构造器（buildMessageEl 定义于 core.js），仅负责挂载与滚动
  // 返回元素：插入气泡要留句柄改状态，靠文本反查会在连发相同内容时错位
  const msgs = document.getElementById('messages');
  const el = buildMessageEl(role, content, images, tag);
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
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
    .replace(/~~(.+?)~~/g, '<del>$1</del>')
    .replace(/(^|[^*<\w])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>');
  // 2.5) 行级结构渲染：标题 / 列表 / 引用 / 表格 / 分隔线（美化问答展示）
  t = renderMdBlocks(t);
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

// ── 行级 Markdown 块渲染（标题/列表/表格/引用/分隔线）──
// 输入为已 esc() 转义、已做行内替换的文本；本函数只做结构化包装，
// 不引入任何未转义的用户内容，XSS 防线不变。普通行仍以 <br> 连接。
const _MD_TBL_SEP = /^\s*\|?[\s:|-]+\|?\s*$/;
function _mdCells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
}
function renderMdBlocks(text) {
  const lines = String(text).split('\n');
  const out = [];
  let lastWasBlock = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // 表格：|…| 行 + 紧随的分隔行
    if (/^\s*\|.+\|\s*$/.test(line) && i + 1 < lines.length && _MD_TBL_SEP.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      const head = _mdCells(line);
      let j = i + 2;
      const rows = [];
      while (j < lines.length && /^\s*\|.+\|\s*$/.test(lines[j])) { rows.push(_mdCells(lines[j])); j++; }
      out.push('<div class="md-tbl-wrap"><table class="md-table"><thead><tr>'
        + head.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
        + rows.map(r => '<tr>' + head.map((_, k) => `<td>${r[k] != null ? r[k] : ''}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>');
      i = j - 1; lastWasBlock = true; continue;
    }
    // 分隔线（先于无序列表判断，--- 不是列表）
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push('<div class="md-hr"></div>'); lastWasBlock = true; continue; }
    // 标题 # ~ ####
    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if (h) { out.push(`<div class="md-h md-h${h[1].length}">${h[2]}</div>`); lastWasBlock = true; continue; }
    // 引用 >（esc 后为 &gt;）
    if (/^&gt;\s?/.test(line)) {
      const q = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) { q.push(lines[i].replace(/^&gt;\s?/, '')); i++; }
      i--;
      out.push(`<div class="md-quote">${q.join('<br>')}</div>`); lastWasBlock = true; continue;
    }
    // 无序列表 - / •
    if (/^\s*[-•]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-•]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-•]\s+/, '')); i++; }
      i--;
      out.push(`<ul class="md-list">${items.map(x => `<li>${x}</li>`).join('')}</ul>`); lastWasBlock = true; continue;
    }
    // 有序列表 1. / 1、 / 1)
    if (/^\s*\d+[.、)]\s+/.test(line)) {
      const items = [];
      let start = parseInt(line.match(/^\s*(\d+)/)[1], 10) || 1;
      while (i < lines.length && /^\s*\d+[.、)]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+[.、)]\s+/, '')); i++; }
      i--;
      out.push(`<ol class="md-list" start="${start}">${items.map(x => `<li>${x}</li>`).join('')}</ol>`); lastWasBlock = true; continue;
    }
    // 普通行：块元素自带间距，其后的单个空行不再额外 <br>
    if (line === '' && lastWasBlock) { lastWasBlock = false; continue; }
    out.push(line + '<br>');
    lastWasBlock = false;
  }
  // 去掉结尾多余 <br>
  let s = out.join('');
  return s.replace(/(<br>)+$/, '');
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
    else if (tab === 'integrations') content.innerHTML = await renderIntegrationsTab();
  }
}
function closeModal() { document.getElementById('settings-modal').classList.remove('show'); }

// （设置弹窗已按功能单一职责化：入口在左下角「⚙ 设置」菜单，不再需要弹窗内标签栏）

