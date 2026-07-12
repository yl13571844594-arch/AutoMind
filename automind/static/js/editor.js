// ── 📄 代码标签（Web IDE）— 文件树 + Monaco 编辑器 + Diff 预览 ──
// Monaco 从 jsDelivr 按需加载（CSP 已放行）；离线时回退为轻量 textarea，
// 功能（打开/编辑/保存/撤销记录）不受影响。保存走 /api/files/write，
// 前像记入改动日志 → 右栏「文件改动」可一键撤销。

const MONACO_CDN = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min';
let _monacoState = 'idle';   // idle | loading | ready | failed
let _monacoQueue = [];
let _editor = null;          // monaco 编辑器实例（或 null=回退 textarea）
let _diffEditor = null;
let _openedPath = '';
let _openedContent = '';
let _diffMode = false;
let _rightTab = 'panel';

const _LANG_BY_EXT = { py:'python', js:'javascript', ts:'typescript', jsx:'javascript',
  tsx:'typescript', json:'json', html:'html', htm:'html', css:'css', md:'markdown',
  yml:'yaml', yaml:'yaml', toml:'ini', sh:'shell', ps1:'powershell', bat:'bat',
  sql:'sql', xml:'xml', txt:'plaintext', csv:'plaintext', go:'go', rs:'rust',
  java:'java', c:'c', h:'c', cpp:'cpp', cs:'csharp', rb:'ruby', php:'php' };

function _codeLang(path){ return _LANG_BY_EXT[(path.split('.').pop()||'').toLowerCase()] || 'plaintext'; }
function _monacoTheme(){ return document.body.classList.contains('light') ? 'vs' : 'vs-dark'; }

// ── 右栏标签切换 ──
function switchRightTab(tab){
  _rightTab = tab;
  document.getElementById('rp-tab-panel').classList.toggle('active', tab==='panel');
  document.getElementById('rp-tab-code').classList.toggle('active', tab==='code');
  document.getElementById('rp-panel-view').style.display = tab==='panel' ? '' : 'none';
  document.getElementById('rp-code-view').style.display = tab==='code' ? 'flex' : 'none';
  document.getElementById('right-panel').classList.toggle('code-wide', tab==='code');
  if (tab === 'code') { refreshFileTree(); if (_editor && _editor.layout) setTimeout(()=>_editor.layout(), 60); }
}

// ── Monaco 按需加载（worker 走 blob 代理，跨域标准做法）──
function loadMonaco(cb){
  if (_monacoState === 'ready') return cb(true);
  if (_monacoState === 'failed') return cb(false);
  _monacoQueue.push(cb);
  if (_monacoState === 'loading') return;
  _monacoState = 'loading';
  const s = document.createElement('script');
  s.src = MONACO_CDN + '/vs/loader.js';
  s.onload = () => {
    try {
      window.require.config({ paths: { vs: MONACO_CDN + '/vs' } });
      window.MonacoEnvironment = { getWorkerUrl: () => URL.createObjectURL(new Blob(
        [`self.MonacoEnvironment={baseUrl:'${MONACO_CDN}/'};importScripts('${MONACO_CDN}/vs/base/worker/workerMain.js');`],
        { type: 'text/javascript' })) };
      window.require(['vs/editor/editor.main'],
        () => { _monacoState = 'ready'; _monacoQueue.splice(0).forEach(f => f(true)); },
        () => { _monacoState = 'failed'; _monacoQueue.splice(0).forEach(f => f(false)); });
    } catch(e) { _monacoState = 'failed'; _monacoQueue.splice(0).forEach(f => f(false)); }
  };
  s.onerror = () => { _monacoState = 'failed'; _monacoQueue.splice(0).forEach(f => f(false)); };
  document.head.appendChild(s);
}

