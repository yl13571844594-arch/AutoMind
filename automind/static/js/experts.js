// ── 🎓 专家市场 — 官方精选 / 我的专家 / 激活 / 进阶能力（专业版）──
// 专家 = 可复用角色设定；激活后所有任务带该设定执行。
// 社区版：浏览官方 10 个专家 + 一键安装 + 自建最多 3 个；
// 专业版（experts_pro）：无限创建、团队分享、JSON 导入/导出、使用统计；
// 企业版（expert_approval）：分享需管理员审批后全员可见。

let _expertsData = null;

async function loadExpertsView(){
  const msgs = document.getElementById('messages');
  msgs.innerHTML = '<div class="msg agent"><div class="avatar">🎓</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">加载专家市场…</div></div></div>';
  try { _expertsData = await (await fetch(`${API}/experts?session_id=${encodeURIComponent(chatSid())}`)).json(); }
  catch(e){ msgs.innerHTML = '<div class="msg agent"><div class="avatar">🎓</div><div class="col"><div class="bubble">加载失败，请重试</div></div></div>'; return; }
  const d = _expertsData;
  const active = d.active || '';
  const custom = d.installed.filter(e => e.source === 'custom');
  const installed = d.installed.filter(e => e.source === 'official');
  const limitStr = d.custom_limit == null ? '不限' : `${d.custom_count}/${d.custom_limit}`;

  const expertCard = (e, actions) => `
  <div class="expert-card ${active===e.id?'active':''}">
    <div class="ec-icon">${esc(e.icon||'🎓')}</div>
    <div class="ec-body">
      <b>${esc(e.name)}</b>
      ${active===e.id?'<span class="tag safe" style="margin-left:6px">✓ 已激活</span>':''}
      ${e.shared?'<span class="tag" style="margin-left:4px;font-size:.66em">👥 已分享'+(d.approval&&!e.approved?'·待审批':'')+'</span>':''}
      <div class="ec-desc">${esc(e.desc||'')}</div>
      ${d.pro && e.usage ? `<div style="font-size:.7em;color:var(--text3);margin-top:2px">已调用 ${e.usage} 次</div>` : ''}
    </div>
    <div class="ec-actions">${actions}</div>
  </div>`;

  msgs.innerHTML = `
<div class="msg agent"><div class="avatar">🎓</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>🎓 专家市场</b>
  <span style="font-size:.76em;color:var(--text3);margin-left:8px">专家 = 可复用的角色设定；激活后所有任务带该设定执行</span>
  ${d.pro ? `<span style="float:right;display:flex;gap:6px">
    <button class="btn-secondary" style="padding:3px 10px;font-size:.76em;border-radius:6px" onclick="window.open('${API}/experts/export','_blank')">📤 导出</button>
    <button class="btn-secondary" style="padding:3px 10px;font-size:.76em;border-radius:6px" onclick="importExpertsFile()">📥 导入</button>
    <button class="btn-secondary" style="padding:3px 10px;font-size:.76em;border-radius:6px" onclick="showExpertStats()">📊 统计</button>
  </span>` : `<span style="float:right;font-size:.74em;color:var(--text3)">导入/导出/统计/分享 🔒 专业版</span>`}
  <input type="file" id="expert-import-file" accept=".json" style="display:none" onchange="onExpertImport(event)">

  ${active ? `<div class="card lt-green" style="margin-top:10px;display:flex;align-items:center;gap:8px">
    <span>当前激活：<b>${esc((d.installed.find(e=>e.id===active)||{}).name||active)}</b></span>
    <button class="btn-secondary" style="margin-left:auto;padding:3px 12px;font-size:.78em;border-radius:6px" onclick="activateExpert('')">取消激活</button>
  </div>` : ''}

  <div style="margin-top:14px"><b style="font-size:.92em">⭐ 我的专家（${d.custom_limit == null ? custom.length + ' · 不限' : limitStr}）</b></div>
  ${custom.length ? custom.map(e => expertCard(e, `
      ${active===e.id?'':`<button class="btn-primary ec-btn" onclick="activateExpert('${jsq(e.id)}')">激活</button>`}
      ${d.pro && !e.shared ? `<button class="btn-secondary ec-btn" onclick="shareExpert('${jsq(e.id)}')" title="团队分享">👥</button>` : ''}
      <button class="icon-del" onclick="deleteExpert('${jsq(e.id)}')" title="删除">✕</button>`)).join('')
    : '<div class="hint" style="margin:6px 0">还没有自建专家 — 在下方创建，或先从官方精选安装。</div>'}
  <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
    <input type="text" id="exp-icon" placeholder="图标" style="width:52px" maxlength="4" value="🎓">
    <input type="text" id="exp-name" placeholder="专家名称（如：SQL 优化师）" style="width:180px" maxlength="24">
    <input type="text" id="exp-desc" placeholder="一句话简介" style="flex:1;min-width:140px" maxlength="80">
    <button class="btn-primary" style="padding:0 16px;white-space:nowrap" onclick="createExpert()">创建专家</button>
  </div>
  <textarea id="exp-prompt" placeholder="角色设定提示词（它是谁、擅长什么、输出风格与硬性要求）" rows="3"
    style="width:100%;margin-top:8px;resize:vertical"></textarea>

  <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:10px">
    <b style="font-size:.92em">🏛️ 官方精选（${d.official.length}）</b>
    <span style="font-size:.74em;color:var(--text3);margin-left:6px">一键安装即可激活使用</span>
  </div>
  ${d.official.map(e => {
    const inst = installed.find(i => i.id === e.id);
    return expertCard(inst || e, e.installed
      ? (active===e.id ? '' : `<button class="btn-primary ec-btn" onclick="activateExpert('${jsq(e.id)}')">激活</button>`)
        + `<button class="icon-del" onclick="deleteExpert('${jsq(e.id)}')" title="卸载">✕</button>`
      : `<button class="btn-secondary ec-btn" onclick="installExpert('${jsq(e.id)}')">⬇ 安装</button>`);
  }).join('')}
</div></div></div>`;
}

