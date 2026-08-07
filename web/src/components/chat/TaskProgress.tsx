// 输入框上方的轻量执行进度条。
//
// 观测中心（DAG）信息更全，但普通用户既不知道有那个页面，跑任务时也不会切过去；
// 长任务在对话区就表现为"半天没动静，像卡住了"。这里把最关键的三件事
// —— 第几步 / 共几步、当前在做什么、已耗时 —— 常驻在视线里，并给一个
// 直达观测中心的入口。
import { useEffect, useState } from 'react';
import { useApp } from '../../store/app';
import { usePanel } from '../../store/panel';

function elapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分`;
}

export default function TaskProgress() {
  const running = useApp((s) => s.running);
  const progress = usePanel((s) => s.progress);
  const [now, setNow] = useState(Date.now());

  // 计时器只在跑任务时存在，空闲时不留常驻定时器
  useEffect(() => {
    if (!running || !progress) return;
    const iv = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(iv);
  }, [running, !!progress]);

  if (!running || !progress) return null;

  const { phase, cur, total, label, startedAt } = progress;
  // total=0 表示步数未知（对话/编程模式无预生成计划）——此时不画百分比，
  // 用流动条表示"在动但不知道还剩多少"，编不出来的进度不如不编。
  const known = total > 0 && cur > 0;
  const pct = known ? Math.min(100, Math.round((cur / total) * 100)) : 0;

  return (
    <div className="task-progress">
      <div className="tp-row">
        <span className="tp-spin" />
        <b className="tp-phase">{phase}</b>
        {known && <span className="tp-step">第 {cur} / {total} 步</span>}
        {label && <span className="tp-label" title={label}>{label}</span>}
        <span style={{ flex: 1 }} />
        <span className="tp-time">已用 {elapsed(now - startedAt)}</span>
        <button
          className="tp-link"
          title="打开观测中心，查看完整执行过程（DAG / 工具调用 / 耗时）"
          onClick={() => useApp.getState().setView('observe')}
        >查看详情</button>
      </div>
      <div className={`tp-bar${known ? '' : ' indeterminate'}`}>
        <div className="tp-fill" style={known ? { width: `${pct}%` } : undefined} />
      </div>
    </div>
  );
}
