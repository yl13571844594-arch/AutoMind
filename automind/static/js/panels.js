<<<<<<< HEAD
// ── 统计分析 ──
function ringColor(p){ return p==null?'var(--text3)':(p>=80?'var(--green)':p>=50?'var(--accent)':p>=30?'var(--yellow)':'var(--red)'); }
function ring(label, pct){
  const v = pct==null ? '—' : pct+'%';
  return `<div class="ring-item"><div class="ring-chart" style="--pct:${pct||0};--ring-color:${ringColor(pct)}"><span class="rv">${v}</span></div><span class="rlabel">${label}</span></div>`;
}
function sparkline(points){
  const vals = points.map(p=>p.tool_hit_rate).filter(v=>v!=null);
  if(vals.length<2) return '<div style="font-size:.8em;color:var(--text3)">数据不足，至少需 2 次任务</div>';
  const w=280,h=50,max=100,min=0;
  const step = w/(vals.length-1);
  const pts = vals.map((v,i)=>`${(i*step).toFixed(1)},${(h-(v-min)/(max-min)*h).toFixed(1)}`).join(' ');
  const last = vals[vals.length-1];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".35"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    <polygon points="0,${h} ${pts} ${w},${h}" fill="url(#sg)"/>
    <polyline points="${pts}" fill="none" stroke="${ringColor(last)}" stroke-width="2" stroke-linejoin="round"/>
  </svg>`;
}
async function loadStatsView() {
  let base, d, hist;
  try {
    [base, d, hist] = await Promise.all([
      (await fetch(`${API}/stats`)).json(),
      (await fetch(`${API}/stats/detail`)).json(),
      (await fetch(`${API}/stats/history`)).json(),
    ]);
  } catch(e) {
    document.getElementById('messages').innerHTML = `<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%"><div class="panel-head"><b>📊 高级统计仪表盘</b><button onclick="loadStatsView()" class="btn-secondary" style="margin-left:auto;padding:4px 12px;border-radius:8px;font-size:.76em">🔄 刷新</button></div><div class="dash-section"><div class="dash-card" style="text-align:center;color:var(--red)">❌ 统计加载失败: ${esc(e.message||'')}<br><span style="font-size:.8em;color:var(--text3)">请确认 Agent 已初始化并有任务记录</span></div></div></div></div></div>`;
    return;
  }
  const hr = (d && d.hit_rates) || {};
  const ctx = (d && d.context) || {estimated_tokens:0, max_tokens:100000, usage_pct:0, compressed:false, summary_length:0, message_count:0};
  const eff = (d && d.efficiency) || {token_efficiency_chars_per_token:null, total_prompt_tokens:0, total_completion_tokens:0, total_output_chars:0};
  const mem = (d && d.memory) || {long_term_docs:0, short_term_msgs:0, kg_entities:0, kg_relations:0};
  const totals = (d && d.totals) || {tool_calls:0, tool_successes:0, plan_goals_total:0, plan_goals_completed:0, tasks_total:0, tasks_success:0};
  const tools = Object.entries((base && base.tool_usage)||{}).slice(0,8).map(([t,n])=>`
    <span class="tag safe" style="margin:3px;display:inline-block">${esc(t)} ×${n}</span>`).join('') || '<em style="color:var(--text3)">暂无工具调用</em>';
  const audit = (base && base.audit) || {ask_user:0, dangerous:0};
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <div class="panel-head"><b>📊 高级统计仪表盘</b>
    <button onclick="loadStatsView()" class="btn-secondary" style="margin-left:auto;padding:4px 12px;border-radius:8px;font-size:.76em">🔄 刷新</button></div>

  <div class="dash-section"><h4>命中率仪表盘</h4>
    <div class="ring-row">
      ${ring('工具命中', hr.tool_hit_rate)}
      ${ring('计划命中', hr.plan_hit_rate)}
      ${ring('任务成功', hr.task_success_rate)}
      ${ring('自我修正', hr.self_correction_rate)}
    </div>
    <div style="text-align:center">${hr.average_hit_rate!=null?`<span class="avg-pill">★ 综合平均命中率 ${hr.average_hit_rate}%</span>`:'<span style="font-size:.82em;color:var(--text3)">暂无足够数据计算命中率</span>'}</div>
  </div>

  <div class="dash-section"><h4>上下文使用率</h4>
    <div class="dash-card">
      <div style="display:flex;justify-content:space-between;font-size:.86em"><span>${ctx.estimated_tokens.toLocaleString()} / ${ctx.max_tokens.toLocaleString()} tokens</span><span class="mv">${ctx.usage_pct}%</span></div>
      <div class="glow-bar"><div class="fill" style="width:${Math.min(ctx.usage_pct,100)}%"></div></div>
      <div style="font-size:.78em;color:var(--text3);margin-top:6px">${ctx.compressed?`⚠ 已触发压缩 · 摘要 ${ctx.summary_length} 字`:'未触发压缩'} · 窗口 ${ctx.message_count} 条消息</div>
    </div>
  </div>

  <div class="dash-section"><h4>效率与用量</h4>
    <div class="dash-card">
      <div class="metric-row"><span>Token 效率</span><span class="mv">${eff.token_efficiency_chars_per_token!=null?eff.token_efficiency_chars_per_token+' 字/Token':'—'}</span></div>
      <div class="metric-row"><span>累计 Token（输入/输出）</span><span class="mv">${eff.total_prompt_tokens.toLocaleString()} / ${eff.total_completion_tokens.toLocaleString()}</span></div>
      <div class="metric-row"><span>总输出字符</span><span class="mv">${eff.total_output_chars.toLocaleString()}</span></div>
      <div class="metric-row"><span>任务 / 成功</span><span class="mv">${totals.tasks_total||0} / ${totals.tasks_success||0}</span></div>
      <div class="metric-row"><span>工具调用 / 成功</span><span class="mv">${totals.tool_calls||0} / ${totals.tool_successes||0}</span></div>
    </div>
  </div>

  <div class="dash-section"><h4>📈 工具命中率趋势（最近 ${hist.count} 次）</h4>
    <div class="dash-card">${sparkline(hist.points)}</div>
  </div>

  <div class="dash-section"><h4>🧠 记忆系统</h4>
    <div class="dash-card">
      <div class="metric-row"><span>向量存储</span><span class="mv">${mem.long_term_docs} docs</span></div>
      <div class="metric-row"><span>知识图谱</span><span class="mv">${mem.kg_entities} 实体 / ${mem.kg_relations} 关系</span></div>
      <div class="metric-row"><span>短期窗口</span><span class="mv">${mem.short_term_msgs} 条消息</span></div>
    </div>
  </div>

  <div class="dash-section"><h4>工具使用 Top</h4><div>${tools}</div>
    <div style="font-size:.78em;color:var(--text3);margin-top:8px">审批请求 ${audit.ask_user||0} · 高危 ${audit.dangerous||0} · 定时任务 ${base&&base.scheduled_tasks||0}
      <button onclick="fetch('${API}/tokens',{method:'DELETE'}).then(()=>loadStatsView())" class="btn-secondary" style="float:right;padding:3px 9px;border-radius:6px;font-size:.92em">重置Token</button></div>
  </div>
