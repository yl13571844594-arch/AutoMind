// 📄 代码标签（Web IDE）：文件树 + Monaco（CDN 按需加载，离线回退 textarea）+ Diff。
import { App } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import { useApp } from '../../store/app';

const MONACO_CDN = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min';
const LANG_BY_EXT: Record<string, string> = {
  py: 'python', js: 'javascript', ts: 'typescript', jsx: 'javascript', tsx: 'typescript',
  json: 'json', html: 'html', htm: 'html', css: 'css', md: 'markdown', yml: 'yaml',
  yaml: 'yaml', toml: 'ini', sh: 'shell', ps1: 'powershell', bat: 'bat', sql: 'sql',
  xml: 'xml', txt: 'plaintext', go: 'go', rs: 'rust', java: 'java', c: 'c', h: 'c',
  cpp: 'cpp', cs: 'csharp', rb: 'ruby', php: 'php',
};
const FILE_ICON: Record<string, string> = {
  py: '🐍', js: '🟨', ts: '🟦', html: '🌐', htm: '🌐', css: '🎨', md: '📝', json: '🧾',
  yml: '🧾', yaml: '🧾', toml: '🧾', png: '🖼️', jpg: '🖼️', gif: '🖼️', svg: '🖼️',
};
const codeLang = (p: string) => LANG_BY_EXT[(p.split('.').pop() || '').toLowerCase()] || 'plaintext';

let monacoState: 'idle' | 'loading' | 'ready' | 'failed' = 'idle';
const monacoQueue: ((ok: boolean) => void)[] = [];

function loadMonaco(cb: (ok: boolean) => void) {
  if (monacoState === 'ready') return cb(true);
  if (monacoState === 'failed') return cb(false);
  monacoQueue.push(cb);
  if (monacoState === 'loading') return;
  monacoState = 'loading';
  const s = document.createElement('script');
  s.src = MONACO_CDN + '/vs/loader.js';
  s.onload = () => {
    try {
      const req = (window as any).require;
      req.config({ paths: { vs: MONACO_CDN + '/vs' } });
      (window as any).MonacoEnvironment = {
        getWorkerUrl: () => URL.createObjectURL(new Blob(
          [`self.MonacoEnvironment={baseUrl:'${MONACO_CDN}/'};importScripts('${MONACO_CDN}/vs/base/worker/workerMain.js');`],
          { type: 'text/javascript' })),
      };
      req(['vs/editor/editor.main'],
        () => { monacoState = 'ready'; monacoQueue.splice(0).forEach((f) => f(true)); },
        () => { monacoState = 'failed'; monacoQueue.splice(0).forEach((f) => f(false)); });
    } catch { monacoState = 'failed'; monacoQueue.splice(0).forEach((f) => f(false)); }
  };
  s.onerror = () => { monacoState = 'failed'; monacoQueue.splice(0).forEach((f) => f(false)); };
  document.head.appendChild(s);
}

