"""task_notify —— 任务结束后发出完成通知。

两种通道，均不引入第三方依赖：
1. **本地通知日志**（默认）：写入 ``~/.automind/notifications.log``；
2. **Webhook 推送**（可选）：设置环境变量 ``AUTOMIND_NOTIFY_WEBHOOK``
   后，任务结束会以 JSON 形式 POST 到该地址。适配任意接收 JSON 的机器人
   （企业微信群机器人 / 钉钉自定义机器人 / Slack Incoming Webhook 等），
   这些平台只需一个 URL、无需 SDK。

payload 示例：``{"text": "✅ AutoMind 任务完成（成功/失败，耗时 x 秒）"}``
    - 企业微信/钉钉自定义机器人接受 ``{"text": ...}`` 结构；
    - 其它接收方通常也能直接读取 text 字段。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

from automind.core.hooks import AgentHooks

logger = logging.getLogger("automind.plugin.task_notify")

_LOG_PATH = Path("~/.automind/notifications.log").expanduser()
_lock = threading.Lock()


def _notify(result) -> None:
    """生成通知文案，写本地日志并（可选）推 Webhook。"""
    success = bool(getattr(result, "success", False))
    duration_ms = int(getattr(result, "duration_ms", 0) or 0)
    plan = getattr(result, "plan", None)
    task = getattr(plan, "task_description", "") if plan is not None else ""
    status = "成功" if success else "失败"
    text = f"✅ AutoMind 任务{status}（耗时 {duration_ms / 1000:.1f} 秒）"
    if task:
        text += f"：{(task or '')[:60]}"
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n"
        with _lock, _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:  # pragma: no cover
        logger.warning("task_notify local write failed", error=str(e))

    webhook = os.environ.get("AUTOMIND_NOTIFY_WEBHOOK", "").strip()
    if not webhook:
        return
    try:
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - 用户显式配置
            logger.info("task_notify webhook delivered", status=resp.status)
    except Exception as e:  # pragma: no cover - 通知失败不影响主流程
        logger.warning("task_notify webhook failed", error=str(e))


def get_hooks() -> AgentHooks:
    """返回任务完成通知钩子。"""

    async def after_run(result) -> None:
        _notify(result)

    return AgentHooks(after_run=after_run)
