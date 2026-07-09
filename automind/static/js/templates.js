// ── 模板库（社区版内置 10 个基础模板）+ 新手引导 ──
// 模板：一键填入任务描述并切到合适的模式，让新用户"打开就知道能做什么"。

const TEMPLATES = [
  { icon: '🌐', title: '个人主页', mode: 'coding',
    desc: '生成一个漂亮的响应式个人主页',
    prompt: '帮我生成一个响应式的个人主页 index.html：深色科技风，包含姓名标语、关于我、技能列表、项目卡片和联系方式区块，CSS 内联在文件里，不依赖外部库。' },
  { icon: '📦', title: '项目脚手架', mode: 'work',
    desc: '创建一个规范的 Python 项目骨架',
    prompt: '在当前目录创建一个 FastAPI 项目骨架：包含 app/main.py（含 /health 健康检查）、requirements.txt、README.md、tests/ 目录和一个示例测试，创建后运行测试验证。' },
  { icon: '🐍', title: '实用脚本', mode: 'coding',
    desc: '写一个整理文件的 Python 脚本',
    prompt: '写一个 Python 脚本 organize.py：把指定目录下的文件按扩展名分类移动到 images/、docs/、videos/、others/ 子目录，带 --dry-run 预览模式和执行日志。' },
  { icon: '🔧', title: '修复报错', mode: 'coding',
    desc: '粘贴报错信息，自动定位并修复',
    prompt: '我的代码报错了，请阅读相关文件、定位原因并修复，修复后运行验证。报错信息如下：\n（把报错粘贴到这里）' },
  { icon: '🧪', title: '补单元测试', mode: 'coding',
    desc: '为现有代码生成 pytest 测试',
    prompt: '阅读项目中的核心模块，为主要函数补充 pytest 单元测试（含边界与异常用例），放到 tests/ 目录，写完运行确认全部通过。' },
  { icon: '📄', title: '生成 README', mode: 'coding',
    desc: '扫描项目自动写文档',
    prompt: '阅读当前项目的代码结构与入口文件，生成一份专业的 README.md：项目简介、功能列表、安装步骤、使用示例和目录结构说明。' },
  { icon: '📊', title: '数据分析', mode: 'work',
    desc: '分析数据文件并产出报告',
    prompt: '分析当前目录下的数据文件（CSV/Excel），统计关键指标与分布，生成一份带结论的分析报告 report.html（内嵌图表，可直接在浏览器打开）。' },
  { icon: '🕷️', title: '网页抓取', mode: 'coding',
    desc: '写爬虫抓取网页数据',
    prompt: '写一个 Python 爬虫脚本：抓取指定网页的标题和正文要点，保存为 JSON 文件，带请求间隔与异常重试。目标网址：\n（把网址粘贴到这里）' },
  { icon: '📝', title: '写周报', mode: 'chat',
    desc: '把要点整理成一篇周报',
    prompt: '把下面的工作要点整理成一篇结构清晰的周报（本周完成 / 数据与结果 / 问题与风险 / 下周计划）：\n（把要点粘贴到这里）' },
  { icon: '🌍', title: '翻译润色', mode: 'chat',
    desc: '中英互译并润色表达',
    prompt: '把下面的内容翻译成地道的英文（保留专业术语准确性），并在末尾附一版更精炼的表达建议：\n（把内容粘贴到这里）' },
];
window.TEMPLATES = TEMPLATES;  // 供 core.js renderWelcome 的快速开始区引用

async function showTemplates() {
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  overlay.classList.add('show');
  content.innerHTML = `
<h2>📚 模板库</h2>
<div class="hint">选一个模板快速开始 — 点击后自动切换模式并填入任务描述，你只需补充细节再发送。</div>
<div class="tpl-grid">
  ${TEMPLATES.map((t, i) => `
  <div class="tpl-card" onclick="useTemplate(${i})">
    <div class="tpl-icon">${t.icon}</div>
    <div style="flex:1;min-width:0">
      <b>${esc(t.title)}</b>
      <span class="tag" style="margin-left:6px;font-size:.68em">${({chat:'💬对话',work:'⚙️工作',coding:'💻编程'})[t.mode]||t.mode}</span>
      <div style="font-size:.78em;color:var(--text3);margin-top:3px">${esc(t.desc)}</div>
    </div>
  </div>`).join('')}
</div>
<div id="custom-tpl-box" style="margin-top:16px;border-top:1px solid var(--border);padding-top:12px"></div>`;
  renderCustomTemplates();
}