</div></div></div>`;
}

// ── 定时任务 ──
async function loadScheduleView() {
  const list = await (await fetch(`${API}/schedule`)).json();
  const rows = list.length ? list.map(s=>`
    <div class="card ${s.enabled?'lt-green':''}">
      <b>${esc(s.name)}</b>
      <span class="tag safe">${MODE_LABELS[s.interaction]||s.interaction}</span>
      <span style="float:right">
        <button class="btn-secondary" style="padding:3px 9px;font-size:.74em;border-radius:6px" onclick="runSchedule('${jsq(s.id)}')">立即运行</button>
        <button class="btn-secondary" style="padding:3px 9px;font-size:.74em;border-radius:6px" onclick="toggleSchedule('${jsq(s.id)}',${!s.enabled})">${s.enabled?'暂停':'启用'}</button>
        <button class="btn-danger" style="padding:3px 9px;font-size:.74em;border-radius:6px" onclick="delSchedule('${jsq(s.id)}')">删除</button>
      </span>
      <div style="font-size:.8em;color:var(--text2);margin-top:5px">${esc(s.task.slice(0,80))}</div>
      <div style="font-size:.76em;color:var(--text3);margin-top:3px">每 ${fmtInterval(s.interval)} · 已运行 ${s.runs} 次 ${s.last_status?'· '+esc(s.last_status):''} ${s.enabled&&s.next_in!=null?'· 下次 '+fmtInterval(s.next_in)+'后':''}</div>
    </div>`).join('') : '<em style="color:var(--text3)">暂无定时任务</em>';
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>⏰ 定时任务</b>
  <div class="card" style="background:var(--bg1);margin-top:10px">
    <input type="text" id="sch-name" placeholder="名称（可选）" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.85em;margin-bottom:8px">
    <textarea id="sch-task" placeholder="要定时执行的任务内容..." style="width:100%;min-height:54px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.85em;resize:vertical;font-family:var(--font)"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
      <select id="sch-mode" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.84em">
        <option value="chat">对话</option><option value="work">工作</option>
        <option value="coding">编程</option><option value="loop">循环</option><option value="multi">协同</option>
      </select>
      <select id="sch-interval" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.84em">
        <option value="300">每5分钟</option><option value="3600" selected>每小时</option>
        <option value="21600">每6小时</option><option value="86400">每天</option>
      </select>
      <button class="btn-primary" style="padding:8px 16px" onclick="addSchedule()">添加</button>
    </div>
  </div>
  <div style="margin-top:10px">${rows}</div>
</div></div></div>`;
}
function fmtInterval(s){ s=+s; if(s>=86400)return Math.round(s/86400)+'天'; if(s>=3600)return Math.round(s/3600)+'小时'; if(s>=60)return Math.round(s/60)+'分钟'; return s+'秒'; }
async function addSchedule(){
  const task=document.getElementById('sch-task').value.trim();
  if(!task) return toast('请输入任务内容','error');
  await fetch(`${API}/schedule`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    name:document.getElementById('sch-name').value.trim(), task,
    interaction:document.getElementById('sch-mode').value,
    interval:parseInt(document.getElementById('sch-interval').value)})});
  toast('定时任务已添加','success'); loadScheduleView();
}
async function runSchedule(id){ await fetch(`${API}/schedule/${id}/run`,{method:'POST'}); toast('已触发运行','info'); }
async function toggleSchedule(id,en){ await fetch(`${API}/schedule/${id}/toggle`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})}); loadScheduleView(); }
async function delSchedule(id){ await fetch(`${API}/schedule/${id}`,{method:'DELETE'}); loadScheduleView(); toast('已删除','info'); }