export default function CodeTab() {
  const { message, modal } = App.useApp();
  const theme = useApp((s) => s.theme);
  const [entries, setEntries] = useState<any[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [openedPath, setOpenedPath] = useState('');
  const [diffMode, setDiffMode] = useState(false);
  const [monacoOk, setMonacoOk] = useState<boolean | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const wrapRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<any>(null);
  const diffRef = useRef<any>(null);
  const openedContent = useRef('');
  const taRef = useRef<HTMLTextAreaElement>(null);

  const refreshTree = () => {
    apiGet('/files/tree?limit=600').then((r) => {
      setEntries(r.entries || []);
      setTruncated(!!r.truncated);
    }).catch(() => message.error('文件树加载失败'));
  };
  useEffect(refreshTree, []);

  useEffect(() => {
    if (monacoState === 'ready' && (window as any).monaco) {
      (window as any).monaco.editor.setTheme(theme === 'light' ? 'vs' : 'vs-dark');
    }
  }, [theme]);

  const editorValue = (): string | null => {
    if (editorRef.current?.getValue) return editorRef.current.getValue();
    if (taRef.current) return taRef.current.value;
    return null;
  };

  const mountEditor = (content: string, lang: string) => {
    loadMonaco((ok) => {
      setMonacoOk(ok);
      const wrap = wrapRef.current;
      if (!wrap) return;
      if (ok) {
        const monaco = (window as any).monaco;
        if (diffRef.current) { diffRef.current.dispose(); diffRef.current = null; wrap.innerHTML = ''; }
        if (editorRef.current?.getModel) {
          monaco.editor.setModelLanguage(editorRef.current.getModel(), lang);
          editorRef.current.setValue(content);
        } else {
          wrap.innerHTML = '';
          editorRef.current = monaco.editor.create(wrap, {
            value: content, language: lang,
            theme: theme === 'light' ? 'vs' : 'vs-dark',
            fontSize: 13, minimap: { enabled: false }, automaticLayout: true,
            scrollBeyondLastLine: false,
          });
          editorRef.current.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, save);
        }
      } else if (taRef.current) {
        taRef.current.value = content;
      }
    });
  };

  const openFile = async (path: string) => {
    const v = editorValue();
    if (openedPath && v !== null && v !== openedContent.current && !diffMode) {
      const ok = await new Promise((res) => modal.confirm({
        title: '当前文件有未保存的修改', content: '放弃并打开新文件？',
        onOk: () => res(true), onCancel: () => res(false),
      }));
      if (!ok) return;
    }
    const r = await apiGet(`/files/read?path=${encodeURIComponent(path)}`);
    if (r.error) { message.error(r.error); return; }
    setOpenedPath(path);
    setDiffMode(false);
    openedContent.current = r.content;
    mountEditor(r.content, codeLang(path));
  };

  const save = async () => {
    const content = editorValue();
    if (!openedPath || content === null) return;
    const r = await apiPost('/files/write', { path: openedPath, content });
    if (r.error) { message.error(r.error); return; }
    openedContent.current = content;
    message.success(`已保存 ${openedPath.split('/').pop()}（可在「文件改动」撤销）`);
  };

  const toggleDiff = async () => {
    const wrap = wrapRef.current;
    if (!wrap || !openedPath) return;
    if (diffMode) {
      setDiffMode(false);
      if (diffRef.current) { diffRef.current.dispose(); diffRef.current = null; }
      editorRef.current = null;
      wrap.innerHTML = '';
      mountEditor(openedContent.current, codeLang(openedPath));
      return;
    }
    const r = await apiGet(`/changes/diff?path=${encodeURIComponent(openedPath)}`);
    if (r.error) { message.error(r.error + '（只有被 Agent/编辑器改过的文件才有 Diff）'); return; }
    setDiffMode(true);
    loadMonaco((ok) => {
      if (!wrapRef.current) return;
      if (ok) {
        const monaco = (window as any).monaco;
        if (editorRef.current?.dispose) { editorRef.current.dispose(); editorRef.current = null; }
        wrapRef.current.innerHTML = '';
        diffRef.current = monaco.editor.createDiffEditor(wrapRef.current, {
          theme: theme === 'light' ? 'vs' : 'vs-dark', fontSize: 12, automaticLayout: true,
          readOnly: true, renderSideBySide: false, minimap: { enabled: false },
        });
        const lang = codeLang(openedPath);
        diffRef.current.setModel({
          original: monaco.editor.createModel(r.before, lang),
          modified: monaco.editor.createModel(r.after, lang),
        });
      } else {
        message.info('Monaco 未加载，Diff 不可用（离线模式）');
        setDiffMode(false);
      }
    });
  };

  // 目录折叠：隐藏 path 前缀匹配的子项
  const visible = entries.filter((e) => {
    for (const dir of collapsed) {
      if (e.path !== dir && e.path.startsWith(dir + '/')) return false;
    }
    return true;
  });

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
        borderBottom: '1px solid var(--border)', fontSize: '.8em',
      }}>
        <a style={{ cursor: 'pointer' }} title="刷新文件树" onClick={refreshTree}>⟳</a>
        <span className="mono" style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text2)' }}
          title={openedPath}>{openedPath || '未打开文件'}</span>
        {openedPath && (
          <>
            <a style={{ cursor: 'pointer', color: diffMode ? 'var(--accent)' : undefined }} onClick={toggleDiff}
              title="查看该文件的改动 Diff">± Diff</a>
            {!diffMode && <a style={{ cursor: 'pointer', color: 'var(--accent)' }} onClick={save} title="保存 (Ctrl+S)">💾 保存</a>}
          </>
        )}
      </div>
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <div style={{ width: 180, overflowY: 'auto', borderRight: '1px solid var(--border)', padding: 4 }}>
          {visible.length === 0 && <em className="hint-text" style={{ padding: 8, display: 'block' }}>项目目录为空</em>}
          {visible.map((e) => {
            const name = e.path.split('/').pop();
            const pad = 6 + e.level * 12;
            if (e.dir) {
              const isCollapsed = collapsed.has(e.path);
              return (
                <div key={e.path} className="ft-item" style={{ paddingLeft: pad }}
                  onClick={() => {
                    const next = new Set(collapsed);
                    if (isCollapsed) next.delete(e.path); else next.add(e.path);
                    setCollapsed(next);
                  }}>
                  {isCollapsed ? '▸' : '▾'} 📁 {name}
                </div>
              );
            }
            return (
              <div key={e.path} className={`ft-item ${openedPath === e.path ? 'active' : ''}`}
                style={{ paddingLeft: pad }} title={e.path} onClick={() => openFile(e.path)}>
                {FILE_ICON[(name.split('.').pop() || '').toLowerCase()] || '📄'} {name}
              </div>
            );
          })}
          {truncated && <div className="hint-text" style={{ padding: 8 }}>（目录过大，仅显示前 600 项）</div>}
        </div>
        <div style={{ flex: 1, position: 'relative', minWidth: 0 }}>
          {!openedPath ? (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 6, color: 'var(--text3)', fontSize: '.85em' }}>
              <div style={{ fontSize: '2em' }}>📄</div>
              <div>从左侧文件树选择文件开始编辑</div>
              <div className="hint-text">Monaco Editor 按需加载（首次需联网）；保存的改动可在「文件改动」中撤销</div>
            </div>
          ) : monacoOk === false && !diffMode ? (
            <textarea ref={taRef} spellCheck={false} defaultValue={openedContent.current} style={{
              width: '100%', height: '100%', border: 'none', outline: 'none', resize: 'none',
              background: 'var(--bg0)', color: 'var(--text)', fontFamily: 'var(--mono)', fontSize: 13, padding: 10,
            }} />
          ) : null}
          <div ref={wrapRef} style={{
            position: 'absolute', inset: 0,
            display: openedPath && monacoOk !== false ? 'block' : 'none',
          }} />
        </div>
      </div>
    </div>
  );
}
