<<<<<<< HEAD
// ── Audit ──
async function loadAuditView() {
  const a = await (await fetch(`${API}/audit`)).json();
  const s = a.summary;
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>🛡️ 安全审计日志</b>
  <button onclick="clearAudit()" class="btn-danger" style="float:right;padding:4px 10px;border-radius:6px;font-size:.78em">清空</button>
  <div class="stat-grid" style="margin:12px 0">
    <div class="stat-item"><div class="label">总调用</div><div class="value">${s.total}</div></div>
    <div class="stat-item"><div class="label">放行</div><div class="value ok">${s.allow}</div></div>
    <div class="stat-item"><div class="label">需确认</div><div class="value warn">${s.ask_user}</div></div>
    <div class="stat-item"><div class="label">高危操作</div><div class="value" style="color:var(--red)">${s.dangerous}</div></div>
  </div>
  ${a.entries.length === 0 ? '<em style="color:var(--text3)">暂无工具调用记录。在工作/编程模式执行任务后，这里会记录每次操作的风险评估与授权决策。</em>' :
    a.entries.map(e => `
    <div class="card ${e.tier==='dangerous'?'lt-red':e.tier==='sensitive'?'lt-yellow':'lt-green'}">
      <span style="font-family:var(--mono)">${e.time}</span>
      <b style="margin-left:8px">${esc(e.tool)}</b>
      <span class="tag ${e.tier}">${e.tier}</span>
      <span style="float:right;font-size:.8em;color:${e.decision==='allow'?'var(--green)':'var(--yellow)'}">${e.decision} · 风险${e.risk}</span>
      <div style="font-size:.8em;color:var(--text2);margin-top:4px">${esc(e.reason)}</div>
      ${Object.keys(e.params).length?`<div style="font-size:.76em;color:var(--text3);margin-top:3px;font-family:var(--mono)">${esc(JSON.stringify(e.params))}</div>`:''}
    </div>`).join('')}
</div></div></div>`;
}
async function refreshAuditMini() {
  try {
    const a = await (await fetch(`${API}/audit?limit=5`)).json();
    const el = document.getElementById('audit-mini');
    if (!a.entries.length) { el.innerHTML = '<em style="color:var(--text3)">暂无工具调用记录</em>'; return; }
    el.innerHTML = `
      <div style="margin-bottom:8px">放行 <b style="color:var(--green)">${a.summary.allow}</b> ·
        需确认 <b style="color:var(--yellow)">${a.summary.ask_user}</b> ·
        高危 <b style="color:var(--red)">${a.summary.dangerous}</b></div>
      ${a.entries.slice(0,5).map(e=>`<div style="font-size:.78em;margin:3px 0;color:${e.tier==='dangerous'?'var(--red)':'var(--text2)'}">
        <span style="font-family:var(--mono)">${e.time}</span> ${esc(e.tool)}
        <span class="tag ${e.tier}" style="font-size:.68em">${e.tier}</span></div>`).join('')}`;
  } catch(e) {}
}
async function clearAudit() { await fetch(`${API}/audit`, {method:'DELETE'}); loadAuditView(); refreshAuditMini(); toast('审计日志已清空','info'); }

// ── Clear / History ──
async function handleClear() {
  // 仅清空当前模式的会话；对话模式同时清服务端历史
  if (currentMode === 'chat') {
    await fetch(`${API}/chat/history?session_id=${encodeURIComponent(SID)}`, {method:'DELETE'}).catch(()=>{});
  }
  delete modeTranscripts[currentMode];
  delete _pendingResults[currentMode];  // 清除该模式的缓存结果
  persistTranscripts();
  document.getElementById('plan-view').innerHTML = '<em style="color:var(--text3)">对话模式无计划。</em>';
  ['stat-steps','stat-backtracks','stat-tokens'].forEach(id => document.getElementById(id).textContent='0');
  document.getElementById('stat-duration').textContent = '0ms';
  currentView = 'chat';
  document.querySelectorAll('#sidebar nav button').forEach(b=>b.classList.toggle('active', b.dataset.view==='chat'));
  renderWelcome();
  toast(`已清空${MODE_LABELS[currentMode]||''}模式会话`, 'info');
}
async function clearHistory() { await fetch(`${API}/history`, {method:'DELETE'}); loadHistoryView(); toast('历史已清空', 'info'); }
async function delHistory(sid) { await fetch(`${API}/history/${sid}`, {method:'DELETE'}); loadHistoryView(); toast('记录已删除', 'info'); }

// ── Toast ──
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `${type||'info'} show`;
  setTimeout(() => el.className = '', 2500);
}

document.getElementById('settings-modal').addEventListener('click', function(e){ if (e.target === this) closeModal(); });
document.getElementById('user-input').addEventListener('input', function(){
  this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 180) + 'px';
});

// ── 一键复制（代码块 / 整条消息）──
function copyText(text, btn){
  const done = () => { if(btn){ const o=btn.textContent; btn.textContent='✓ 已复制'; setTimeout(()=>btn.textContent=o,1500); } };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(()=>fallbackCopy(text,done));
  else fallbackCopy(text, done);
}
function fallbackCopy(text, done){
  const ta=document.createElement('textarea'); ta.value=text; ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand('copy'); done&&done(); }catch(e){} document.body.removeChild(ta);
}
document.getElementById('messages').addEventListener('click', function(e){
  const codeBtn = e.target.closest('.copy-code');
  if (codeBtn){ const pre=codeBtn.closest('.code-block').querySelector('pre'); copyText(pre.innerText, codeBtn); return; }
  const msgBtn = e.target.closest('.copy-msg');
  if (msgBtn){ const b=msgBtn.closest('.col').querySelector('.bubble'); copyText(b.innerText, msgBtn); return; }
});

// 键盘快捷键（§9.3 子集）：Esc 关弹窗 / Ctrl+. 中断 / Ctrl+L 新会话
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') { closeModal(); }
  else if (e.ctrlKey && e.key === '.') { e.preventDefault(); if (running) stopTask(); }
  else if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) { e.preventDefault(); handleClear(); }
});
=======
// ── Audit ──
async function loadAuditView() {
  const a = await (await fetch(`${API}/audit`)).json();
  const s = a.summary;
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>🛡️ 安全审计日志</b>
  <button onclick="clearAudit()" class="btn-danger" style="float:right;padding:4px 10px;border-radius:6px;font-size:.78em">清空</button>
  <div class="stat-grid" style="margin:12px 0">
    <div class="stat-item"><div class="label">总调用</div><div class="value">${s.total}</div></div>
    <div class="stat-item"><div class="label">放行</div><div class="value ok">${s.allow}</div></div>
    <div class="stat-item"><div class="label">需确认</div><div class="value warn">${s.ask_user}</div></div>
    <div class="stat-item"><div class="label">高危操作</div><div class="value" style="color:var(--red)">${s.dangerous}</div></div>
  </div>
  ${a.entries.length === 0 ? '<em style="color:var(--text3)">暂无工具调用记录。在工作/编程模式执行任务后，这里会记录每次操作的风险评估与授权决策。</em>' :
    a.entries.map(e => `
    <div class="card ${e.tier==='dangerous'?'lt-red':e.tier==='sensitive'?'lt-yellow':'lt-green'}">
      <span style="font-family:var(--mono)">${e.time}</span>
      <b style="margin-left:8px">${esc(e.tool)}</b>
      <span class="tag ${e.tier}">${e.tier}</span>
      <span style="float:right;font-size:.8em;color:${e.decision==='allow'?'var(--green)':'var(--yellow)'}">${e.decision} · 风险${e.risk}</span>
      <div style="font-size:.8em;color:var(--text2);margin-top:4px">${esc(e.reason)}</div>
      ${Object.keys(e.params).length?`<div style="font-size:.76em;color:var(--text3);margin-top:3px;font-family:var(--mono)">${esc(JSON.stringify(e.params))}</div>`:''}
    </div>`).join('')}
</div></div></div>`;
}
async function refreshAuditMini() {
  try {
    const a = await (await fetch(`${API}/audit?limit=5`)).json();
    const el = document.getElementById('audit-mini');
    if (!a.entries.length) { el.innerHTML = '<em style="color:var(--text3)">暂无工具调用记录</em>'; return; }
    el.innerHTML = `
      <div style="margin-bottom:8px">放行 <b style="color:var(--green)">${a.summary.allow}</b> ·
        需确认 <b style="color:var(--yellow)">${a.summary.ask_user}</b> ·
        高危 <b style="color:var(--red)">${a.summary.dangerous}</b></div>
      ${a.entries.slice(0,5).map(e=>`<div style="font-size:.78em;margin:3px 0;color:${e.tier==='dangerous'?'var(--red)':'var(--text2)'}">
        <span style="font-family:var(--mono)">${e.time}</span> ${esc(e.tool)}
        <span class="tag ${e.tier}" style="font-size:.68em">${e.tier}</span></div>`).join('')}`;
  } catch(e) {}
}
async function clearAudit() { await fetch(`${API}/audit`, {method:'DELETE'}); loadAuditView(); refreshAuditMini(); toast('审计日志已清空','info'); }