let toolsTab = 'tools';
async function loadToolsView(tab) {
  toolsTab = tab || toolsTab;
  // 拉取各分类数量用于角标
  let counts = {tools:0, skills:0, mcp:0, plugins:0};
  try {
    const [t,s,m,p] = await Promise.all([
      fetch(`${API}/tools`).then(r=>r.json()),
      fetch(`${API}/skills`).then(r=>r.json()),
      fetch(`${API}/mcp`).then(r=>r.json()),
      fetch(`${API}/plugins`).then(r=>r.json()),
    ]);
    counts = {tools: t.length, skills: s.length, mcp: (m.servers||[]).length, plugins: (p.plugins||[]).length};
  } catch(e) {}
  const segs = [
    ['tools','🔧','工具', counts.tools],
    ['skills','✨','技能', counts.skills],
    ['mcp','🔌','MCP', counts.mcp],
    ['plugins','🧩','插件', counts.plugins],
  ];
  const bar = `<div class="seg-tabs">
    ${segs.map(([k,ic,l,n])=>`
      <button class="seg ${k===toolsTab?'active':''}" onclick="loadToolsView('${k}')">
        <span class="seg-ic">${ic}</span><span class="seg-label">${l}</span>
        <span class="seg-badge">${n}</span>
      </button>`).join('')}
  </div>`;
  let body = '';
  if (toolsTab === 'tools') body = await renderToolsList();
  else if (toolsTab === 'skills') body = await renderSkillsList();
  else if (toolsTab === 'mcp') body = await renderMCPList();
  else if (toolsTab === 'plugins') body = await renderPluginsList();
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  ${bar}${body}
</div></div></div>`;
}
const TOOL_ICONS = {terminal:'⌨️', file_read:'📖', file_write:'✍️', file_edit:'✏️', python_sandbox:'🐍', browser:'🌐', web_fetch:'🔗'};
async function renderToolsList() {
  const tools = await (await fetch(`${API}/tools`)).json();
  const enabled = tools.filter(t=>t.enabled).length;
  return `<div class="panel-head"><b>🔧 可用工具</b><span class="count-pill">${enabled}/${tools.length} 启用</span></div>
  <div class="hint" style="margin:2px 0 12px">关闭开关可临时禁用某工具（Agent 执行时将不可调用）。</div>
  <div class="tool-grid">
  ${tools.map(t => `
    <div class="tool-card ${t.enabled?'':'off'}">
      <div class="tool-icon">${TOOL_ICONS[t.name]||(t.mcp?'🔌':'🛠')}</div>
      <div class="tool-main">
        <div class="tool-title">${esc(t.name)}
          <span class="tag ${t.tier}">${t.tier}</span>
          <span class="risk-dot" title="风险 ${t.risk}" style="background:${t.risk>=80?'var(--red)':t.risk>=40?'var(--yellow)':'var(--green)'}"></span>
        </div>
        <div class="tool-desc">${esc(t.description)}</div>
        ${(t.params||[]).length?`<div class="tool-params">参数: ${t.params.map(p=>`<code>${esc(p)}</code>`).join(' ')}</div>`:''}
      </div>
      <label class="switch" title="${t.enabled?'点击禁用':'点击启用'}">
        <input type="checkbox" ${t.enabled?'checked':''} onchange="toggleTool('${esc(t.name)}', this.checked)">
        <span class="slider"></span>
      </label>
    </div>`).join('')}
  </div>`;
}
async function toggleTool(name, enabled){
  await fetch(`${API}/tools/toggle`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, enabled})});
  toast(enabled?`已启用 ${name}`:`已禁用 ${name}`, enabled?'success':'info');
  loadToolsView('tools');
}
async function renderSkillsList() {
  const skills = await (await fetch(`${API}/skills`)).json();
  const custom = skills.filter(s=>!s.builtin).length;
  const mdCount = skills.filter(s=>s.type==='markdown').length;
  return `<div class="panel-head"><b>✨ 技能库</b><span class="count-pill">${skills.length} 个 · ${custom} 自定义 · ${mdCount} SKILL.md</span></div>
  <div class="add-box">
    <div class="add-box-title">➕ 添加技能</div>
    <div class="hint" style="margin-bottom:8px">支持 <b>SKILL.md 技能包</b>（文件夹含 <code>SKILL.md</code>）与 <b>.py 技能</b>（含 <code>AbstractSkill</code> 子类）。</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <input type="text" id="skill-dir" placeholder="技能目录，如 C:\\Users\\you\\Desktop\\skills" style="flex:1;min-width:180px;padding:9px 11px;border:1px solid var(--border);border-radius:8px;background:var(--bg0);color:var(--text);font-size:.85em">
      <button class="btn-primary" style="padding:9px 16px" onclick="loadSkillDir()">📁 加载目录</button>
      <input type="file" id="skill-file" accept=".py" style="display:none" onchange="importSkillFile(event)">
      <button class="btn-secondary" style="padding:9px 16px" onclick="document.getElementById('skill-file').click()">📄 导入 .py</button>
    </div>
    <button class="btn-secondary" style="margin-top:8px;padding:8px 14px;width:100%" onclick="importDesktopSkills()">⬇️ 一键导入桌面 skills 文件夹</button>
  </div>
  <div class="card-list">
  ${skills.map(s => {
    const isMd = s.type==='markdown';
    const tag = s.builtin?'<span class="tag safe">内置</span>'
      : isMd?'<span class="tag" style="background:var(--purple-bg);color:var(--purple)">SKILL.md</span>'
      : '<span class="tag sensitive">Python</span>';
    const reqs = (s.required_tools||[]).length?`<div style="font-size:.74em;color:var(--text3);margin-top:3px">依赖工具: ${s.required_tools.map(t=>esc(t)).join(', ')}</div>`:'';
    return `
    <div class="item-card ${s.builtin?'lt-accent':isMd?'lt-purple':'lt-yellow'}">
      <div class="skill-emoji">${esc(s.emoji||'✨')}</div>
      <div class="item-main">
        <div class="item-title">${esc(s.name)} ${tag}</div>
        <div class="item-desc">${esc(s.description||'(无描述)')}</div>
        ${reqs}
      </div>
      ${s.builtin?'':`<button class="icon-del" title="删除技能" onclick="delSkill('${jsq(s.name)}')">🗑</button>`}
    </div>`;}).join('')}
  </div>`;
}
async function importDesktopSkills(){
  // 跨平台：交由后端 expanduser 解析当前用户主目录，不再硬编码具体用户名
  const candidates = ['~/Desktop/skills', '~/桌面/skills', '~/skills'];
  toast('正在导入桌面 skills 文件夹...', 'info');
  let done = false;
  for (const directory of candidates) {
    const r = await (await fetch(`${API}/skills/load`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ directory }),
    })).json();
    if (!r.error) { toast(`已导入 ${r.loaded} 个技能（SKILL.md ${r.markdown||0} · Python ${r.py||0}）`, 'success'); done = true; break; }
  }
  if (!done) toast('未找到桌面 skills 文件夹，请用「加载目录」手动指定', 'error');
  loadToolsView('skills');
}
async function loadSkillDir() {
  const directory = document.getElementById('skill-dir').value.trim();
  if (!directory) return toast('请输入技能目录', 'error');
  const r = await (await fetch(`${API}/skills/load`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ directory }),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已加载 ${r.loaded} 个技能（共 ${r.total}）`, r.loaded?'success':'info');
  loadToolsView('skills');
}
async function importSkillFile(ev){
  const file = ev.target.files[0]; if(!file) return;
  const code = await file.text();
  const r = await (await fetch(`${API}/skills/import`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name: file.name, code }),
  })).json();
  ev.target.value='';
  if (r.error) return toast(r.error, 'error');
  toast(`已导入技能: ${(r.imported||[]).join(', ')}`, 'success');
  loadToolsView('skills');
}
async function delSkill(name){
  if(!confirm(`确定删除技能「${name}」？`)) return;
  const r = await (await fetch(`${API}/skills/${encodeURIComponent(name)}`, {method:'DELETE'})).json();
  if (r.error) return toast(r.error, 'error');
  toast('已删除 '+name, 'info');
  loadToolsView('skills');
}
async function renderMCPList() {
  const data = await (await fetch(`${API}/mcp`)).json();
  const sdkWarn = data.sdk_installed ? '' :
    `<div class="item-card lt-yellow" style="font-size:.82em">⚠ 未检测到 MCP SDK，服务器可保存但无法连接。请先执行 <code>pip install mcp</code>。</div>`;
  const importBox = `
  <div class="add-box">
    <div class="add-box-title">📥 批量导入 (Claude Desktop 格式)</div>
    <div class="hint" style="margin-bottom:6px">粘贴 <code>{"mcpServers": {...}}</code> 配置，或导入 JSON 文件。</div>
    <textarea id="mcp-import" placeholder='{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}' class="mcp-import-ta"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
      <input type="file" id="mcp-file" accept=".json" style="display:none" onchange="importMCPFile(event)">
      <button class="btn-secondary" style="padding:7px 14px" onclick="document.getElementById('mcp-file').click()">📄 选择文件</button>
      <span style="flex:1"></span>
      <button class="btn-primary" style="padding:7px 16px" onclick="importMCP()">导入并连接</button>
    </div>
  </div>`;
  const form = `
  <div class="add-box">
    <div class="add-box-title">➕ 添加单个 MCP 服务器</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <input type="text" id="mcp-name" placeholder="名称 (如 filesystem)" class="mcp-in">
      <select id="mcp-transport" class="mcp-in" onchange="toggleMcpTransport()">
        <option value="stdio">stdio (本地命令)</option>
        <option value="sse">sse (URL)</option>
      </select>
    </div>
    <div id="mcp-stdio" style="display:grid;grid-template-columns:1fr 2fr;gap:8px;margin-top:8px">
      <input type="text" id="mcp-command" placeholder="命令 (如 npx)" class="mcp-in">
      <input type="text" id="mcp-args" placeholder="参数 (空格分隔)" class="mcp-in">
    </div>
    <div id="mcp-sse" style="display:none;margin-top:8px">
      <input type="text" id="mcp-url" placeholder="SSE URL (如 http://localhost:3000/sse)" class="mcp-in" style="width:100%">
    </div>
    <div style="text-align:right;margin-top:10px">
      <button class="btn-primary" style="padding:8px 16px" onclick="addMCP()">添加并连接</button>
    </div>
  </div>`;
  const list = (data.servers||[]).length ? `<div class="card-list">${data.servers.map(s => `
    <div class="item-card ${s.connected?'lt-green':'lt-red'}">
      <div class="item-main">
        <div class="item-title">🔌 ${esc(s.name)} <span class="tag ${s.connected?'safe':'dangerous'}">${s.connected?'已连接':'未连接'}</span></div>
        <div class="item-desc" style="font-family:var(--mono);font-size:.78em">${esc(s.transport)} · ${esc(s.command||s.url)} ${esc((s.args||[]).join(' '))}</div>
        ${s.tools.length?`<div style="font-size:.8em;color:var(--text2);margin-top:3px">工具: ${s.tools.map(t=>esc(t)).join(', ')}</div>`:''}
      </div>
      <button class="icon-del" title="删除服务器" onclick="delMCP('${jsq(s.name)}')">🗑</button>
    </div>`).join('')}</div>` : '<em style="color:var(--text3)">暂无 MCP 服务器</em>';
  return `<div class="panel-head"><b>🔌 MCP 服务器</b><span class="count-pill">${(data.servers||[]).length} 个</span></div>${sdkWarn}${importBox}${form}${list}`;
}
async function renderPluginsList() {
  const data = await (await fetch(`${API}/plugins`)).json();
  const plugins = data.plugins || [];
  const loaded = plugins.filter(p=>p.loaded).length;
  const infoBox = `
  <div class="add-box">
    <div class="add-box-title">🧩 插件目录</div>
    <div class="hint" style="margin-bottom:8px">把插件放在 <code>~/.automind/plugins/&lt;名称&gt;/</code> 下，每个插件包含 <code>plugin.json</code>（元信息）与 <code>hooks.py</code>（提供 <code>get_hooks() → AgentHooks</code>）。加载后其生命周期钩子（任务开始/结束、解析后、计划后、出错等）将自动生效。</div>
    <div style="text-align:right"><button class="btn-secondary" style="padding:7px 14px" onclick="loadToolsView('plugins')">🔄 重新扫描</button></div>
  </div>`;
  const list = plugins.length ? `<div class="card-list">${plugins.map(p => `
    <div class="item-card ${p.loaded?'lt-green':'lt-yellow'}">
      <div class="item-main">
        <div class="item-title">🧩 ${esc(p.name)} <span class="tag ${p.loaded?'safe':'sensitive'}">${p.loaded?'已加载':'未加载'}</span>
          ${p.version?`<span style="font-size:.72em;color:var(--text3);margin-left:4px">v${esc(p.version)}</span>`:''}</div>
        <div class="item-desc">${esc(p.description||'(无描述)')}</div>
        ${p.author?`<div style="font-size:.74em;color:var(--text3);margin-top:3px">作者: ${esc(p.author)}</div>`:''}
      </div>
      ${p.loaded
        ? `<button class="btn-secondary" style="padding:6px 14px" onclick="unloadPlugin('${jsq(p.name)}')">卸载</button>`
        : `<button class="btn-primary" style="padding:6px 14px" onclick="loadPlugin('${jsq(p.name)}')">加载</button>`}
    </div>`).join('')}</div>`
    : '<em style="color:var(--text3)">未发现插件。请在 <code>~/.automind/plugins</code> 下放置插件目录后点击「重新扫描」。</em>';
  return `<div class="panel-head"><b>🧩 插件</b><span class="count-pill">${loaded}/${plugins.length} 已加载</span></div>${infoBox}${list}`;
}
async function loadPlugin(name){
  const r = await (await fetch(`${API}/plugins/${encodeURIComponent(name)}/load`, {method:'POST'})).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已加载插件 ${name}`, 'success');
  loadToolsView('plugins');
}
async function unloadPlugin(name){
  await fetch(`${API}/plugins/${encodeURIComponent(name)}/unload`, {method:'POST'});
  toast(`已卸载插件 ${name}`, 'info');
  loadToolsView('plugins');
}
function toggleMcpTransport() {
  const t = document.getElementById('mcp-transport').value;
  document.getElementById('mcp-stdio').style.display = t==='stdio'?'grid':'none';
  document.getElementById('mcp-sse').style.display = t==='sse'?'block':'none';
}
async function addMCP() {
  const body = {
    name: document.getElementById('mcp-name').value.trim(),
    transport: document.getElementById('mcp-transport').value,
    command: document.getElementById('mcp-command').value.trim(),
    args: document.getElementById('mcp-args').value.trim(),
    url: document.getElementById('mcp-url').value.trim(),
  };
  if (!body.name) return toast('请输入服务器名称', 'error');
  const r = await (await fetch(`${API}/mcp`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body),
  })).json();
  if (r.error && !r.status) return toast(r.error, 'error');
  toast(r.connected ? 'MCP 已连接' : ('已保存，但'+(r.error||'未连接')), r.connected?'success':'info');
  loadToolsView('mcp');
}
async function importMCP() {
  const raw = document.getElementById('mcp-import').value.trim();
  if (!raw) return toast('请粘贴 MCP 配置', 'error');
  let cfg; try { cfg = JSON.parse(raw); } catch(e){ return toast('JSON 解析失败: '+e.message, 'error'); }
  const r = await (await fetch(`${API}/mcp/import`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({config: cfg}),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已导入 ${r.imported} 个服务器${r.connected_any?'（部分已连接）':''}`, 'success');
  loadToolsView('mcp');
}
async function importMCPFile(ev){
  const file = ev.target.files[0]; if(!file) return;
  document.getElementById('mcp-import').value = await file.text();
  ev.target.value='';
  toast('已读取文件，点击「导入并连接」', 'info');
}
async function delMCP(name) {
  if(!confirm(`确定删除 MCP 服务器「${name}」？`)) return;
  await fetch(`${API}/mcp/${encodeURIComponent(name)}`, {method:'DELETE'});
  toast('已删除 '+name, 'info');
  loadToolsView('mcp');
}
async function loadHistoryView() {
  const history = await (await fetch(`${API}/history`)).json();
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>📜 任务历史 (${history.length})</b>
  <button onclick="clearHistory()" class="btn-danger" style="float:right;padding:4px 10px;border-radius:6px;font-size:.78em">清空</button>
  <br>
  ${history.length === 0 ? '<em style="color:var(--text3)">暂无历史记录</em>' :
    history.slice().reverse().map(h => `
    <div class="card ${h.success?'lt-green':'lt-red'}">
      <button class="btn-danger" style="float:right;padding:2px 9px;font-size:.74em;border-radius:6px" onclick="delHistory('${jsq(h.session_id)}')">删除</button>
      <div style="font-weight:500;padding-right:50px">${esc((h.task||'').slice(0,120))}</div>
      <div style="font-size:.78em;color:var(--text3);margin-top:3px">
        ${({chat:'💬对话',work:'⚙️工作',coding:'💻编程',multi:'🤝协同',loop:'🔁循环'})[h.interaction]||''}${h.scheduled?' ⏰':''}
        · ${h.steps}步 · ${h.tokens}tk · ${h.duration_ms}ms · ${h.session_id}
      </div>
      <div style="font-size:.82em;color:var(--text2);margin-top:4px;max-height:60px;overflow:hidden">${esc((h.output||'').slice(0,200))}</div>
    </div>`).join('')}
</div></div></div>`;
}
function loadPlanView() {
  const planEl = document.getElementById('plan-view');
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>📋 最近执行计划</b><br><br>${planEl.innerHTML}
</div></div></div>`;
}

