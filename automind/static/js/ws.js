// ── WebSocket (status only) ──
let _wsRetry = 0, _wsTimer = null;
function connectWS() {
  if (_wsTimer) { clearTimeout(_wsTimer); _wsTimer = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
  catch(_) { return scheduleReconnect(); }
  ws.onopen = () => { _wsRetry = 0; updateStatus('connected'); };
  ws.onclose = () => { updateStatus('disconnected'); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch(_){} };
  ws.onmessage = e => { try { handleWS(JSON.parse(e.data)); } catch(_){} };
}
// 指数退避 + 抖动，封顶 30s，避免断线时对服务端造成重连风暴
function scheduleReconnect() {
  if (_wsTimer) return;  // 已排程，去重
  const base = Math.min(30000, 1000 * Math.pow(2, _wsRetry++));
  const delay = base * (0.5 + Math.random() * 0.5);
  _wsTimer = setTimeout(() => { _wsTimer = null; connectWS(); }, delay);
}
function updateStatus(s) {
  const b = document.getElementById('status-badge');
  if (s === 'connected') { b.textContent = '● 已连接'; b.className = 'badge badge-ok'; }
  else if (s === 'running') { b.textContent = '◉ 执行中'; b.className = 'badge badge-warn'; }
  else { b.textContent = '○ 未连接'; b.className = 'badge'; }
}
let _streamEl = null, _streamBuf = '';
function handleWS(data) {
  const taskMode = _taskMode || data.interaction || currentMode;
  switch (data.type) {
    case 'task_start':
      removeTyping();
      _taskMode = taskMode;  // 确保 taskMode 被记录
      if (data.interaction === 'chat') startStreamBubble();
      else if (data.interaction === 'multi') startMultiPanel();
      else if (data.interaction === 'loop') startLoopPanel();
      else startExecPanel();
      break;
    case 'ma_plan': maRenderPlan(data.plan); break;
    case 'ma_step_start': maStepStart(data); break;
    case 'ma_step_end': maStepEnd(data); break;
    case 'ma_done': break;
    case 'loop_iter_start': loopIterStart(data); break;
    case 'loop_action': loopAction(data); break;
    case 'loop_observation': loopObservation(data); break;
    case 'loop_done': break;
    case 'approval_request': showApprovalDialog(data); break;
    // ── 执行过程实时展示 ──
    case 'plan_created': execPlanCreated(data); break;
    case 'plan_step_start': execPlanStepStart(data); break;
    case 'plan_step_end': execPlanStepEnd(data); break;
    case 'plan_backtrack': execTrace('↺ 回溯', esc(data.reason), 'warn'); break;
    case 'step_thought': execTrace('🧠 思考' + (data.iter?(' · 第'+data.iter+'轮'):''), formatContent(data.text), 'think'); break;
    case 'step_action': execStepAction(data); break;
    case 'chat_chunk':
      if (!_streamEl) startStreamBubble();
      _streamBuf += data.delta;
      scheduleStreamRender();   // 节流：合并高频 chunk，避免每帧全量重扫（O(n²)→~20fps）
      break;
    case 'chat_done':
      finalizeStream(data);
      updateStats(data);
      setRunning(false);
      refreshTokens();
      break;
    case 'task_complete':
      removeTyping(); finalizeMulti(); finalizeLoop(data); finalizeExec();
      routeResultToMode(taskMode, 'result', data);
      updateStats(data);
      if (data.plan) updatePlanView(data.plan);
      refreshAuditMini(); refreshHtmlFiles();
      setRunning(false);
      break;
    case 'task_error':
      removeTyping(); finalizeStream(null); finalizeExec();
      routeResultToMode(taskMode, 'message', { role: 'agent', text: '❌ **错误**: ' + data.error });
      setRunning(false);
      break;
    case 'task_cancelled':
      finalizeStream(null); removeTyping(); finalizeExec();
      routeResultToMode(taskMode, 'message', { role: 'agent', text: '⏹ 任务已中断' });
      setRunning(false);
      break;
  }
}
// 流式渲染节流：把高频 chunk 合并为最多 ~20fps 的整块重渲染。
// 每个 chunk 仅累加字符串（O(1)），实际 formatContent 全量解析被限频，
// 长回复不再随长度平方级变慢；chat_done 时 finalizeStream 做最终整渲。
let _streamRenderPending = false, _streamLastRender = 0;
const _STREAM_RENDER_MS = 50;
function scheduleStreamRender() {
  if (_streamRenderPending || !_streamEl) return;
  const gap = performance.now() - _streamLastRender;
  if (gap >= _STREAM_RENDER_MS) { _renderStreamNow(); return; }
  _streamRenderPending = true;
  setTimeout(() => { _streamRenderPending = false; _renderStreamNow(); }, _STREAM_RENDER_MS - gap);
}
function _renderStreamNow() {
  if (!_streamEl) return;
  _streamLastRender = performance.now();
  _streamEl.innerHTML = formatContent(_streamBuf, true) + '<span class="cursor">▍</span>';
  const msgs = document.getElementById('messages');
  if (msgs) msgs.scrollTop = 1e9;
}
function startStreamBubble() {
  removeTyping();
  _streamRenderPending = false; _streamLastRender = 0;
  _streamBuf = '';
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent'; div.id = 'stream-msg';
  const timeEl = document.createElement('div'); timeEl.className = 'time';
  const bubbleEl = document.createElement('div'); bubbleEl.className = 'bubble';
  bubbleEl.innerHTML = '<span class="cursor">▍</span>';
  const col = document.createElement('div'); col.className = 'col';
  col.appendChild(bubbleEl); col.appendChild(timeEl);
  div.innerHTML = '<div class="avatar">AM</div>';
  div.appendChild(col);
  msgs.appendChild(div); msgs.scrollTop = 1e9;
  _streamEl = bubbleEl;
  _streamTimeEl = timeEl;
  _streamDiv = div;
}
let _streamTimeEl = null, _streamDiv = null;
function finalizeStream(data) {
  if (_streamEl && data) {
    // 正常完成：渲染最终内容 + 元信息
    _streamEl.innerHTML = formatContent(_streamBuf || '(无回复)', true);
    if (_streamTimeEl) {
      let meta = [];
      if (data.tokens) meta.push(`🪙 ${data.tokens}tk (${data.prompt_tokens||0}↑/${data.completion_tokens||0}↓ · 估算)`);
      if (data.duration_ms) meta.push(`${data.duration_ms}ms`);
      _streamTimeEl.textContent = meta.join(' · ') || new Date().toLocaleTimeString();
    }
  } else if (_streamEl && !data) {
    // 错误/取消：有部分内容则定格渲染，否则移除空气泡（消除残留光标气泡）
    if (_streamBuf.trim()) {
      _streamEl.innerHTML = formatContent(_streamBuf, true);
    } else if (_streamDiv && _streamDiv.parentNode) {
      _streamDiv.remove();
    }
  }
  if (_streamDiv) { _streamDiv.id = ''; }
  _streamEl = null; _streamTimeEl = null; _streamDiv = null; _streamBuf = '';
}

// ── 多智能体协同进度 ──
let _maEl = null;
let _maDiv = null;
function startMultiPanel() {
  removeTyping();
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent'; div.id = 'ma-msg';
  const bodyEl = document.createElement('div'); bodyEl.style.marginTop = '8px';
  bodyEl.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  const bubble = document.createElement('div'); bubble.className = 'bubble';
  bubble.innerHTML = '<b>🤝 多智能体协同</b>';
  bubble.appendChild(bodyEl);
  const col = document.createElement('div'); col.className = 'col'; col.style.maxWidth = '88%';
  col.appendChild(bubble);
  div.innerHTML = '<div class="avatar">🤝</div>';
  div.appendChild(col);
  msgs.appendChild(div); msgs.scrollTop = 1e9;
  _maEl = bodyEl;
  _maDiv = div;
}
function maRenderPlan(plan) {
  if (!_maEl) startMultiPanel();
  _maEl.innerHTML = (plan||[]).map((s,i) =>
    `<div class="ma-step" style="padding:6px 0;border-bottom:1px solid var(--border)">
      <span class="ma-ic">○</span> <b>${esc(({planner:'🧭 规划',researcher:'🔎 研究',coder:'💻 编程',writer:'✍️ 写作',reviewer:'🧐 审阅'})[s.role]||s.role)}</b>
      <span style="color:var(--text2)">${esc(s.subtask)}</span>
      <div class="ma-out" style="display:none;margin-top:4px;font-size:.86em;color:var(--text2);white-space:pre-wrap"></div>
    </div>`).join('');
  document.getElementById('messages').scrollTop = 1e9;
}
function maStepStart(d) {
  if (!_maEl) return;
  const el = _maEl.querySelectorAll('.ma-step')[d.index];
  if (el) { el.querySelector('.ma-ic').textContent = '◐'; el.querySelector('.ma-ic').style.color = 'var(--yellow)'; }
}
function maStepEnd(d) {
  if (!_maEl) return;
  const el = _maEl.querySelectorAll('.ma-step')[d.index];
  if (el) {
    el.querySelector('.ma-ic').textContent = '✓'; el.querySelector('.ma-ic').style.color = 'var(--green)';
    const out = el.querySelector('.ma-out');
    out.style.display = 'block';
    out.innerHTML = formatContent((d.output||'').slice(0, 600) + ((d.output||'').length>600?' …':''));
  }
  document.getElementById('messages').scrollTop = 1e9;
}
function finalizeMulti() { if (_maDiv) { _maDiv.id = ''; _maDiv = null; } _maEl = null; }

// ── 循环工程进度面板 ──
let _loopEl = null;
let _loopDiv = null;
function startLoopPanel() {
  removeTyping();
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent'; div.id = 'loop-msg';
  const bodyEl = document.createElement('div'); bodyEl.style.marginTop = '8px';
  const bubble = document.createElement('div'); bubble.className = 'bubble';
  bubble.innerHTML = '<b>🔁 循环工程（自主迭代）</b>';
  bubble.appendChild(bodyEl);
  const col = document.createElement('div'); col.className = 'col'; col.style.maxWidth = '90%';
  col.appendChild(bubble);
  div.innerHTML = '<div class="avatar">AM</div>';
  div.appendChild(col);
  msgs.appendChild(div); msgs.scrollTop = 1e9;
  _loopEl = bodyEl;
  _loopDiv = div;
}
function loopIterStart(d) {
  if (!_loopEl) startLoopPanel();
  const node = document.createElement('div');
  node.className = 'card'; node.dataset.loopIter = d.iter;
  node.innerHTML = `<b>第 ${d.iter} 轮 / 最多 ${d.max}</b> <span class="cursor">▍</span>
    <div class="loop-act" style="font-size:.84em;color:var(--text2);margin-top:4px"></div>
    <div class="loop-obs" style="font-size:.82em;margin-top:4px"></div>`;
  _loopEl.appendChild(node);
  document.getElementById('messages').scrollTop = 1e9;
}
function loopAction(d) {
  if (!_loopEl) return;
  const n = _loopEl.querySelector(`[data-loop-iter="${d.iter}"]`); if (!n) return;
  const c = n.querySelector('.cursor'); if (c) c.remove();
  n.querySelector('.loop-act').innerHTML = '🛠 ' + formatContent((d.output||'').slice(0,300));
}
function loopObservation(d) {
  if (!_loopEl) return;
  const n = _loopEl.querySelector(`[data-loop-iter="${d.iter}"]`); if (!n) return;
  const obs = n.querySelector('.loop-obs');
  obs.innerHTML = d.done
    ? '<span style="color:var(--green)">✓ 观察：任务已完成</span>'
    : '<span style="color:var(--yellow)">↻ 观察：' + esc((d.reason||'').slice(0,160)) + '</span>';
  if (d.done) n.classList.add('lt-green'); else n.classList.add('lt-yellow');
}
function finalizeLoop(data) {
  if (_loopEl && data && data.stop_reason) {
    const tag = {completed:'✅ 已完成', no_progress:'⏹ 连续无进展，已停止', converged:'🔄 输出已收敛，已停止', idle:'💤 连续多轮未执行操作，已停止', max_iterations:'⛔ 达到最大轮数'}[data.stop_reason] || '';
    if (tag) { const t = document.createElement('div'); t.style.cssText='margin-top:8px;font-weight:600'; t.textContent = tag; _loopEl.appendChild(t); }
  }
  if (_loopDiv) { _loopDiv.id = ''; _loopDiv = null; }
  _loopEl = null;
}

// ── 执行过程实时面板（工作/编程模式）──
let _execEl = null;
let _execDiv = null;
let _execTypingEl = null;
function startExecPanel() {
  removeTyping();
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent'; div.id = 'exec-msg';
  const bodyEl = document.createElement('div'); bodyEl.className = 'exec-trace'; bodyEl.style.marginTop = '8px';
  const typingEl = document.createElement('div'); typingEl.className = 'typing-dots'; typingEl.style.marginTop = '6px';
  typingEl.innerHTML = '<span></span><span></span><span></span>';
  const bubble = document.createElement('div'); bubble.className = 'bubble';
  bubble.innerHTML = '<b>⚙️ 执行过程</b>';
  bubble.appendChild(bodyEl);
  bubble.appendChild(typingEl);
  const col = document.createElement('div'); col.className = 'col'; col.style.maxWidth = '92%';
  col.appendChild(bubble);
  div.innerHTML = '<div class="avatar">AM</div>';
  div.appendChild(col);
  msgs.appendChild(div); msgs.scrollTop = 1e9;
  _execEl = bodyEl;
  _execDiv = div;
  _execTypingEl = typingEl;
}
function traceTarget() { return _loopEl || _execEl; }
function execTrace(label, html, kind) {
  const el = traceTarget(); if (!el) return;
  const node = document.createElement('div');
  node.className = 'trace-item trace-' + (kind||'info');
  node.innerHTML = `<div class="trace-label">${label}</div><div class="trace-body">${html}</div>`;
  el.appendChild(node);
  document.getElementById('messages').scrollTop = 1e9;
}
function execStepAction(d) {
  const args = Object.keys(d.args||{}).length ? `<div class="trace-args">${esc(JSON.stringify(d.args))}</div>` : '';
  const out = d.output ? `<div class="trace-out ${d.success?'':'fail'}">${d.success?'→ ':'✗ '}${esc(String(d.output).slice(0,400))}</div>` : '';
  execTrace((d.success?'🛠 ':'⚠ ') + '调用 ' + esc(d.tool), args + out, d.success?'action':'warn');
}
function execPlanCreated(d) {
  const el = traceTarget(); if (!el || !(d.steps||[]).length) return;
  const steps = d.steps.map((s,i)=>`<div class="plan-row"><span class="ps-icon">○</span> ${i+1}. ${esc(s.description)}${s.tool?` <span style="color:var(--text3)">[${esc(s.tool)}]</span>`:''}</div>`).join('');
  execTrace('📋 已生成计划（' + d.steps.length + ' 步）', `<div class="plan-rows">${steps}</div>`, 'plan');
}
function execPlanStepStart(d) {
  // 在当前 exec 面板中通过 DOM 查找对应计划行
  const el = _execEl; if (!el) return;
  const rows = el.querySelectorAll('.plan-row');
  if (rows[d.index]) rows[d.index].querySelector('.ps-icon').textContent = '◐';
}
function execPlanStepEnd(d) {
  const el = _execEl; if (!el) return;
  const rows = el.querySelectorAll('.plan-row');
  if (rows[d.index]) {
    const ic = rows[d.index].querySelector('.ps-icon');
    ic.textContent = d.success ? '✓' : '✗';
    rows[d.index].style.color = d.success ? 'var(--green)' : 'var(--red)';
    if (!d.success && d.error) rows[d.index].title = d.error;
  }
}
function finalizeExec() {
  if (_execTypingEl) { _execTypingEl.remove(); _execTypingEl = null; }
  if (_execDiv) { _execDiv.id = ''; _execDiv = null; }
  _execEl = null;
}

// ── 审批对话框 ──
function showApprovalDialog(d) {
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  const params = Object.entries(d.params||{}).map(([k,v])=>`<div style="font-family:var(--mono);font-size:.8em;color:var(--text3)">${esc(k)} = ${esc(v)}</div>`).join('');
  content.innerHTML = `
<h2>🙋 工具调用审批</h2>
<div class="card lt-yellow" style="margin:12px 0">
  <b>${esc(d.tool)}</b> <span class="tag ${d.tier}">${d.tier}</span>
  <div style="font-size:.85em;color:var(--text2);margin-top:6px">${esc(d.reason||'')}</div>
  ${params?`<div style="margin-top:8px">${params}</div>`:''}
</div>
<div class="btn-row">
  <button class="btn-danger" onclick="respondApproval('${jsq(d.approval_id)}',false)">拒绝</button>
  <button class="btn-primary" onclick="respondApproval('${jsq(d.approval_id)}',true)">批准</button>
</div>`;
  overlay.classList.add('show');
}
function respondApproval(id, approved) {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({action:'approval_response', approval_id:id, approved}));
  closeModal();
  toast(approved ? '已批准' : '已拒绝', approved?'success':'info');
}