// ── 自定义模板（专业版特性 custom_templates）──
let _customTpls = [];
async function renderCustomTemplates() {
  const box = document.getElementById('custom-tpl-box');
  if (!box) return;
  if (!featureOn('custom_templates')) {
    box.innerHTML = `<b style="font-size:.92em">⭐ 我的模板 <span style="font-size:.8em;color:var(--text3)">🔒 专业版</span></b>
      <div class="hint" style="margin-top:6px">专业版可把常用任务保存为自定义模板（最多 100 个），团队沉淀提示词资产。
      <a href="javascript:void(0)" onclick="upgradeToast('自定义模板')" style="color:var(--accent)">了解升级 →</a></div>`;
    return;
  }
  try { _customTpls = (await (await fetch(`${API}/templates/custom`)).json()).templates || []; }
  catch (e) { _customTpls = []; }
  box.innerHTML = `
<b style="font-size:.92em">⭐ 我的模板（${_customTpls.length}）</b>
${_customTpls.length ? `<div class="tpl-grid" style="margin-top:10px">
  ${_customTpls.map((t, i) => `
  <div class="tpl-card" onclick="useCustomTemplate(${i})">
    <div class="tpl-icon">${esc(t.icon || '⭐')}</div>
    <div style="flex:1;min-width:0">
      <b>${esc(t.title)}</b>
      <span class="tag" style="margin-left:6px;font-size:.68em">${({chat:'💬对话',work:'⚙️工作',coding:'💻编程',multi:'🤝协同',loop:'🔁循环'})[t.mode]||t.mode}</span>
      <div style="font-size:.78em;color:var(--text3);margin-top:3px">${esc(t.desc || '')}</div>
    </div>
    <button class="icon-del" title="删除模板" onclick="event.stopPropagation();deleteCustomTemplate('${jsq(t.id)}')">✕</button>
  </div>`).join('')}
</div>` : '<div class="hint" style="margin-top:6px">还没有自定义模板 — 在下方创建第一个。</div>'}
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
  <input type="text" id="ctpl-icon" placeholder="图标" style="width:56px" maxlength="4" value="⭐">
  <input type="text" id="ctpl-title" placeholder="模板名称" style="width:150px" maxlength="40">
  <select id="ctpl-mode" style="padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg0);color:var(--text)">
    <option value="work">⚙️ 工作</option><option value="coding">💻 编程</option><option value="chat">💬 对话</option>
  </select>
  <button class="btn-secondary" style="padding:0 12px;white-space:nowrap" onclick="fillCustomTplFromInput()" title="把当前输入框内容作为模板提示词">📥 取输入框</button>
  <button class="btn-primary" style="padding:0 16px;white-space:nowrap" onclick="saveCustomTemplate()">保存模板</button>
</div>
<textarea id="ctpl-prompt" placeholder="模板提示词（任务描述，可含「（把 XX 粘贴到这里）」占位）" rows="3"
  style="width:100%;margin-top:8px;resize:vertical"></textarea>`;
}
function fillCustomTplFromInput() {
  const v = document.getElementById('user-input').value.trim();
  if (!v) return toast('输入框是空的', 'error');
  document.getElementById('ctpl-prompt').value = v;
}
async function saveCustomTemplate() {
  const body = {
    icon: document.getElementById('ctpl-icon').value.trim() || '⭐',
    title: document.getElementById('ctpl-title').value.trim(),
    mode: document.getElementById('ctpl-mode').value,
    prompt: document.getElementById('ctpl-prompt').value.trim(),
  };
  if (!body.title || !body.prompt) return toast('模板名称与提示词必填', 'error');
  const r = await (await fetch(`${API}/templates/custom`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })).json();
  if (r.error) return toast(r.error, 'error');
  toast(`模板「${body.title}」已保存`, 'success');
  renderCustomTemplates();
}
async function deleteCustomTemplate(id) {
  if (!confirm('删除该自定义模板？')) return;
  await fetch(`${API}/templates/custom/${encodeURIComponent(id)}`, { method: 'DELETE' });
  toast('已删除', 'info');
  renderCustomTemplates();
}
async function useCustomTemplate(i) {
  const t = _customTpls[i];
  if (!t) return;
  closeModal();
  if (t.mode !== currentMode && featureOn(MODE_FEATURE[t.mode])) await setMode(t.mode);
  const input = document.getElementById('user-input');
  input.value = t.prompt;
  input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  input.focus();
  toast('模板已填入，补充细节后按 Enter 发送', 'info');
}

