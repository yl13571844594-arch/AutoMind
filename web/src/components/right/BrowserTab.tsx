// 🌐 浏览器实时预览（v1.11.1）：Agent 每做一步浏览器操作，后端推一帧画面过来，
// 这里显示最新画面 + 地址栏 + 最近操作的胶片条。用户不用等任务结束、也不用翻对话，
// 就能看到 Agent "正在网页上干什么" —— 走偏了可以当场插话纠正。
import { App, Tooltip } from 'antd';
import { useEffect, useState } from 'react';
import { apiGet, errText } from '../../api/client';
import { useApp } from '../../store/app';
import { usePanel, type BrowserFrame } from '../../store/panel';
import { useUi } from '../../store/ui';

const ACTION_ZH: Record<string, string> = {
  navigate: '打开页面', click: '点击', type: '输入', select: '选择', hover: '悬停',
  press: '按键', scroll: '滚动', back: '后退', forward: '前进', reload: '刷新',
  screenshot: '截图', snapshot: '读取元素',
};

function ago(ts: number, now: number): string {
  const s = Math.max(0, Math.round((now - ts) / 1000));
  if (s < 5) return '刚刚';
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  return `${Math.round(s / 3600)} 小时前`;
}

const src = (f: BrowserFrame) => `data:image/${f.fmt};base64,${f.b64}`;

export default function BrowserTab() {
  const { message } = App.useApp();
  const frames = usePanel((s) => s.frames);
  const running = useApp((s) => s.running);
  const [pick, setPick] = useState<number | null>(null);    // 选中的历史帧；null = 跟随最新
  const [now, setNow] = useState(Date.now());
  const [probe, setProbe] = useState<{ loading: boolean; text: string; ok?: boolean }>({ loading: false, text: '' });

  // 相对时间每 5 秒刷一下；没有画面时不必转
  useEffect(() => {
    if (!frames.length) return;
    const t = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(t);
  }, [frames.length]);
  // 有新帧时回到"跟随最新"
  useEffect(() => { setPick(null); }, [frames[0]?.id]);

  const cur = (pick != null && frames.find((f) => f.id === pick)) || frames[0];

  const check = async () => {
    setProbe({ loading: true, text: '' });
    try {
      const r = await apiGet('/browser/status');
      setProbe({ loading: false, ok: !!r.ready, text: r.ready ? `就绪：${r.detail}` : r.detail });
    } catch (e) {
      setProbe({ loading: false, ok: false, text: errText(e) });
    }
  };

  const enlarge = () => {
    if (!cur) return;
    useUi.getState().openPreview({
      label: cur.title || cur.url || '浏览器画面',
      html: `<body style="margin:0;background:#111;display:flex;justify-content:center">`
        + `<img src="${src(cur)}" style="max-width:100%;height:auto" alt="网页画面"></body>`,
    });
  };

  if (!cur) {
    return (
      <div className="bt-empty">
        <div className="bt-empty-ico">🌐</div>
        <b>浏览器实时预览</b>
        <p>Agent 操作网页时（打开、点击、输入…），每一步的画面都会实时显示在这里。</p>
        <p className="hint-text">试试：「打开 example.com，告诉我页面标题」</p>
        <button className="bt-btn" onClick={check} disabled={probe.loading}>
          {probe.loading ? '正在检测…' : '🔍 检测浏览器环境'}
        </button>
        {probe.text && (
          <pre className={'bt-probe' + (probe.ok ? ' ok' : ' bad')}>{probe.text}</pre>
        )}
      </div>
    );
  }

  const live = running && cur.id === frames[0]?.id;
  return (
    <div className="bt-wrap">
      <div className="bt-bar">
        <span className={'bt-dot' + (live ? ' live' : '')} title={live ? '任务进行中，画面实时更新' : '最近一次画面'} />
        <div className="bt-url" title={cur.url}>
          <span className="bt-title">{cur.title || '（无标题）'}</span>
          <span className="bt-addr">{cur.url}</span>
        </div>
        <Tooltip title="复制地址">
          <button className="bt-icon" aria-label="复制地址" onClick={() => {
            navigator.clipboard?.writeText(cur.url).then(() => message.success('地址已复制'), () => undefined);
          }}>⧉</button>
        </Tooltip>
        <Tooltip title="在新标签页打开（你自己的浏览器，不带 Agent 的登录态）">
          <button className="bt-icon" aria-label="在新标签页打开"
            disabled={!/^https?:/.test(cur.url)}
            onClick={() => window.open(cur.url, '_blank', 'noopener,noreferrer')}>↗</button>
        </Tooltip>
      </div>

      <div className="bt-stage" onClick={enlarge} title="点击放大">
        <img key={cur.id} className="bt-shot" src={src(cur)} alt={cur.title || '网页画面'} />
        <span className="bt-badge">{ACTION_ZH[cur.action] || cur.action} · {ago(cur.at, now)}</span>
      </div>

      {frames.length > 1 && (
        <>
          <div className="bt-strip-head">
            <span>最近操作</span>
            <a onClick={() => usePanel.getState().clearFrames()}>清空</a>
          </div>
          <div className="bt-strip">
            {frames.map((f) => (
              <button key={f.id} className={'bt-thumb' + (f.id === cur.id ? ' on' : '')}
                onClick={() => setPick(f.id === frames[0].id ? null : f.id)}
                title={`${ACTION_ZH[f.action] || f.action} · ${f.title || f.url}`}>
                <img src={src(f)} alt="" loading="lazy" />
                <span>{ACTION_ZH[f.action] || f.action}</span>
              </button>
            ))}
          </div>
        </>
      )}
      {pick != null && (
        <div className="bt-follow">
          正在查看历史画面 · <a onClick={() => setPick(null)}>回到最新</a>
        </div>
      )}
    </div>
  );
}