// ── Clear / History ──
async function handleClear() {
  // 仅清空当前模式的会话；对话模式同时清服务端历史
  if (currentMode === 'chat') {
    await fetch(`${API}/chat/history?session_id=${encodeURIComponent(SID)}`, {method:'DELETE'}).catch(()=>{});
  }
  delete modeTranscripts[currentMode];
  delete _pendingResults[currentMode];  // 清除该模式的缓存结果
  persistTranscripts();
  document.getElementById('plan-view').innerHTML = '<em style="color:var(--text3)">对话模式无计划。</em>';
  ['stat-steps','stat-backtracks','stat-tokens'].forEach(id => document.getElementById(id).textContent='0');
  document.getElementById('stat-duration').textContent = '0ms';
  currentView = 'chat';
  document.querySelectorAll('#sidebar nav button').forEach(b=>b.classList.toggle('active', b.dataset.view==='chat'));
  renderWelcome();
  toast(`已清空${MODE_LABELS[currentMode]||''}模式会话`, 'info');
}
async function clearHistory() { await fetch(`${API}/history`, {method:'DELETE'}); loadHistoryView(); toast('历史已清空', 'info'); }
async function delHistory(sid) { await fetch(`${API}/history/${sid}`, {method:'DELETE'}); loadHistoryView(); toast('记录已删除', 'info'); }