async function useTemplate(i) {
  const t = TEMPLATES[i];
  if (!t) return;
  closeModal();
  if (t.mode !== currentMode) await setMode(t.mode);
  const input = document.getElementById('user-input');
  input.value = t.prompt;
  input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  input.focus();
  toast('模板已填入，补充细节后按 Enter 发送', 'info');
}

// ── 新手引导（首次打开自动显示；❓ 按钮可随时重看）──
const TOUR_STEPS = [
  { icon: '🔑', title: '第 1 步 · 配置模型',
    body: '点击右上角 <b>「🔑 API Keys」</b>，为你使用的模型提供商填入 API Key 并「测试连接」。<br><br>支持 OpenAI / Claude / DeepSeek / Kimi / 智谱 / 豆包 / Gemini / 本地 Ollama，以及任意 OpenAI 兼容中转代理。' },
  { icon: '🧭', title: '第 2 步 · 选择模式',
    body: '顶部三个主模式：<br>• 💬 <b>对话</b> — 问答聊天，不动你的文件<br>• ⚙️ <b>工作</b> — 自动规划并执行任务（建项目、跑命令）<br>• 💻 <b>编程</b> — 读代码 → 改代码 → 跑测试闭环<br><br>不确定选哪个？从 💬 对话开始最安全。' },
  { icon: '📚', title: '第 3 步 · 从模板开始',
    body: '不知道能做什么？点击 <b>「📚 模板」</b>，内置 10 个常用模板（个人主页、修 Bug、写脚本、数据报告…），一键填入即可开跑。' },
  { icon: '🛡️', title: '放心使用',
    body: '• 每次工具调用都经过<b>风险评估与审批</b>，高危操作必须你确认<br>• Agent 改过的文件可在右栏 <b>「↩️ 文件改动」</b>一键撤销回滚<br>• 任务历史自动保存（📜），关掉浏览器也不会丢<br>• 全部数据只存在你自己的电脑上' },
];
let _tourIdx = 0;

function showTour(fromStart) {
  if (fromStart !== false) _tourIdx = 0;
  const overlay = document.getElementById('settings-modal');
  const content = document.getElementById('settings-content');
  overlay.classList.add('show');
  renderTourStep();
}
function renderTourStep() {
  const s = TOUR_STEPS[_tourIdx];
  const content = document.getElementById('settings-content');
  content.innerHTML = `
<div style="text-align:center;padding:8px 4px">
  <div style="font-size:2.6em">${s.icon}</div>
  <h2 style="margin:10px 0 4px">${s.title}</h2>
  <div style="font-size:.9em;color:var(--text2);line-height:1.8;text-align:left;margin:14px 0">${s.body}</div>
  <div style="display:flex;justify-content:center;gap:6px;margin:12px 0">
    ${TOUR_STEPS.map((_, i) => `<span style="width:8px;height:8px;border-radius:50%;background:${i === _tourIdx ? 'var(--accent)' : 'var(--bg3)'}"></span>`).join('')}
  </div>
  <div class="btn-row" style="justify-content:center">
    ${_tourIdx > 0 ? '<button class="btn-secondary" onclick="tourPrev()">上一步</button>' : ''}
    ${_tourIdx < TOUR_STEPS.length - 1
      ? '<button class="btn-primary" onclick="tourNext()">下一步</button>'
      : '<button class="btn-primary" onclick="finishTour()">开始使用 🚀</button>'}
  </div>
  <div style="margin-top:10px"><a href="javascript:void(0)" onclick="finishTour()" style="font-size:.78em;color:var(--text3)">跳过引导</a></div>
</div>`;
}
function tourNext() { if (_tourIdx < TOUR_STEPS.length - 1) { _tourIdx++; renderTourStep(); } }
function tourPrev() { if (_tourIdx > 0) { _tourIdx--; renderTourStep(); } }
function finishTour() {
  localStorage.setItem('automind_onboarded', '1');
  closeModal();
  toast('随时点右上角 ❓ 重看引导，📚 查看模板', 'info');
}

// 首次访问自动弹引导
document.addEventListener('DOMContentLoaded', () => {
  if (!localStorage.getItem('automind_onboarded')) setTimeout(() => showTour(), 600);
});
