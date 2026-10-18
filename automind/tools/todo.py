"""待办清单工具（v1.11.11）—— 让模型维护一份用户能看见的短清单。

与「计划树」的分工：计划树是执行前生成、按步骤推进的；``todo_write`` 是
任务中途随时可整表重写的**工作清单**（出现新子任务、发现漏项时用）。两者
互不替代，也不互相覆盖。

只暴露一个工具：读清单不需要额外工具（模型上下文里就有最近一次提交的内容），
清空 = 提交空数组。工具表每多一项都要在每一步重发 schema，能省则省。
"""

from __future__ import annotations

from typing import Any

from automind.core import session_ctx
from automind.core.logging import get_logger
from automind.core.todo_store import counts, replace
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, err, ok
from automind.tools.base import AbstractTool

logger = get_logger("automind.tool.todo")


class TodoWriteTool(AbstractTool):
    """整表替换当前会话的待办清单。"""

    name = "todo_write"
    description = (
        "Maintain a short, user-visible todo list for the current session. "
        "Always send the COMPLETE list (it replaces the previous one, empty array clears it); "
        "use it for multi-step work to track what is pending / in progress / done. "
        "Keep at most 50 items; exactly one item should be in_progress at a time "
        "unless parallel work is intentional."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Complete todo list (replaces the previous one).",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Stable id (optional; e.g. t1)."},
                        "content": {"type": "string", "description": "What to do (1-500 chars)."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        sid = session_ctx.session_id() or "default"
        try:
            todos = replace(sid, kwargs.get("todos"))
        except ValueError as e:
            return bad(self.name, str(e))
        except Exception as e:
            return err(self.name, e)
        summary = counts(todos)
        # 推给界面（右栏待办区块）。没有事件通道（CLI/库用法）不算失败：
        # 清单已经落库，下一次打开界面还能看到。
        await session_ctx.emit_event({
            "type": "todo_updated",
            "session_id": sid,
            "todos": todos,
            "counts": summary,
        })
        return ok(self.name, todos=todos, counts=summary,
                  message=(
                      f"Updated todo list: {summary['pending']} pending, "
                      f"{summary['in_progress']} in progress, "
                      f"{summary['completed']} completed."))

    def get_execution_plan(self, **kwargs: Any) -> str:
        todos = kwargs.get("todos")
        n = len(todos) if isinstance(todos, list) else 0
        return f"[todo_write] 更新待办清单（{n} 条）"