// ── Toast ──
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `${type||'info'} show`;
  setTimeout(() => el.className = '', 2500);
}

document.getElementById('settings-modal').addEventListener('click', function(e){ if (e.target === this) closeModal(); });
document.getElementById('user-input').addEventListener('input', function(){
  this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 180) + 'px';
});

// ── 一键复制（代码块 / 整条消息）──
function copyText(text, btn){
  const done = () => { if(btn){ const o=btn.textContent; btn.textContent='✓ 已复制'; setTimeout(()=>btn.textContent=o,1500); } };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(()=>fallbackCopy(text,done));
  else fallbackCopy(text, done);
}
function fallbackCopy(text, done){
  const ta=document.createElement('textarea'); ta.value=text; ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand('copy'); done&&done(); }catch(e){} document.body.removeChild(ta);
}
document.getElementById('messages').addEventListener('click', function(e){
  const codeBtn = e.target.closest('.copy-code');
  if (codeBtn){ const pre=codeBtn.closest('.code-block').querySelector('pre'); copyText(pre.innerText, codeBtn); return; }
  const msgBtn = e.target.closest('.copy-msg');
  if (msgBtn){ const b=msgBtn.closest('.col').querySelector('.bubble'); copyText(b.innerText, msgBtn); return; }
});

// 键盘快捷键（§9.3 子集）：Esc 关弹窗 / Ctrl+. 中断 / Ctrl+L 新会话
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') { closeModal(); }
  else if (e.ctrlKey && e.key === '.') { e.preventDefault(); if (running) stopTask(); }
  else if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) { e.preventDefault(); handleClear(); }
});
>>>>>>> f7b98f9b6ecabf8d800f9c0521948f7f5db79dbc
