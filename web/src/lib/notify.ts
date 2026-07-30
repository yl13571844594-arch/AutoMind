// 任务完成的系统通知 —— 解决"跑 5 分钟的任务，切走干别的，回来才发现早跑完了"。
//
// 只在**窗口看不见**时才发：用户正盯着界面时，界面本身已经告诉他结果了，
// 再弹一条系统通知纯属打扰。

const KEY_ASKED = 'automind_notify_asked';

export function notifySupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

export function notifyPermission(): NotificationPermission | 'unsupported' {
  return notifySupported() ? Notification.permission : 'unsupported';
}

/**
 * 申请通知权限。
 * 浏览器要求必须由用户手势触发，所以只在设置里点开关时调用，不在启动时偷偷弹。
 */
export async function requestNotifyPermission(): Promise<NotificationPermission | 'unsupported'> {
  if (!notifySupported()) return 'unsupported';
  if (Notification.permission !== 'default') return Notification.permission;
  try {
    const p = await Notification.requestPermission();
    localStorage.setItem(KEY_ASKED, '1');
    return p;
  } catch {
    return Notification.permission;
  }
}

/** 窗口是否对用户不可见（后台标签页 / 最小化 / 被完全遮挡时的失焦）。 */
export function windowHidden(): boolean {
  return document.visibilityState === 'hidden' || !document.hasFocus();
}

interface NotifyOpts {
  title: string;
  body: string;
  /** 同 tag 的通知互相替换，避免连续任务堆一屏 */
  tag?: string;
  /** true = 无论窗口是否可见都发（默认只在不可见时发） */
  force?: boolean;
}

export function notifyTask({ title, body, tag = 'automind-task', force = false }: NotifyOpts): void {
  if (!notifySupported() || Notification.permission !== 'granted') return;
  if (!force && !windowHidden()) return;
  try {
    // 不指定 icon：仓库里没有可伺服的图标文件，指了就是 404 空白块；
    // 留空时桌面版会用 AutoMind 自身的应用图标，正是想要的效果。
    const n = new Notification(title, { body: body.slice(0, 220), tag, silent: false });
    // 点通知回到应用 —— 用户的下一步动作必然是"回去看结果"
    n.onclick = () => { window.focus(); n.close(); };
    setTimeout(() => n.close(), 12000);
  } catch {
    /* 通知构造失败（部分内核在无权限时抛错）不影响主流程 */
  }
}

/** 把毫秒渲染成人读得懂的耗时，用于通知正文。 */
export function fmtDuration(ms: number): string {
  if (!ms || ms < 0) return '';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分`;
}
