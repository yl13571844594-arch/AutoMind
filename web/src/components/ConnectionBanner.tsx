// 断线提示条 —— 顶部横幅 + 倒计时 + 立即重连。
//
// 此前断线只在顶栏留一个不起眼的「○ 未连接」灰标，用户往往是"发消息没反应"
// 才发现出了问题，也不知道系统其实正在自动重连。
import { useEffect, useState } from 'react';
import { useApp } from '../store/app';
import { reconnectNow } from '../ws';

export default function ConnectionBanner() {
  const wsState = useApp((s) => s.wsState);
  const attempt = useApp((s) => s.wsAttempt);
  const nextAt = useApp((s) => s.wsNextRetryAt);
  const [now, setNow] = useState(Date.now());

  const down = wsState === 'disconnected' || wsState === 'reconnecting';

  // 只在断线时开这个 1s 定时器，连接正常时不留常驻计时器
  useEffect(() => {
    if (!down) return;
    const iv = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(iv);
  }, [down]);

  if (!down) return null;

  const left = Math.max(0, Math.ceil((nextAt - now) / 1000));

  return (
    <div className="conn-banner" role="status" aria-live="polite">
      <span className="conn-dot" />
      <b>与服务器的连接已断开</b>
      <span className="conn-sub">
        {left > 0
          ? `正在自动重连… ${left} 秒后重试${attempt > 1 ? `（已尝试 ${attempt} 次）` : ''}`
          : '正在尝试重新连接…'}
      </span>
      <span style={{ flex: 1 }} />
      <button className="conn-btn" onClick={reconnectNow}>立即重连</button>
    </div>
  );
}
