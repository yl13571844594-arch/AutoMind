// 📁 目录选择器（v1.11.5）：快捷位置（桌面 / 文档 / 下载 / 主目录 / 当前项目）+ 盘符，
// 可编辑的地址栏、面包屑、过滤、显示文件、新建文件夹。本机访问时可浏览整块磁盘，
// 远程访问仍按服务端锚点限制（见 server.py `_fs_full_disk_allowed`）。
import { App, Button, Input, Tooltip } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiGet, apiPost, errText } from '../../api/client';

interface Listing {
  path: string; parent: string; dirs: string[]; files: string[]; truncated?: boolean;
  drives: string[]; places: { key: string; label: string; path: string }[]; full_disk?: boolean;
}

const PLACE_ICON: Record<string, string> = {
  desktop: '🖥️', documents: '📄', downloads: '⬇️', home: '🏠', project: '📌',
};

/** 把路径拆成面包屑：C:\a\b → [C:\, C:\a, C:\a\b]；/a/b → [/, /a, /a/b] */
function crumbs(p: string): { label: string; path: string }[] {
  const win = /^[A-Za-z]:/.test(p);
  const sep = win ? '\\' : '/';
  const parts = p.split(/[\\/]+/).filter(Boolean);
  const out: { label: string; path: string }[] = [];
  let acc = '';
  parts.forEach((part, i) => {
    if (win && i === 0) acc = part + sep;
    else acc = (acc && !acc.endsWith(sep) ? acc + sep : (acc || sep)) + part;
    out.push({ label: win && i === 0 ? part + sep : part, path: acc });
  });
  if (!win) out.unshift({ label: '/', path: '/' });
  return out;
}

function join(base: string, name: string): string {
  const sep = /^[A-Za-z]:/.test(base) || base.includes('\\') ? '\\' : '/';
  return base.endsWith('\\') || base.endsWith('/') ? base + name : base + sep + name;
}