async function installExpert(id){
  const r = await (await fetch(`${API}/experts/install`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})})).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已安装「${r.expert.name}」`, 'success');
  loadExpertsView();
}
async function createExpert(){
  const body = {
    icon: document.getElementById('exp-icon').value.trim() || '🎓',
    name: document.getElementById('exp-name').value.trim(),
    desc: document.getElementById('exp-desc').value.trim(),
    prompt: document.getElementById('exp-prompt').value.trim(),
    session_id: chatSid(),
  };
  if (!body.name || !body.prompt) return toast('名称与角色设定必填', 'error');
  const r = await (await fetch(`${API}/experts`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})).json();
  if (r.error) return toast(r.error, 'error');
  toast(`专家「${body.name}」已创建`, 'success');
  loadExpertsView();
}
async function deleteExpert(id){
  if (!confirm('删除/卸载该专家？')) return;
  await fetch(`${API}/experts/${encodeURIComponent(id)}`, {method:'DELETE'});
  toast('已删除', 'info');
  loadExpertsView();
  updateExpertBadge();
}
async function activateExpert(id){
  const r = await (await fetch(`${API}/experts/activate`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})})).json();
  if (r.error) return toast(r.error, 'error');
  toast(id ? '专家已激活 — 之后的任务将带该角色设定执行' : '已取消专家模式', id?'success':'info');
  loadExpertsView();
  updateExpertBadge();
}
async function shareExpert(id){
  const r = await (await fetch(`${API}/experts/${encodeURIComponent(id)}/share`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({shared:true})})).json();
  if (r.error) return toast(r.error, 'error');
  toast(_expertsData && _expertsData.approval ? '已提交分享，待管理员审批' : '已分享给团队', 'success');
  loadExpertsView();
}
function importExpertsFile(){ document.getElementById('expert-import-file').click(); }
async function onExpertImport(ev){
  const f = ev.target.files[0]; if (!f) return;
  let data; try { data = JSON.parse(await f.text()); } catch(e){ return toast('JSON 解析失败', 'error'); }
  const r = await (await fetch(`${API}/experts/import`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)})).json();
  ev.target.value = '';
  if (r.error) return toast(r.error, 'error');
  toast(`已导入 ${r.imported} 个专家`, 'success');
  loadExpertsView();
}
async function showExpertStats(){
  const r = await (await fetch(`${API}/experts/stats`)).json();
  if (r.error) return toast(r.error, 'error');
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  overlay.classList.add('show');
  const max = Math.max(1, ...(r.stats||[]).map(s=>s.usage));
  content.innerHTML = `<h2>📊 专家使用统计</h2>
    <div class="hint">哪个专家被调用最多（按任务注入次数累计）。</div>
    ${(r.stats||[]).length ? r.stats.map(s => `
      <div style="display:flex;align-items:center;gap:10px;margin:8px 0">
        <span style="width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.icon)} ${esc(s.name)}</span>
        <div style="flex:1;height:10px;background:var(--bg2);border-radius:5px;overflow:hidden">
          <div style="width:${Math.round(s.usage/max*100)}%;height:100%;background:var(--accent-grad)"></div></div>
        <b style="width:46px;text-align:right">${s.usage}</b>
      </div>`).join('') : '<em style="color:var(--text3)">暂无使用记录</em>'}
    <div class="btn-row"><button class="btn-secondary" onclick="closeModal()">关闭</button></div>`;
}

// 激活徽标：模式提示条尾部显示当前专家
async function updateExpertBadge(){
  try {
    const d = await (await fetch(`${API}/experts?session_id=${encodeURIComponent(chatSid())}`)).json();
    const hint = document.getElementById('mode-hint');
    let chip = document.getElementById('expert-chip');
    if (d.active) {
      const e = (d.installed||[]).find(x => x.id === d.active) || {name: d.active, icon:'🎓'};
      if (!chip) { chip = document.createElement('span'); chip.id = 'expert-chip'; hint.appendChild(chip); }
      chip.innerHTML = `　<b style="color:var(--purple)">${esc(e.icon)} 专家模式：${esc(e.name)}</b>
        <a href="javascript:void(0)" onclick="activateExpert('')" style="font-size:.86em;color:var(--text3);margin-left:4px" title="取消专家模式">✕</a>`;
    } else if (chip) chip.remove();
  } catch(e){}
}
document.addEventListener('DOMContentLoaded', () => setTimeout(updateExpertBadge, 800));
