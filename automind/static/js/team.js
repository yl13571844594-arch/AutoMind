// ── 👥 团队协作 — 任务分配看板 + 操作通知流 ──
// 同一服务器 = 同一团队：工作区、任务历史、模板与专家天然共享；
// 本模块补齐「任务分配」与「同事的 Agent 改了文件 → 通知你」。

let _teamFeed = [];   // 会话内活动流（ws team_activity 事件）

function onTeamActivity(d){
  _teamFeed.unshift(d);
  if (_teamFeed.length > 50) _teamFeed.pop();
  // 别人的动作才提醒（自己的任务完成已有结果气泡）
  if (d.sid && d.sid !== chatSid()) {
    if (d.kind === 'task_done') {
      toast(`👥 同事完成了任务「${d.task}」${d.changed_files ? `（涉及 ${d.changed_files} 个文件改动，右栏可查看/回滚）` : ''}`, 'info');
      refreshChanges();
    } else if (d.kind === 'task_assigned') {
      toast(`👥 新团队任务：「${d.title}」${d.assignee ? ` → ${d.assignee}` : ''}`, 'info');
    }
  }
  if (currentView === 'team') loadTeamView();
}

async function loadTeamView(){
  const msgs = document.getElementById('messages');
  let tasks = [];
  try { tasks = (await (await fetch(`${API}/team/tasks`)).json()).tasks || []; } catch(e){}
  const cols = { todo: '📥 待办', doing: '🔧 进行中', done: '✅ 已完成' };
  const badge = s => ({todo:'',doing:'lt-yellow',done:'lt-green'})[s] || '';
  msgs.innerHTML = `
<div class="msg agent"><div class="avatar">👥</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>👥 团队协作</b>
  <span style="font-size:.76em;color:var(--text3);margin-left:8px">同一服务器 = 同一团队：工作区 / 模板 / 专家 / 任务历史全员共享</span>

  <div style="margin-top:12px"><b style="font-size:.92em">📋 任务分配（${tasks.length}）</b></div>
  <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
    <input type="text" id="tt-title" placeholder="任务标题（如：重构登录模块）" style="flex:1;min-width:180px" maxlength="120">
    <input type="text" id="tt-assignee" placeholder="指派给（成员名）" style="width:140px" maxlength="40">
    <button class="btn-primary" style="padding:0 16px;white-space:nowrap" onclick="addTeamTask()">＋ 分配任务</button>
  </div>
  ${['todo','doing','done'].map(s => {
    const list = tasks.filter(t => t.status === s);
    return `<div style="margin-top:10px"><b style="font-size:.84em;color:var(--text2)">${cols[s]}（${list.length}）</b>
      ${list.map(t => `
      <div class="card ${badge(s)}" style="display:flex;align-items:center;gap:10px">
        <div style="flex:1;min-width:0">
          <b>${esc(t.title)}</b>
          ${t.assignee ? `<span class="tag" style="margin-left:6px;font-size:.68em">👤 ${esc(t.assignee)}</span>` : ''}
          <div style="font-size:.72em;color:var(--text3)">${esc(t.created||'')}${t.desc?` · ${esc(t.desc.slice(0,60))}`:''}</div>
        </div>
        ${s!=='todo' ? `<button class="btn-secondary ec-btn" onclick="setTeamTask('${jsq(t.id)}','todo')" title="退回待办">↩</button>`:''}
        ${s!=='doing' ? `<button class="btn-secondary ec-btn" onclick="setTeamTask('${jsq(t.id)}','doing')" title="开始">▶</button>`:''}
        ${s!=='done' ? `<button class="btn-primary ec-btn" onclick="setTeamTask('${jsq(t.id)}','done')" title="完成">✓</button>`:''}
        <button class="icon-del" onclick="delTeamTask('${jsq(t.id)}')" title="删除">✕</button>
      </div>`).join('') || '<div style="font-size:.78em;color:var(--text3);padding:4px 2px">（空）</div>'}
    </div>`;
  }).join('')}

  <div style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px">
    <b style="font-size:.92em">🔔 操作通知（本次会话，实时）</b>
    <span style="font-size:.72em;color:var(--text3);margin-left:6px">同事的 Agent 完成任务/改文件会实时出现在这里并弹提醒</span>
  </div>
  ${_teamFeed.length ? _teamFeed.slice(0,20).map(d => `
    <div style="font-size:.8em;padding:5px 0;border-bottom:1px dashed var(--border)">
      <span style="font-family:var(--mono);color:var(--text3)">${esc(d.time||'')}</span>
      ${d.kind==='task_done'
        ? `<b>${d.sid===chatSid()?'我':'同事'}</b> 完成任务「${esc(d.task||'')}」${d.success?'<span style="color:var(--green)">✓</span>':'<span style="color:var(--red)">✗</span>'}${d.changed_files?` · ${d.changed_files} 个文件改动`:''}`
        : `<b>新任务</b>「${esc(d.title||'')}」${d.assignee?` → ${esc(d.assignee)}`:''}`}
    </div>`).join('') : '<div style="font-size:.78em;color:var(--text3);margin-top:6px">暂无活动 — 任一成员执行任务后这里会出现记录</div>'}

  <div class="hint" style="margin-top:12px">
    💡 共享语义：<b>工作区</b>（同一目录协同）、<b>自定义模板</b>（专业版）、<b>专家</b>（分享后全员可用，
    企业版含审批流）均为服务器级存储 —— 部署到一台服务器即天然团队共享；
    配合企业版 SSO/RBAC 可获得成员身份与权限控制（见手册 10.4）。</div>
</div></div></div>`;
}

async function addTeamTask(){
  const title = document.getElementById('tt-title').value.trim();
  if (!title) return toast('任务标题必填', 'error');
  const r = await (await fetch(`${API}/team/tasks`, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({title, assignee: document.getElementById('tt-assignee').value.trim(),
                          session_id: chatSid()})})).json();
  if (r.error) return toast(r.error, 'error');
  toast('任务已分配', 'success');
  loadTeamView();
}
async function setTeamTask(id, status){
  await fetch(`${API}/team/tasks/${encodeURIComponent(id)}`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
  loadTeamView();
}
async function delTeamTask(id){
  if (!confirm('删除该团队任务？')) return;
  await fetch(`${API}/team/tasks/${encodeURIComponent(id)}`, {method:'DELETE'});
  loadTeamView();
}
