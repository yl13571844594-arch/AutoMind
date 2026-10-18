"""向用户提问工具（v1.11.11）。

「审批」只能让用户对一个**即将执行的动作**批准/拒绝；本工具补的是
「让用户做选择或补充缺失信息」——例如目标目录、输出格式、要覆盖还是另存。

实现要点：工具不直接碰 WebSocket，而是
1) 在进程内等待表登记问题（``core/user_questions.py``）；
2) 通过当前任务的事件通道把 ``user_question`` 推给界面；
3) await 用户回答（带超时；超时**绝不等于默认选项/批准**）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from automind.core import session_ctx, user_questions
from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, err, ok
from automind.tools.base import AbstractTool

logger = get_logger("automind.tool.ask_user")

#: 默认等待时长（秒）——与审批等待上限同一量级，避免任务无声挂住
_DEFAULT_TIMEOUT = 300.0
#: 上限：即使模型写 -1（无限期）也只等一小时，防止任务永久挂起
_MAX_TIMEOUT = 3600.0


class AskUserTool(AbstractTool):
    """向用户提问并等待回答（选项 / 自定义输入）。"""

    name = "ask_user_question"
    description = (
        "Ask the user to make a choice or provide missing information, then wait "
        "for the answer. Use only when the decision is genuinely the user's "
        "(target directory, format, destructive choice, ambiguity inspection "
        "cannot resolve). Put the recommended option first and append "
        "'(Recommended)' to its label. Returns the selected option labels and/or "
        "custom text; a timeout is NOT approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "1-4 questions; each id must be unique in this call.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Stable id for the answer."},
                        "question": {"type": "string", "description": "What to ask."},
                        "header": {"type": "string", "description": "Short heading (optional)."},
                        "options": {
                            "type": "array",
                            "description": "0-6 options; recommended first.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                            },
                        },
                        "multi": {"type": "boolean",
                                  "description": "Allow selecting multiple options."},
                    },
                    "required": ["id", "question"],
                },
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Seconds to wait (default 300, max 3600; -1 = wait up to 1 hour). "
                    "On timeout the call fails — do not treat it as approval."),
            },
        },
        "required": ["questions"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        # 子智能体没有可用的回答界面；让它把未决问题写进最终结果交回父级
        if session_ctx.subagent_depth() > 0:
            return bad(self.name,
                       "子智能体不能直接向用户提问：请把需要用户决定的问题写进"
                       "最终结果，由父智能体统一询问")
        try:
            raw_timeout = float(kwargs.get("timeout") or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            raw_timeout = _DEFAULT_TIMEOUT
        timeout_s = min(max(raw_timeout, 1.0), _MAX_TIMEOUT)
        if raw_timeout < 0:
            timeout_s = _MAX_TIMEOUT

        sid = session_ctx.session_id() or "default"
        try:
            normalized = user_questions.normalize_questions(kwargs.get("questions"))
            question_id, future = user_questions.create(
                sid, normalized, timeout_s=timeout_s)
        except ValueError as e:
            return bad(self.name, str(e))
        except Exception as e:
            return err(self.name, e)

        # 先把问题推给界面；没有事件通道（CLI/库调用）时如实失败，
        # 不能"登记了就干等"——那会挂满 timeout 才报一句没有信息量的超时。
        shown = await session_ctx.emit_event({
            "type": "user_question",
            "session_id": sid,
            "question_id": question_id,
            "questions": normalized,
            "timeout_s": timeout_s,
        })
        if not shown:
            user_questions.cancel(question_id, "no question channel")
            return bad(self.name,
                       "当前运行没有可用的问答通道（例如 CLI/库调用），无法向用户提问。"
                       "请基于合理假设继续，或在最终回复里列出需要用户确认的问题。")
        try:
            payload = await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            user_questions.cancel(question_id, "timeout")
            return bad(self.name,
                       f"用户 {int(timeout_s)} 秒内没有回答（超时不等于批准/默认选项）。"
                       "请基于合理假设继续，或把问题拆小后再问。",
                       question_id=question_id, timed_out=True)
        except asyncio.CancelledError:
            user_questions.cancel(question_id, "task cancelled")
            raise
        finally:
            # 兜底：任何路径都不能留下一个永远挂着的 Future
            user_questions.cancel(question_id, "finished")

        if isinstance(payload, dict) and payload.get("cancelled"):
            return bad(self.name,
                       f"问题已被取消：{payload.get('reason') or '用户关闭了界面'}",
                       question_id=question_id)
        answers = (payload or {}).get("answers") or []
        return ok(self.name, question_id=question_id, answers=answers,
                  message="User answered the question(s).")

    def get_execution_plan(self, **kwargs: Any) -> str:
        qs = kwargs.get("questions")
        n = len(qs) if isinstance(qs, list) else 0
        return f"[ask_user_question] 向用户提问（{n} 个问题，等待回答）"
