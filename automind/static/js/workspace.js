// ── 工作区管理 — 每个工作区 = 独立目录 + 独立上下文 ──
// 切换工作区后 Agent 在新目录下重建（记忆/索引/权限根随之切换）；
// 前端会话内容（transcripts）与对话历史（chat session）按工作区隔离，互不污染。

// 工作区上下文后缀：默认工作区为 ''（兼容既有历史），命名工作区为 '_w<hash>'
window.WS_SUFFIX = localStorage.getItem('automind_ws_suffix') || '';
window.WS_ACTIVE = localStorage.getItem('automind_ws_active') || '';

function _wsHash(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
  return Math.abs(h).toString(36).slice(0, 8);
}

async function showWorkspaces() {
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  overlay.classList.add('show');
  content.innerHTML = '<h2>🗂 工作区</h2><div class="hint">加载中…</div>';
  let data = { workspaces: [], active: '' };
  try { data = await (await fetch(`${API}/workspaces`)).json(); } catch (e) {}
  const rows = (data.workspaces || []).map(w => {
    const isActive = window.WS_ACTIVE === w.name;
    return `<div class="card ${isActive ? 'lt-green' : ''}" style="display:flex;align-items:center;gap:10px">
      <div style="flex:1;min-width:0">
        <b>${isActive ? '● ' : ''}${esc(w.name)}</b>
        <div style="font-size:.76em;color:var(--text3);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(w.path)}">${esc(w.path)}</div>
      </div>
      ${isActive ? '<span class="tag safe">当前</span>'
        : `<button class="btn-primary" style="padding:4px 12px;font-size:.78em;border-radius:6px" onclick="switchWorkspace('${jsq(w.name)}')">切换</button>`}
      <button class="btn-danger" style="padding:4px 10px;font-size:.78em;border-radius:6px" onclick="deleteWorkspace('${jsq(w.name)}')">删除</button>
    </div>`;
  }).join('');
  content.innerHTML = `
<h2>🗂 工作区管理</h2>
<div class="hint">每个工作区 = 独立目录 + 独立上下文。切换后 Agent 在新目录下操作，
各工作区的会话内容互不可见、任务互不污染。当前 Agent 目录：
<span style="font-family:var(--mono)">${esc(data.active || '')}</span></div>
${rows || '<em style="color:var(--text3);display:block;margin:10px 0">暂无已保存的工作区。在下方添加第一个 →</em>'}
${window.WS_ACTIVE ? `<div style="margin-top:8px"><button class="btn-secondary" style="padding:5px 14px;font-size:.8em;border-radius:6px" onclick="switchWorkspace('')">↩ 回到默认工作区</button></div>` : ''}
<div style="margin-top:16px;border-top:1px solid var(--border);padding-top:12px">
  <b style="font-size:.92em">➕ 新增工作区</b>
  <div style="display:flex;gap:8px;margin-top:8px">
    <input type="text" id="ws-name" placeholder="名称（如：博客项目）" style="width:160px">
    <input type="text" id="ws-path" placeholder="目录绝对路径（如 D:\\projects\\blog）" style="flex:1">
    <button class="btn-primary" style="padding:0 16px;white-space:nowrap" onclick="addWorkspace()">添加</button>
  </div>
</div>`;
}

async function addWorkspace() {
  const name = document.getElementById('ws-name').value.trim();
  const path = document.getElementById('ws-path').value.trim();
  if (!name || !path) return toast('名称与目录路径均必填', 'error');
  const r = await (await fetch(`${API}/workspaces`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, path }),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast(`工作区「${name}」已保存`, 'success');
  showWorkspaces();
}

async function deleteWorkspace(name) {
  if (!confirm(`删除工作区「${name}」？（只删记录，不删磁盘目录）`)) return;
  await fetch(`${API}/workspaces/${encodeURIComponent(name)}`, { method: 'DELETE' });
  toast('已删除', 'info');
  showWorkspaces();
}

async function switchWorkspace(name) {
  if (running) return toast('有任务正在执行，请先停止再切换工作区', 'error');
  captureTranscript();  // 保存当前工作区的会话内容
  if (!name) {
    // 回到默认工作区：后端项目目录重置为服务器启动目录
    const r = await (await fetch(`${API}/workspaces/switch`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: '' }),
    })).json();
    if (r.error) return toast(r.error, 'error');
    window.WS_ACTIVE = ''; window.WS_SUFFIX = '';
    localStorage.setItem('automind_ws_active', '');
    localStorage.setItem('automind_ws_suffix', '');
  } else {
    const r = await (await fetch(`${API}/workspaces/switch`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })).json();
    if (r.error) return toast(r.error, 'error');
    window.WS_ACTIVE = name;
    window.WS_SUFFIX = '_w' + _wsHash(r.project || name);
    localStorage.setItem('automind_ws_active', name);
    localStorage.setItem('automind_ws_suffix', window.WS_SUFFIX);
  }
  // 加载目标工作区自己的上下文（transcripts 按工作区键隔离）
  loadTranscripts();
  await showConversation(currentMode);
  loadStatus();
  refreshHtmlFiles();
  updateWorkspaceBadge();
  closeModal();
  toast(name ? `已切换到工作区「${name}」` : '已回到默认工作区', 'success');
}

function updateWorkspaceBadge() {
  const el = document.getElementById('ws-badge');
  if (el) el.textContent = '🗂 ' + (window.WS_ACTIVE || '默认');
}
document.addEventListener('DOMContentLoaded', updateWorkspaceBadge);