// ── 文件树 ──
async function refreshFileTree(){
  const el = document.getElementById('file-tree');
  try {
    const r = await (await fetch(`${API}/files/tree?limit=600`)).json();
    const entries = r.entries || [];
    if (!entries.length) { el.innerHTML = '<em style="color:var(--text3);font-size:.8em;padding:8px;display:block">项目目录为空</em>'; return; }
    el.innerHTML = entries.map(e => {
      const name = e.path.split('/').pop();
      const pad = 8 + e.level * 14;
      if (e.dir) return `<div class="ft-item ft-dir" data-dir="${esc(e.path)}" style="padding-left:${pad}px"
        onclick="toggleTreeDir(this)"><span class="ft-caret">▾</span> 📁 ${esc(name)}</div>`;
      return `<div class="ft-item ft-file" data-path="${esc(e.path)}" style="padding-left:${pad}px"
        onclick="openCodeFile('${jsq(e.path)}', this)" title="${esc(e.path)}">${_fileIcon(name)} ${esc(name)}</div>`;
    }).join('') + (r.truncated ? '<div style="padding:8px;font-size:.74em;color:var(--text3)">（目录过大，仅显示前 600 项）</div>' : '');
  } catch(e) { el.innerHTML = '<em style="color:var(--red);font-size:.8em;padding:8px;display:block">文件树加载失败</em>'; }
}
function _fileIcon(name){
  const ext = (name.split('.').pop()||'').toLowerCase();
  return ({py:'🐍',js:'🟨',ts:'🟦',html:'🌐',htm:'🌐',css:'🎨',md:'📝',json:'🧾',
           yml:'🧾',yaml:'🧾',toml:'🧾',png:'🖼️',jpg:'🖼️',gif:'🖼️',svg:'🖼️'})[ext] || '📄';
}
function toggleTreeDir(node){
  // 折叠：隐藏该目录之后所有缩进更深的相邻节点
  const collapsed = node.classList.toggle('collapsed');
  node.querySelector('.ft-caret').textContent = collapsed ? '▸' : '▾';
  const myPad = parseInt(node.style.paddingLeft);
  let sib = node.nextElementSibling;
  while (sib && parseInt(sib.style.paddingLeft) > myPad) {
    sib.style.display = collapsed ? 'none' : '';
    if (!collapsed && sib.classList.contains('ft-dir') && sib.classList.contains('collapsed')) {
      // 展开父级时保持子目录自身的折叠状态：跳过其子树
      const subPad = parseInt(sib.style.paddingLeft);
      let inner = sib.nextElementSibling;
      while (inner && parseInt(inner.style.paddingLeft) > subPad) { inner.style.display = 'none'; inner = inner.nextElementSibling; }
      sib = inner; continue;
    }
    sib = sib.nextElementSibling;
  }
}