export default function DirPicker({ initial, onPick, onCancel, pickLabel = '✓ 选此目录' }: {
  initial?: string;
  onPick: (path: string) => void;
  onCancel?: () => void;
  pickLabel?: string;
}) {
  const { message, modal } = App.useApp();
  const [data, setData] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(false);
  const [addr, setAddr] = useState('');
  const [filter, setFilter] = useState('');
  const [showFiles, setShowFiles] = useState(false);
  const [sel, setSel] = useState('');
  const listRef = useRef<HTMLDivElement>(null);

  const go = async (path: string, files = showFiles) => {
    setLoading(true);
    try {
      const r: Listing = await apiGet(`/fs/list?path=${encodeURIComponent(path)}${files ? '&files=true' : ''}`);
      setData(r); setAddr(r.path); setFilter(''); setSel('');
      listRef.current?.scrollTo?.(0, 0);
    } catch (e) {
      message.error(`无法打开该目录：${errText(e)}`);
      if (data) setAddr(data.path);
    } finally { setLoading(false); }
  };

  useEffect(() => {
    // 初始路径打不开（被删 / 不在范围内）时退回主目录，而不是让选择器空着
    (async () => {
      try {
        const r: Listing = await apiGet(`/fs/list?path=${encodeURIComponent(initial || '')}`);
        setData(r); setAddr(r.path);
      } catch { go(''); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return {
      dirs: (data?.dirs || []).filter((d) => !f || d.toLowerCase().includes(f)),
      files: (data?.files || []).filter((d) => !f || d.toLowerCase().includes(f)),
    };
  }, [data, filter]);

  const mkdir = () => {
    if (!data) return;
    let name = '';
    modal.confirm({
      title: '新建文件夹',
      content: <Input autoFocus placeholder="文件夹名称" onChange={(e) => { name = e.target.value; }} />,
      okText: '新建',
      onOk: async () => {
        if (!name.trim()) { message.error('请填写名称'); throw new Error('empty'); }
        try {
          const r = await apiPost('/fs/mkdir', { path: data.path, name: name.trim() });
          message.success('已新建：' + r.path);
          await go(r.path);
        } catch (e) { message.error(errText(e)); throw e; }
      },
    });
  };

  const target = sel ? join(data?.path || '', sel) : data?.path || '';

  return (
    <div className="dirpicker">
      <div className="dp-side">
        {(data?.places || []).map((p) => (
          <div key={p.key} className={'dp-place' + (data?.path === p.path ? ' active' : '')}
            title={p.path} onClick={() => go(p.path)}>
            <span className="dp-ico">{PLACE_ICON[p.key] || '📁'}</span>{p.label}
          </div>
        ))}
        {(data?.drives || []).length > 0 && <div className="dp-side-title">此电脑</div>}
        {(data?.drives || []).map((d) => (
          <div key={d} className={'dp-place' + (data?.path === d ? ' active' : '')} onClick={() => go(d)}>
            <span className="dp-ico">💽</span>{d.replace(/\\$/, '')}
          </div>
        ))}
      </div>

      <div className="dp-main">
        <div className="dp-bar">
          <Tooltip title="上一级">
            <Button size="small" disabled={!data?.parent} onClick={() => data?.parent && go(data.parent)}>⬆</Button>
          </Tooltip>
          <Input size="small" className="mono" value={addr} onChange={(e) => setAddr(e.target.value)}
            onPressEnter={() => addr.trim() && go(addr.trim())} placeholder="输入路径后回车，如 D:\projects"
            style={{ flex: 1, minWidth: 0 }} />
          <Tooltip title="刷新"><Button size="small" onClick={() => data && go(data.path)}>⟳</Button></Tooltip>
        </div>
        {data && (
          <div className="dp-crumbs">
            {crumbs(data.path).map((c, i, arr) => (
              <span key={c.path}>
                <a onClick={() => go(c.path)}>{c.label}</a>{i < arr.length - 1 && <span className="dp-sep">›</span>}
              </span>
            ))}
          </div>
        )}
        <div className="dp-bar">
          <Input size="small" allowClear placeholder="过滤当前目录" value={filter}
            onChange={(e) => setFilter(e.target.value)} style={{ flex: 1, minWidth: 0 }} />
          <Button size="small" type={showFiles ? 'primary' : 'default'} ghost={showFiles}
            onClick={() => { const v = !showFiles; setShowFiles(v); if (data) go(data.path, v); }}>
            {showFiles ? '隐藏文件' : '显示文件'}</Button>
          <Button size="small" onClick={mkdir}>＋ 新建文件夹</Button>
        </div>
        <div className="dp-list" ref={listRef} style={{ opacity: loading ? 0.55 : 1 }}>
          {shown.dirs.map((d) => (
            <div key={'d' + d} className={'ft-item dp-row' + (sel === d ? ' active' : '')}
              onClick={() => setSel(sel === d ? '' : d)} onDoubleClick={() => go(join(data!.path, d))}
              title="单击选中，双击进入">
              <span>📁 {d}</span>
              <a className="dp-enter" onClick={(e) => { e.stopPropagation(); go(join(data!.path, d)); }}>进入 ›</a>
            </div>
          ))}
          {showFiles && shown.files.map((f) => (
            <div key={'f' + f} className="ft-item dp-row dp-file">📄 {f}</div>
          ))}
          {data && !shown.dirs.length && !(showFiles && shown.files.length) && (
            <div className="hint-text" style={{ padding: 10 }}>{filter ? '没有匹配的项' : '（无子目录）'}</div>
          )}
          {data?.truncated && <div className="hint-text" style={{ padding: 6 }}>项目过多，仅显示前 500 项，可用上方过滤</div>}
        </div>
        <div className="dp-foot">
          <span className="mono hint-text dp-target" title={target}>{target}</span>
          {onCancel && <Button size="small" onClick={onCancel}>取消</Button>}
          <Button size="small" type="primary" disabled={!target} onClick={() => onPick(target)}>{pickLabel}</Button>
        </div>
        {data && data.full_disk === false && (
          <div className="hint-text" style={{ padding: '0 10px 6px' }}>
            远程访问时只能浏览主目录、项目与已配置的工作区；在本机打开界面可浏览整块磁盘。
          </div>
        )}
      </div>
    </div>
  );
}