=======
// ── 统计分析 ──
function ringColor(p){ return p==null?'var(--text3)':(p>=80?'var(--green)':p>=50?'var(--accent)':p>=30?'var(--yellow)':'var(--red)'); }
function ring(label, pct){
  const v = pct==null ? '—' : pct+'%';
  return `<div class="ring-item"><div class="ring-chart" style="--pct:${pct||0};--ring-color:${ringColor(pct)}"><span class="rv">${v}</span></div><span class="rlabel">${label}</span></div>`;
}
function sparkline(points){
  const vals = points.map(p=>p.tool_hit_rate).filter(v=>v!=null);
  if(vals.length<2) return '<div style="font-size:.8em;color:var(--text3)">数据不足，至少需 2 次任务</div>';
  const w=280,h=50,max=100,min=0;
  const step = w/(vals.length-1);
  const pts = vals.map((v,i)=>`${(i*step).toFixed(1)},${(h-(v-min)/(max-min)*h).toFixed(1)}`).join(' ');
  const last = vals[vals.length-1];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".35"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
    <polygon points="0,${h} ${pts} ${w},${h}" fill="url(#sg)"/>
    <polyline points="${pts}" fill="none" stroke="${ringColor(last)}" stroke-width="2" stroke-linejoin="round"/>
  </svg>`;
}
async function loadStatsView() {
  let base, d, hist;
  try {
    [base, d, hist] = await Promise.all([
      (await fetch(`${API}/stats`)).json(),
      (await fetch(`${API}/stats/detail`)).json(),
      (await fetch(`${API}/stats/history`)).json(),
    ]);
  } catch(e) {
    document.getElementById('messages').innerHTML = `<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%"><div class="panel-head"><b>📊 高级统计仪表盘</b><button onclick="loadStatsView()" class="btn-secondary" style="margin-left:auto;padding:4px 12px;border-radius:8px;font-size:.76em">🔄 刷新</button></div><div class="dash-section"><div class="dash-card" style="text-align:center;color:var(--red)">❌ 统计加载失败: ${esc(e.message||'')}<br><span style="font-size:.8em;color:var(--text3)">请确认 Agent 已初始化并有任务记录</span></div></div></div></div></div>`;
    return;
  }
  const hr = (d && d.hit_rates) || {};
  const ctx = (d && d.context) || {estimated_tokens:0, max_tokens:100000, usage_pct:0, compressed:false, summary_length:0, message_count:0};
  const eff = (d && d.efficiency) || {token_efficiency_chars_per_token:null, total_prompt_tokens:0, total_completion_tokens:0, total_output_chars:0};
  const mem = (d && d.memory) || {long_term_docs:0, short_term_msgs:0, kg_entities:0, kg_relations:0};
  const totals = (d && d.totals) || {tool_calls:0, tool_successes:0, plan_goals_total:0, plan_goals_completed:0, tasks_total:0, tasks_success:0};
  const tools = Object.entries((base && base.tool_usage)||{}).slice(0,8).map(([t,n])=>`
    <span class="tag safe" style="margin:3px;display:inline-block">${esc(t)} ×${n}</span>`).join('') || '<em style="color:var(--text3)">暂无工具调用</em>';
  const audit = (base && base.audit) || {ask_user:0, dangerous:0};
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <div class="panel-head"><b>📊 高级统计仪表盘</b>
    <button onclick="loadStatsView()" class="btn-secondary" style="margin-left:auto;padding:4px 12px;border-radius:8px;font-size:.76em">🔄 刷新</button></div>

  <div class="dash-section"><h4>命中率仪表盘</h4>
    <div class="ring-row">
      ${ring('工具命中', hr.tool_hit_rate)}
      ${ring('计划命中', hr.plan_hit_rate)}
      ${ring('任务成功', hr.task_success_rate)}
      ${ring('自我修正', hr.self_correction_rate)}
    </div>
    <div style="text-align:center">${hr.average_hit_rate!=null?`<span class="avg-pill">★ 综合平均命中率 ${hr.average_hit_rate}%</span>`:'<span style="font-size:.82em;color:var(--text3)">暂无足够数据计算命中率</span>'}</div>
  </div>

  <div class="dash-section"><h4>上下文使用率</h4>
    <div class="dash-card">
      <div style="display:flex;justify-content:space-between;font-size:.86em"><span>${ctx.estimated_tokens.toLocaleString()} / ${ctx.max_tokens.toLocaleString()} tokens</span><span class="mv">${ctx.usage_pct}%</span></div>
      <div class="glow-bar"><div class="fill" style="width:${Math.min(ctx.usage_pct,100)}%"></div></div>
      <div style="font-size:.78em;color:var(--text3);margin-top:6px">${ctx.compressed?`⚠ 已触发压缩 · 摘要 ${ctx.summary_length} 字`:'未触发压缩'} · 窗口 ${ctx.message_count} 条消息</div>
    </div>
  </div>

  <div class="dash-section"><h4>效率与用量</h4>
    <div class="dash-card">
      <div class="metric-row"><span>Token 效率</span><span class="mv">${eff.token_efficiency_chars_per_token!=null?eff.token_efficiency_chars_per_token+' 字/Token':'—'}</span></div>
      <div class="metric-row"><span>累计 Token（输入/输出）</span><span class="mv">${eff.total_prompt_tokens.toLocaleString()} / ${eff.total_completion_tokens.toLocaleString()}</span></div>
      <div class="metric-row"><span>总输出字符</span><span class="mv">${eff.total_output_chars.toLocaleString()}</span></div>
      <div class="metric-row"><span>任务 / 成功</span><span class="mv">${totals.tasks_total||0} / ${totals.tasks_success||0}</span></div>
      <div class="metric-row"><span>工具调用 / 成功</span><span class="mv">${totals.tool_calls||0} / ${totals.tool_successes||0}</span></div>
    </div>
  </div>

  <div class="dash-section"><h4>📈 工具命中率趋势（最近 ${hist.count} 次）</h4>
    <div class="dash-card">${sparkline(hist.points)}</div>
  </div>

  <div class="dash-section"><h4>🧠 记忆系统</h4>
    <div class="dash-card">
      <div class="metric-row"><span>向量存储</span><span class="mv">${mem.long_term_docs} docs</span></div>
      <div class="metric-row"><span>知识图谱</span><span class="mv">${mem.kg_entities} 实体 / ${mem.kg_relations} 关系</span></div>
      <div class="metric-row"><span>短期窗口</span><span class="mv">${mem.short_term_msgs} 条消息</span></div>
    </div>
  </div>

  <div class="dash-section"><h4>工具使用 Top</h4><div>${tools}</div>
    <div style="font-size:.78em;color:var(--text3);margin-top:8px">审批请求 ${audit.ask_user||0} · 高危 ${audit.dangerous||0} · 定时任务 ${base&&base.scheduled_tasks||0}
      <button onclick="fetch('${API}/tokens',{method:'DELETE'}).then(()=>loadStatsView())" class="btn-secondary" style="float:right;padding:3px 9px;border-radius:6px;font-size:.92em">重置Token</button></div>
  </div>