// ── 打开 / 编辑 / 保存 ──
async function openCodeFile(path, node){
  if (_dirtyGuard()) return;
  const r = await (await fetch(`${API}/files/read?path=${encodeURIComponent(path)}`)).json();
  if (r.error) return toast(r.error, 'error');
  _openedPath = path; _openedContent = r.content; _diffMode = false;
  document.querySelectorAll('.ft-file.active').forEach(n => n.classList.remove('active'));
  if (node) node.classList.add('active');
  document.getElementById('code-path').textContent = path;
  document.getElementById('code-path').title = path;
  document.getElementById('code-save-btn').style.display = '';
  document.getElementById('code-diff-btn').style.display = '';
  document.getElementById('code-editor-empty').style.display = 'none';
  _mountEditor(r.content, _codeLang(path));
}
function _mountEditor(content, lang){
  const wrap = document.getElementById('code-editor');
  wrap.style.display = 'block';
  loadMonaco(ok => {
    if (_diffMode) return;  // 期间用户切到了 diff
    if (ok) {
      if (_diffEditor) { _diffEditor.dispose(); _diffEditor = null; wrap.innerHTML=''; }
      if (_editor && _editor.getModel) {
        monaco.editor.setModelLanguage(_editor.getModel(), lang);
        _editor.setValue(content);
        monaco.editor.setTheme(_monacoTheme());
      } else {
        wrap.innerHTML = '';
        _editor = monaco.editor.create(wrap, {
          value: content, language: lang, theme: _monacoTheme(),
          fontSize: 13, minimap: { enabled: false }, automaticLayout: true,
          scrollBeyondLastLine: false, wordWrap: 'off',
        });
        _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveCodeFile);
      }
    } else {
      // 离线回退：轻量 textarea（保存/撤销链路不变）
      wrap.innerHTML = `<textarea id="code-fallback" spellcheck="false"></textarea>
        <div style="font-size:.72em;color:var(--text3);padding:4px 8px">Monaco 加载失败（离线？）— 已回退为基础编辑器</div>`;
      document.getElementById('code-fallback').value = content;
      _editor = null;
    }
  });
}
function _editorValue(){
  if (_editor && _editor.getValue) return _editor.getValue();
  const ta = document.getElementById('code-fallback');
  return ta ? ta.value : null;
}
function _dirtyGuard(){
  const v = _editorValue();
  if (_openedPath && v !== null && v !== _openedContent && !_diffMode) {
    return !confirm('当前文件有未保存的修改，放弃并打开新文件？');
  }
  return false;
}
async function saveCodeFile(){
  if (!_openedPath || _diffMode) return;
  const content = _editorValue();
  if (content === null) return;
  const r = await (await fetch(`${API}/files/write`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ path: _openedPath, content }),
  })).json();
  if (r.error) return toast(r.error, 'error');
  _openedContent = content;
  toast(`已保存 ${_openedPath.split('/').pop()}（可在「文件改动」撤销）`, 'success');
  refreshChanges();
}

// ── Diff 预览（改动前 vs 当前）──
async function toggleCodeDiff(){
  const wrap = document.getElementById('code-editor');
  if (_diffMode) {  // 退出 diff → 恢复编辑
    _diffMode = false;
    document.getElementById('code-diff-btn').classList.remove('on');
    if (_diffEditor) { _diffEditor.dispose(); _diffEditor = null; }
    _editor = null; wrap.innerHTML = '';
    _mountEditor(_openedContent, _codeLang(_openedPath));
    return;
  }
  if (!_openedPath) return;
  const r = await (await fetch(`${API}/changes/diff?path=${encodeURIComponent(_openedPath)}`)).json();
  if (r.error) return toast(r.error + '（只有被 Agent/编辑器改过的文件才有 Diff）', 'error');
  _diffMode = true;
  document.getElementById('code-diff-btn').classList.add('on');
  loadMonaco(ok => {
    if (!_diffMode) return;
    if (ok) {
      if (_editor && _editor.dispose) { _editor.dispose(); _editor = null; }
      wrap.innerHTML = '';
      _diffEditor = monaco.editor.createDiffEditor(wrap, {
        theme: _monacoTheme(), fontSize: 12, automaticLayout: true,
        readOnly: true, renderSideBySide: false, minimap: { enabled: false },
      });
      const lang = _codeLang(_openedPath);
      _diffEditor.setModel({
        original: monaco.editor.createModel(r.before, lang),
        modified: monaco.editor.createModel(r.after, lang),
      });
    } else {
      wrap.innerHTML = `<div style="padding:10px;font-size:.8em;overflow:auto;height:100%">
        <b style="color:var(--red)">− 改动前${r.created ? '（新建文件，无前像）' : ''}</b>
        <pre style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:8px;max-height:40%;overflow:auto">${esc(r.before)}</pre>
        <b style="color:var(--green)">+ 当前</b>
        <pre style="background:var(--bg0);border:1px solid var(--border);border-radius:8px;padding:8px;max-height:40%;overflow:auto">${esc(r.after)}</pre></div>`;
      _editor = null;
    }
  });
}

// 主题切换时同步 Monaco 主题
document.addEventListener('click', function(e){
  if (window.monaco && _monacoState === 'ready') {
    try { monaco.editor.setTheme(_monacoTheme()); } catch(_) {}
  }
}, true);
