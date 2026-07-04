"""task-timer 插件 — 记录每个任务的起止时间与耗时。

放置于 ~/.automind/plugins/task-timer/ 后在 Web「🧩 插件」中加载。
"""

from __future__ import annotations

import time

from automind.core.hooks import AgentHooks
from automind.core.logging import get_logger

logger = get_logger("plugin.task_timer")

_start_times: dict[int, float] = {}


def get_hooks() -> AgentHooks:
    async def before_run(task: str) -> None:
        _start_times[0] = time.perf_counter()
        logger.info("task_start", task=task[:80])

    async def after_run(result) -> None:  # noqa: ANN001
        elapsed = time.perf_counter() - _start_times.pop(0, time.perf_counter())
        logger.info("task_end",
                    success=getattr(result, "success", None),
                    elapsed_s=round(elapsed, 2))

    async def on_error(error: Exception, task: str) -> None:
        logger.error("task_error", task=task[:80], error=str(error))

    return AgentHooks(before_run=before_run, after_run=after_run, on_error=on_error)