</div></div></div>`;
}

// ── 定时任务 ──
async function loadScheduleView() {
  const list = await (await fetch(`${API}/schedule`)).json();
  const rows = list.length ? list.map(s=>`
    <div class="card ${s.enabled?'lt-green':''}">
      <b>${esc(s.name)}</b>
      <span class="tag safe">${MODE_LABELS[s.interaction]||s.interaction}</span>
      <span style="float:right">
        <button class="btn-secondary" style="padding:3px 9px;font-size:.74em;border-radius:6px" onclick="runSchedule('${jsq(s.id)}')">立即运行</button>
        <button class="btn-secondary" style="padding:3px 9px;font-size:.74em;border-radius:6px" onclick="toggleSchedule('${jsq(s.id)}',${!s.enabled})">${s.enabled?'暂停':'启用'}</button>
        <button class="btn-danger" style="padding:3px 9px;font-size:.74em;border-radius:6px" onclick="delSchedule('${jsq(s.id)}')">删除</button>
      </span>
      <div style="font-size:.8em;color:var(--text2);margin-top:5px">${esc(s.task.slice(0,80))}</div>
      <div style="font-size:.76em;color:var(--text3);margin-top:3px">每 ${fmtInterval(s.interval)} · 已运行 ${s.runs} 次 ${s.last_status?'· '+esc(s.last_status):''} ${s.enabled&&s.next_in!=null?'· 下次 '+fmtInterval(s.next_in)+'后':''}</div>
    </div>`).join('') : '<em style="color:var(--text3)">暂无定时任务</em>';
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>⏰ 定时任务</b>
  <div class="card" style="background:var(--bg1);margin-top:10px">
    <input type="text" id="sch-name" placeholder="名称（可选）" style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.85em;margin-bottom:8px">
    <textarea id="sch-task" placeholder="要定时执行的任务内容..." style="width:100%;min-height:54px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.85em;resize:vertical;font-family:var(--font)"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
      <select id="sch-mode" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.84em">
        <option value="chat">对话</option><option value="work">工作</option>
        <option value="coding">编程</option><option value="loop">循环</option><option value="multi">协同</option>
      </select>
      <select id="sch-interval" style="padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg0);color:var(--text);font-size:.84em">
        <option value="300">每5分钟</option><option value="3600" selected>每小时</option>
        <option value="21600">每6小时</option><option value="86400">每天</option>
      </select>
      <button class="btn-primary" style="padding:8px 16px" onclick="addSchedule()">添加</button>
    </div>
  </div>
  <div style="margin-top:10px">${rows}</div>
</div></div></div>`;
}
function fmtInterval(s){ s=+s; if(s>=86400)return Math.round(s/86400)+'天'; if(s>=3600)return Math.round(s/3600)+'小时'; if(s>=60)return Math.round(s/60)+'分钟'; return s+'秒'; }
async function addSchedule(){
  const task=document.getElementById('sch-task').value.trim();
  if(!task) return toast('请输入任务内容','error');
  await fetch(`${API}/schedule`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    name:document.getElementById('sch-name').value.trim(), task,
    interaction:document.getElementById('sch-mode').value,
    interval:parseInt(document.getElementById('sch-interval').value)})});
  toast('定时任务已添加','success'); loadScheduleView();
}
async function runSchedule(id){ await fetch(`${API}/schedule/${id}/run`,{method:'POST'}); toast('已触发运行','info'); }
async function toggleSchedule(id,en){ await fetch(`${API}/schedule/${id}/toggle`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})}); loadScheduleView(); }
async function delSchedule(id){ await fetch(`${API}/schedule/${id}`,{method:'DELETE'}); loadScheduleView(); toast('已删除','info'); }

