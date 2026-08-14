"""cost_tracker —— 每次任务完成后，把 Token 用量与耗时记入本地 CSV。

用途：衔接 Web 工作台右栏的「实时 Token 用量」——那只是当前会话累计，
这里给出**跨会话、可长期累积、可用 Excel 打开**的成本账本，方便按任务
统计模型花费。

数据落在 ``~/.automind/cost_tracker.csv``（首次自动建表头）：
    timestamp, task, success, prompt_tokens, completion_tokens,
    total_tokens, duration_ms

费用单价未内置（不同模型/中转站差异太大）。如需折算金额，可在 CSV 里
用公式（如 ``=total_tokens/1e6*单价``）或自行后处理；本插件只忠实记录
原始 token 与耗时，不做任何猜测。
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime
from pathlib import Path

from automind.core.hooks import AgentHooks

logger = logging.getLogger("automind.plugin.cost_tracker")

_CSV_PATH = Path("~/.automind/cost_tracker.csv").expanduser()
_FIELDS = [
    "timestamp", "task", "success",
    "prompt_tokens", "completion_tokens", "total_tokens", "duration_ms",
]
_lock = threading.Lock()


def _record(result) -> None:
    """把一次任务的用量追加到 CSV（同步、加锁、失败吞掉）。"""
    usage = getattr(result, "token_usage", None)
    try:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        task = getattr(result, "plan", None)
        task_text = getattr(task, "task_description", "") if task is not None else ""
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "task": (task_text or "")[:120],
            "success": bool(getattr(result, "success", False)),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        }
        _CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        new_file = not _CSV_PATH.exists()
        with _lock, _CSV_PATH.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:  # pragma: no cover - 插件副作用绝不影响主流程
        logger.warning("cost_tracker write failed", error=str(e))


def get_hooks() -> AgentHooks:
    """返回成本追踪钩子：任务结束后入账。"""

    async def after_run(result) -> None:
        _record(result)

    return AgentHooks(after_run=after_run)