let toolsTab = 'tools';
async function loadToolsView(tab) {
  toolsTab = tab || toolsTab;
  // 拉取各分类数量用于角标
  let counts = {tools:0, skills:0, mcp:0, plugins:0};
  try {
    const [t,s,m,p] = await Promise.all([
      fetch(`${API}/tools`).then(r=>r.json()),
      fetch(`${API}/skills`).then(r=>r.json()),
      fetch(`${API}/mcp`).then(r=>r.json()),
      fetch(`${API}/plugins`).then(r=>r.json()),
    ]);
    counts = {tools: t.length, skills: s.length, mcp: (m.servers||[]).length, plugins: (p.plugins||[]).length};
  } catch(e) {}
  const segs = [
    ['tools','🔧','工具', counts.tools],
    ['skills','✨','技能', counts.skills],
    ['mcp','🔌','MCP', counts.mcp],
    ['plugins','🧩','插件', counts.plugins],
  ];
  const bar = `<div class="seg-tabs">
    ${segs.map(([k,ic,l,n])=>`
      <button class="seg ${k===toolsTab?'active':''}" onclick="loadToolsView('${k}')">
        <span class="seg-ic">${ic}</span><span class="seg-label">${l}</span>
        <span class="seg-badge">${n}</span>
      </button>`).join('')}
  </div>`;
  let body = '';
  if (toolsTab === 'tools') body = await renderToolsList();
  else if (toolsTab === 'skills') body = await renderSkillsList();
  else if (toolsTab === 'mcp') body = await renderMCPList();
  else if (toolsTab === 'plugins') body = await renderPluginsList();
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  ${bar}${body}
</div></div></div>`;
}
const TOOL_ICONS = {terminal:'⌨️', file_read:'📖', file_write:'✍️', file_edit:'✏️', python_sandbox:'🐍', browser:'🌐', web_fetch:'🔗'};
async function renderToolsList() {
  const tools = await (await fetch(`${API}/tools`)).json();
  const enabled = tools.filter(t=>t.enabled).length;
  return `<div class="panel-head"><b>🔧 可用工具</b><span class="count-pill">${enabled}/${tools.length} 启用</span></div>
  <div class="hint" style="margin:2px 0 12px">关闭开关可临时禁用某工具（Agent 执行时将不可调用）。</div>
  <div class="tool-grid">
  ${tools.map(t => `
    <div class="tool-card ${t.enabled?'':'off'}">
      <div class="tool-icon">${TOOL_ICONS[t.name]||(t.mcp?'🔌':'🛠')}</div>
      <div class="tool-main">
        <div class="tool-title">${esc(t.name)}
          <span class="tag ${t.tier}">${t.tier}</span>
          <span class="risk-dot" title="风险 ${t.risk}" style="background:${t.risk>=80?'var(--red)':t.risk>=40?'var(--yellow)':'var(--green)'}"></span>
        </div>
        <div class="tool-desc">${esc(t.description)}</div>
        ${(t.params||[]).length?`<div class="tool-params">参数: ${t.params.map(p=>`<code>${esc(p)}</code>`).join(' ')}</div>`:''}
      </div>
      <label class="switch" title="${t.enabled?'点击禁用':'点击启用'}">
        <input type="checkbox" ${t.enabled?'checked':''} onchange="toggleTool('${esc(t.name)}', this.checked)">
        <span class="slider"></span>
      </label>
    </div>`).join('')}
  </div>`;
}
async function toggleTool(name, enabled){
  await fetch(`${API}/tools/toggle`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, enabled})});
  toast(enabled?`已启用 ${name}`:`已禁用 ${name}`, enabled?'success':'info');
  loadToolsView('tools');
}
async function renderSkillsList() {
  const skills = await (await fetch(`${API}/skills`)).json();
  const custom = skills.filter(s=>!s.builtin).length;
  const mdCount = skills.filter(s=>s.type==='markdown').length;
  return `<div class="panel-head"><b>✨ 技能库</b><span class="count-pill">${skills.length} 个 · ${custom} 自定义 · ${mdCount} SKILL.md</span></div>
  <div class="add-box">
    <div class="add-box-title">➕ 添加技能</div>
    <div class="hint" style="margin-bottom:8px">支持 <b>SKILL.md 技能包</b>（文件夹含 <code>SKILL.md</code>）与 <b>.py 技能</b>（含 <code>AbstractSkill</code> 子类）。</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <input type="text" id="skill-dir" placeholder="技能目录，如 C:\\Users\\you\\Desktop\\skills" style="flex:1;min-width:180px;padding:9px 11px;border:1px solid var(--border);border-radius:8px;background:var(--bg0);color:var(--text);font-size:.85em">
      <button class="btn-primary" style="padding:9px 16px" onclick="loadSkillDir()">📁 加载目录</button>
      <input type="file" id="skill-file" accept=".py" style="display:none" onchange="importSkillFile(event)">
      <button class="btn-secondary" style="padding:9px 16px" onclick="document.getElementById('skill-file').click()">📄 导入 .py</button>
    </div>
    <button class="btn-secondary" style="margin-top:8px;padding:8px 14px;width:100%" onclick="importDesktopSkills()">⬇️ 一键导入桌面 skills 文件夹</button>
  </div>
  <div class="card-list">
  ${skills.map(s => {
    const isMd = s.type==='markdown';
    const tag = s.builtin?'<span class="tag safe">内置</span>'
      : isMd?'<span class="tag" style="background:var(--purple-bg);color:var(--purple)">SKILL.md</span>'
      : '<span class="tag sensitive">Python</span>';
    const reqs = (s.required_tools||[]).length?`<div style="font-size:.74em;color:var(--text3);margin-top:3px">依赖工具: ${s.required_tools.map(t=>esc(t)).join(', ')}</div>`:'';
    return `
    <div class="item-card ${s.builtin?'lt-accent':isMd?'lt-purple':'lt-yellow'}">
      <div class="skill-emoji">${esc(s.emoji||'✨')}</div>
      <div class="item-main">
        <div class="item-title">${esc(s.name)} ${tag}</div>
        <div class="item-desc">${esc(s.description||'(无描述)')}</div>
        ${reqs}
      </div>
      ${s.builtin?'':`<button class="icon-del" title="删除技能" onclick="delSkill('${jsq(s.name)}')">🗑</button>`}
    </div>`;}).join('')}
  </div>`;
}
async function importDesktopSkills(){
  // 跨平台：交由后端 expanduser 解析当前用户主目录，不再硬编码具体用户名
  const candidates = ['~/Desktop/skills', '~/桌面/skills', '~/skills'];
  toast('正在导入桌面 skills 文件夹...', 'info');
  let done = false;
  for (const directory of candidates) {
    const r = await (await fetch(`${API}/skills/load`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ directory }),
    })).json();
    if (!r.error) { toast(`已导入 ${r.loaded} 个技能（SKILL.md ${r.markdown||0} · Python ${r.py||0}）`, 'success'); done = true; break; }
  }
  if (!done) toast('未找到桌面 skills 文件夹，请用「加载目录」手动指定', 'error');
  loadToolsView('skills');
}
async function loadSkillDir() {
  const directory = document.getElementById('skill-dir').value.trim();
  if (!directory) return toast('请输入技能目录', 'error');
  const r = await (await fetch(`${API}/skills/load`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ directory }),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已加载 ${r.loaded} 个技能（共 ${r.total}）`, r.loaded?'success':'info');
  loadToolsView('skills');
}
async function importSkillFile(ev){
  const file = ev.target.files[0]; if(!file) return;
  const code = await file.text();
  const r = await (await fetch(`${API}/skills/import`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name: file.name, code }),
  })).json();
  ev.target.value='';
  if (r.error) return toast(r.error, 'error');
  toast(`已导入技能: ${(r.imported||[]).join(', ')}`, 'success');
  loadToolsView('skills');
}
async function delSkill(name){
  if(!confirm(`确定删除技能「${name}」？`)) return;
  const r = await (await fetch(`${API}/skills/${encodeURIComponent(name)}`, {method:'DELETE'})).json();
  if (r.error) return toast(r.error, 'error');
  toast('已删除 '+name, 'info');
  loadToolsView('skills');
}
async function renderMCPList() {
  const data = await (await fetch(`${API}/mcp`)).json();
  const sdkWarn = data.sdk_installed ? '' :
    `<div class="item-card lt-yellow" style="font-size:.82em">⚠ 未检测到 MCP SDK，服务器可保存但无法连接。请先执行 <code>pip install mcp</code>。</div>`;
  const importBox = `
  <div class="add-box">
    <div class="add-box-title">📥 批量导入 (Claude Desktop 格式)</div>
    <div class="hint" style="margin-bottom:6px">粘贴 <code>{"mcpServers": {...}}</code> 配置，或导入 JSON 文件。</div>
    <textarea id="mcp-import" placeholder='{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}' class="mcp-import-ta"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
      <input type="file" id="mcp-file" accept=".json" style="display:none" onchange="importMCPFile(event)">
      <button class="btn-secondary" style="padding:7px 14px" onclick="document.getElementById('mcp-file').click()">📄 选择文件</button>
      <span style="flex:1"></span>
      <button class="btn-primary" style="padding:7px 16px" onclick="importMCP()">导入并连接</button>
    </div>
  </div>`;
  const form = `
  <div class="add-box">
    <div class="add-box-title">➕ 添加单个 MCP 服务器</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <input type="text" id="mcp-name" placeholder="名称 (如 filesystem)" class="mcp-in">
      <select id="mcp-transport" class="mcp-in" onchange="toggleMcpTransport()">
        <option value="stdio">stdio (本地命令)</option>
        <option value="sse">sse (URL)</option>
      </select>
    </div>
    <div id="mcp-stdio" style="display:grid;grid-template-columns:1fr 2fr;gap:8px;margin-top:8px">
      <input type="text" id="mcp-command" placeholder="命令 (如 npx)" class="mcp-in">
      <input type="text" id="mcp-args" placeholder="参数 (空格分隔)" class="mcp-in">
    </div>
    <div id="mcp-sse" style="display:none;margin-top:8px">
      <input type="text" id="mcp-url" placeholder="SSE URL (如 http://localhost:3000/sse)" class="mcp-in" style="width:100%">
    </div>
    <div style="text-align:right;margin-top:10px">
      <button class="btn-primary" style="padding:8px 16px" onclick="addMCP()">添加并连接</button>
    </div>
  </div>`;
  const list = (data.servers||[]).length ? `<div class="card-list">${data.servers.map(s => `
    <div class="item-card ${s.connected?'lt-green':'lt-red'}">
      <div class="item-main">
        <div class="item-title">🔌 ${esc(s.name)} <span class="tag ${s.connected?'safe':'dangerous'}">${s.connected?'已连接':'未连接'}</span></div>
        <div class="item-desc" style="font-family:var(--mono);font-size:.78em">${esc(s.transport)} · ${esc(s.command||s.url)} ${esc((s.args||[]).join(' '))}</div>
        ${s.tools.length?`<div style="font-size:.8em;color:var(--text2);margin-top:3px">工具: ${s.tools.map(t=>esc(t)).join(', ')}</div>`:''}
      </div>
      <button class="icon-del" title="删除服务器" onclick="delMCP('${jsq(s.name)}')">🗑</button>
    </div>`).join('')}</div>` : '<em style="color:var(--text3)">暂无 MCP 服务器</em>';
  return `<div class="panel-head"><b>🔌 MCP 服务器</b><span class="count-pill">${(data.servers||[]).length} 个</span></div>${sdkWarn}${importBox}${form}${list}`;
}
async function renderPluginsList() {
  const data = await (await fetch(`${API}/plugins`)).json();
  const plugins = data.plugins || [];
  const loaded = plugins.filter(p=>p.loaded).length;
  const infoBox = `
  <div class="add-box">
    <div class="add-box-title">🧩 插件目录</div>
    <div class="hint" style="margin-bottom:8px">把插件放在 <code>~/.automind/plugins/&lt;名称&gt;/</code> 下，每个插件包含 <code>plugin.json</code>（元信息）与 <code>hooks.py</code>（提供 <code>get_hooks() → AgentHooks</code>）。加载后其生命周期钩子（任务开始/结束、解析后、计划后、出错等）将自动生效。</div>
    <div style="text-align:right"><button class="btn-secondary" style="padding:7px 14px" onclick="loadToolsView('plugins')">🔄 重新扫描</button></div>
  </div>`;
  const list = plugins.length ? `<div class="card-list">${plugins.map(p => `
    <div class="item-card ${p.loaded?'lt-green':'lt-yellow'}">
      <div class="item-main">
        <div class="item-title">🧩 ${esc(p.name)} <span class="tag ${p.loaded?'safe':'sensitive'}">${p.loaded?'已加载':'未加载'}</span>
          ${p.version?`<span style="font-size:.72em;color:var(--text3);margin-left:4px">v${esc(p.version)}</span>`:''}</div>
        <div class="item-desc">${esc(p.description||'(无描述)')}</div>
        ${p.author?`<div style="font-size:.74em;color:var(--text3);margin-top:3px">作者: ${esc(p.author)}</div>`:''}
      </div>
      ${p.loaded
        ? `<button class="btn-secondary" style="padding:6px 14px" onclick="unloadPlugin('${jsq(p.name)}')">卸载</button>`
        : `<button class="btn-primary" style="padding:6px 14px" onclick="loadPlugin('${jsq(p.name)}')">加载</button>`}
    </div>`).join('')}</div>`
    : '<em style="color:var(--text3)">未发现插件。请在 <code>~/.automind/plugins</code> 下放置插件目录后点击「重新扫描」。</em>';
  return `<div class="panel-head"><b>🧩 插件</b><span class="count-pill">${loaded}/${plugins.length} 已加载</span></div>${infoBox}${list}`;
}
async function loadPlugin(name){
  const r = await (await fetch(`${API}/plugins/${encodeURIComponent(name)}/load`, {method:'POST'})).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已加载插件 ${name}`, 'success');
  loadToolsView('plugins');
}
async function unloadPlugin(name){
  await fetch(`${API}/plugins/${encodeURIComponent(name)}/unload`, {method:'POST'});
  toast(`已卸载插件 ${name}`, 'info');
  loadToolsView('plugins');
}
function toggleMcpTransport() {
  const t = document.getElementById('mcp-transport').value;
  document.getElementById('mcp-stdio').style.display = t==='stdio'?'grid':'none';
  document.getElementById('mcp-sse').style.display = t==='sse'?'block':'none';
}
async function addMCP() {
  const body = {
    name: document.getElementById('mcp-name').value.trim(),
    transport: document.getElementById('mcp-transport').value,
    command: document.getElementById('mcp-command').value.trim(),
    args: document.getElementById('mcp-args').value.trim(),
    url: document.getElementById('mcp-url').value.trim(),
  };
  if (!body.name) return toast('请输入服务器名称', 'error');
  const r = await (await fetch(`${API}/mcp`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body),
  })).json();
  if (r.error && !r.status) return toast(r.error, 'error');
  toast(r.connected ? 'MCP 已连接' : ('已保存，但'+(r.error||'未连接')), r.connected?'success':'info');
  loadToolsView('mcp');
}
async function importMCP() {
  const raw = document.getElementById('mcp-import').value.trim();
  if (!raw) return toast('请粘贴 MCP 配置', 'error');
  let cfg; try { cfg = JSON.parse(raw); } catch(e){ return toast('JSON 解析失败: '+e.message, 'error'); }
  const r = await (await fetch(`${API}/mcp/import`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({config: cfg}),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast(`已导入 ${r.imported} 个服务器${r.connected_any?'（部分已连接）':''}`, 'success');
  loadToolsView('mcp');
}
async function importMCPFile(ev){
  const file = ev.target.files[0]; if(!file) return;
  document.getElementById('mcp-import').value = await file.text();
  ev.target.value='';
  toast('已读取文件，点击「导入并连接」', 'info');
}
async function delMCP(name) {
  if(!confirm(`确定删除 MCP 服务器「${name}」？`)) return;
  await fetch(`${API}/mcp/${encodeURIComponent(name)}`, {method:'DELETE'});
  toast('已删除 '+name, 'info');
  loadToolsView('mcp');
}
async function loadHistoryView() {
  const history = await (await fetch(`${API}/history`)).json();
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>📜 任务历史 (${history.length})</b>
  <button onclick="clearHistory()" class="btn-danger" style="float:right;padding:4px 10px;border-radius:6px;font-size:.78em">清空</button>
  <br>
  ${history.length === 0 ? '<em style="color:var(--text3)">暂无历史记录</em>' :
    history.slice().reverse().map(h => `
    <div class="card ${h.success?'lt-green':'lt-red'}">
      <button class="btn-danger" style="float:right;padding:2px 9px;font-size:.74em;border-radius:6px" onclick="delHistory('${jsq(h.session_id)}')">删除</button>
      <div style="font-weight:500;padding-right:50px">${esc((h.task||'').slice(0,120))}</div>
      <div style="font-size:.78em;color:var(--text3);margin-top:3px">
        ${({chat:'💬对话',work:'⚙️工作',coding:'💻编程',multi:'🤝协同',loop:'🔁循环'})[h.interaction]||''}${h.scheduled?' ⏰':''}
        · ${h.steps}步 · ${h.tokens}tk · ${h.duration_ms}ms · ${h.session_id}
      </div>
      <div style="font-size:.82em;color:var(--text2);margin-top:4px;max-height:60px;overflow:hidden">${esc((h.output||'').slice(0,200))}</div>
    </div>`).join('')}
</div></div></div>`;
}
function loadPlanView() {
  const planEl = document.getElementById('plan-view');
  document.getElementById('messages').innerHTML = `
<div class="msg agent"><div class="avatar">AM</div><div class="col" style="max-width:100%"><div class="bubble" style="max-width:100%">
  <b>📋 最近执行计划</b><br><br>${planEl.innerHTML}
</div></div></div>`;
}

>>>>>>> f7b98f9b6ecabf8d800f9c0521948f7f5db79dbc
