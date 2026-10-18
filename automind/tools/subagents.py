"""子智能体委派工具（v1.11.11 专业版 multi_agent MVP）。

为什么是四个小工具而不是一个 ``action`` 大工具：模型在 ReAct 里一次只做一件事，
「派发」「查进度」「补一句」「停下来」发生在不同的步骤、不同的上下文里。
四个短 schema 分别对应四种意图，比一个带 action 分支的五参数工具更容易被
正确调用（与 ``terminal_background`` 把 poll/kill/list 拆开的理由相同）。

边界（有意的）：
  · 本模块**不注册自己** —— 由 Agent 在 ``multi_agent`` 特性可用时统一注册
    （社区版不暴露这些工具，模型就不会规划出用不了的委派动作）；
  · 派发是**一次性**的：子智能体后台跑，父任务立即拿到 ``agent_id`` 继续干活，
    结果用 ``subagent_list`` 读回；父任务不该原地等（等＝白占一个并发位）；
  · 子智能体不能再派生子智能体（深度上限 1），拒绝时给出可执行的替代做法；
  · 所有失败都返回**模型能据此改正**的说明（参数写错→怎么改，业务限制→为什么、
    下一步做什么），绝不返回一个看起来成功、实际没派发的空结果。
"""

from __future__ import annotations

import re
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.planning import subagents
from automind.tools._toolkit import bad, err, ok
from automind.tools.base import AbstractTool

# ═══════════════════════════════════════════════════════════
# 结果回传限额
# ═══════════════════════════════════════════════════════════

#: ``subagent_list`` 里每个已结束子智能体回传的结果字符上限
RESULT_PER_AGENT_CHARS = 4000

#: 一次 ``subagent_list`` 回传的结果总字符上限：20 个子任务的结果一次全塞进
#: 父上下文会把父任务自己的上下文挤爆（而父任务还要继续干活）。
RESULT_TOTAL_CHARS = 12000


# ═══════════════════════════════════════════════════════════
# 公共小工具
# ═══════════════════════════════════════════════════════════


def _manager_of(tool: AbstractTool) -> subagents.SubagentManager:
    """取该工具绑定的管理器（测试可注入；生产用进程级单例）。"""
    return getattr(tool, "_manager", None) or subagents.default_manager()


def _require_parent(tool_name: str) -> tuple[Any, ToolResult | None]:
    """子智能体工具必须在任务执行上下文里调用 —— 这是"父 Agent 是谁"的唯一来源。

    不在任务里（CLI 直接调工具 / 单测）时如实失败，而不是造一个没有会话归属的
    子 Agent：没有父会话就没有工作目录、审批通道和事件出口，跑出来的结果
    也无处归还。
    """
    parent = subagents.current_agent()
    if parent is None:
        return None, bad(
            tool_name,
            "子智能体工具只能在一次**正在执行的任务**里使用：当前没有可用的会话 Agent。"
            "若你确实在任务中被调用，说明运行时的 session_ctx.agent() 绑定没生效 ——"
            "这是版本/接线问题，请报告开发者，重试同一个调用不会成功。")
    return parent, None


def _depth_refusal(tool_name: str) -> ToolResult | None:
    """嵌套深度超限时的拒绝（返回 None = 允许派发）。"""
    depth = subagents.current_depth()
    if depth < subagents.MAX_DEPTH:
        return None
    return bad(
        tool_name,
        f"子智能体不能再派生新的子智能体：当前嵌套深度 {depth}，"
        f"上限 {subagents.MAX_DEPTH} 层。请直接在当前任务里完成这项工作，"
        f"不要再尝试派发。",
        depth=depth, max_depth=subagents.MAX_DEPTH)


def _normalize_allowed_tools(raw: Any) -> list[str] | None:
    """归一化工具白名单：None = 默认只读；空列表 = 明确拒绝（这几乎总是笔误）。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        parts = [p for p in re.split(r"[,，\s]+", text) if p]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(p).strip() for p in raw]
    else:
        raise ValueError(
            f"allowed_tools 必须是工具名列表（收到 {type(raw).__name__}）。"
            "例如 [\"file_read\", \"file_write\"]；不限制就省略该参数（默认只给只读工具）。")
    names: list[str] = []
    for name in parts:
        if name and name not in names:
            names.append(name)
    if not names:
        raise ValueError(
            "allowed_tools 传了空列表：子智能体将**没有任何工具**可用，"
            "这不是有效的用法。若不需要限制，请省略这个参数（默认只给只读 SAFE 工具）；"
            "若子任务需要写文件，请把 file_write / file_edit 等工具名列进来。")
    return names


def _int_param(raw: Any, default: int, *, low: int, high: int, name: str) -> int:
    """参数取整 + 范围校验；越界说明允许范围，让模型一次改对。"""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} 必须是整数（收到 {raw!r}）。允许范围 {low}~{high}。") from None
    if value < low or value > high:
        raise ValueError(
            f"{name} 超出允许范围：{value}（允许 {low}~{high}）。"
            f"请改成范围内的整数后重试。")
    return value


def _from_manager(tool_name: str, result: Any) -> ToolResult:
    """把管理器返回的 ``{ok, ...}`` 字典转成 ToolResult（契约不符按失败报）。"""
    if not isinstance(result, dict):
        return bad(tool_name,
                   f"子智能体运行时返回了意外的结果类型（{type(result).__name__}，"
                   f"期望 dict）—— 这是实现 bug，请报告开发者。")
    payload = {k: v for k, v in result.items() if k not in ("ok", "error")}
    if result.get("ok"):
        return ok(tool_name, **payload)
    return bad(tool_name,
               str(result.get("error") or "子智能体操作失败（运行时没有给出原因）。"),
               **payload)


# ═══════════════════════════════════════════════════════════
# subagent —— 派发
# ═══════════════════════════════════════════════════════════


class SubagentTool(AbstractTool):
    """派发一个一次性子智能体（后台运行，父任务立即继续）。"""

    name = "subagent"
    description = (
        "Delegate a complete, self-contained subtask to a background sub-agent "
        "(professional edition feature). The sub-agent has its own context and tool "
        "set; this call returns immediately with an agent_id — do NOT wait for it. "
        "Read progress/results later with subagent_list, append requirements with "
        "subagent_send, cancel with subagent_stop. Use it for independent parallel "
        "work (research, scanning many files, running a test suite). Do not use it "
        "for a single tool call or for work that depends on your own conversation: "
        "the sub-agent cannot see your chat, so the task text must be self-contained."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The COMPLETE, self-contained task for the sub-agent. It cannot "
                    "see your conversation, so include every path, file name and "
                    "requirement it needs. State the expected deliverable."),
            },
            "title": {
                "type": "string",
                "description": "Short label shown in the UI (defaults to the task preview).",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional exact tool-name whitelist. Omit it to give read-only "
                    "(SAFE) tools only. Add write tools explicitly if the sub-agent "
                    "must modify files. Unknown names are reported back, not silently ignored."),
            },
            "max_steps": {
                "type": "integer",
                "minimum": 1,
                "maximum": subagents.MAX_MAX_STEPS,
                "description": (f"Max ReAct steps for the sub-agent "
                                f"(default {subagents.DEFAULT_MAX_STEPS})."),
            },
            "max_tokens": {
                "type": "integer",
                "minimum": 0,
                "maximum": subagents.MAX_MAX_TOKENS,
                "description": ("Optional token budget for the sub-agent "
                                "(0 or omitted = inherit the parent task budget)."),
            },
        },
        "required": ["task"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 15

    def __init__(self, manager: subagents.SubagentManager | None = None) -> None:
        self._manager = manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        parent, failure = _require_parent(self.name)
        if failure is not None:
            return failure
        refused = _depth_refusal(self.name)
        if refused is not None:
            return refused

        task = str(kwargs.get("task") or "").strip()
        if not task:
            return bad(
                self.name,
                "task 不能为空：请写清子智能体要完成的**完整**任务"
                "（它看不到父任务的对话，必须自包含），并说明期望的交付物。")

        title = str(kwargs.get("title") or "").strip()
        try:
            allowed_tools = _normalize_allowed_tools(kwargs.get("allowed_tools"))
            max_steps = _int_param(
                kwargs.get("max_steps"), subagents.DEFAULT_MAX_STEPS,
                low=1, high=subagents.MAX_MAX_STEPS, name="max_steps")
            max_tokens = _int_param(
                kwargs.get("max_tokens"), 0,
                low=0, high=subagents.MAX_MAX_TOKENS, name="max_tokens")
        except ValueError as e:
            return bad(self.name, str(e))

        try:
            # 会话标识在工具入口显式取一次并贯穿四个工具：派发与后续的
            # list/send/stop 必须落在同一个分组里，不能各自按 contextvar 再取一遍。
            agent_id = await _manager_of(self).start(
                parent, task, title=title, allowed_tools=allowed_tools,
                max_steps=max_steps, max_tokens=max_tokens,
                session_id=subagents.current_session_id())
        except subagents.SubagentLimitError as e:
            # 并发/深度上限：用户能直接理解、模型能据此改道，不作为异常抛出
            return bad(self.name, str(e))
        except subagents.SubagentError as e:
            return bad(self.name, str(e))
        except Exception as e:
            return err(self.name, e)

        return ok(
            self.name,
            agent_id=agent_id,
            title=title or subagents.derive_title(task),
            status="running",
            max_steps=max_steps,
            max_tokens=max_tokens,
            note=("子智能体已在后台运行，本次调用**立即返回**——不要在这里等它。"
                  "用 subagent_list 查看进度与最终结果；要补充要求用 "
                  f"subagent_send(agent_id=\"{agent_id}\", message=...)；"
                  "要停止用 subagent_stop。"
                  "如果当前任务还有不依赖它的步骤，先继续做那些步骤。"))


# ═══════════════════════════════════════════════════════════
# subagent_list —— 查进度 / 取结果
# ═══════════════════════════════════════════════════════════


class SubagentListTool(AbstractTool):
    """列出本会话的子智能体，并回传已结束者的结果。"""

    name = "subagent_list"
    description = (
        "List the sub-agents delegated in this session: status (running/done/failed/"
        "cancelled), steps, token usage, error and output tail. For finished "
        "sub-agents the results are included (most recent first, length-capped). "
        "Use it to check progress and to collect results; do not poll it in a tight "
        "loop — do some other work first."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    def __init__(self, manager: subagents.SubagentManager | None = None) -> None:
        self._manager = manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        _parent, failure = _require_parent(self.name)
        if failure is not None:
            return failure
        manager = _manager_of(self)
        session_id = subagents.current_session_id()
        items = manager.list(session_id=session_id)
        results, truncated = _collect_results(manager, session_id, items)
        running = sum(1 for item in items if item["status"] == "running")

        payload: dict[str, Any] = {
            "agents": items,
            "count": len(items),
            "running": running,
        }
        if results:
            payload["results"] = results
        if truncated:
            payload["results_note"] = (
                "部分结果因长度上限被截断/省略；每个子智能体保留的完整输出见其 "
                "output 字段（上限 8000 字符），这里只回传最近、最重要的部分。")
        if not items:
            payload["note"] = ("还没有派发过子智能体。需要并行/独立的子任务时用 "
                               "subagent(task=...) 派发一个。")
        elif running:
            payload["note"] = ("仍有子智能体在运行：先继续做不依赖它们的步骤，稍后再查；"
                               "要补充要求用 subagent_send，要停止用 subagent_stop。")
        else:
            payload["note"] = "所有子智能体都已结束；results 里是它们的结果。"
        return ok(self.name, **payload)


def _collect_results(manager: subagents.SubagentManager, session_id: str,
                     items: list[dict[str, Any]]) -> tuple[dict[str, str], bool]:
    """收集已结束子智能体的 output（最近优先，受总长度上限约束）。

    为什么不把 20 条结果原样全给：父任务拿到结果后还要继续思考和调工具，
    一屏几万字的结果会把它自己的上下文挤爆。所以按"最近优先"取材并显式
    标注截断 —— 少给但说清楚，比给一半再静默剪掉强。
    """
    results: dict[str, str] = {}
    remaining = RESULT_TOTAL_CHARS
    truncated = False
    for item in items:
        if item["status"] == "running":
            continue
        if remaining <= 0:
            truncated = True
            break
        detail = manager.get(item["agent_id"], session_id=session_id)
        output = str((detail or {}).get("output") or "")
        if not output:
            continue
        limit = min(RESULT_PER_AGENT_CHARS, remaining)
        if len(output) > limit:
            output = f"{output[:limit]}\n…[结果已截断，原长 {len(output)} 字符]"
            truncated = True
        results[item["agent_id"]] = output
        remaining -= len(output)
    return results, truncated


# ═══════════════════════════════════════════════════════════
# subagent_send —— 追加要求
# ═══════════════════════════════════════════════════════════


class SubagentSendTool(AbstractTool):
    """给仍在运行的子智能体追加一句要求（同用户中途插话）。"""

    name = "subagent_send"
    description = (
        "Append a message to a RUNNING sub-agent, the same way a user interjects: "
        "the sub-agent reads it at its next step boundary. Only works while it is "
        "still running; finished sub-agents fail clearly (delegate a new task "
        "instead). The receipt means 'queued', not 'applied' — check subagent_list "
        "afterwards to confirm it took effect."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent_id returned by subagent (e.g. sa3).",
            },
            "message": {
                "type": "string",
                "description": "The extra requirement / correction to deliver.",
            },
        },
        "required": ["agent_id", "message"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    def __init__(self, manager: subagents.SubagentManager | None = None) -> None:
        self._manager = manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        _parent, failure = _require_parent(self.name)
        if failure is not None:
            return failure
        agent_id = str(kwargs.get("agent_id") or "").strip()
        if not agent_id:
            return bad(self.name,
                       "agent_id 不能为空：请填 subagent 返回的那个 id（例如 sa3）。"
                       "不确定有哪些在跑时先用 subagent_list 查。")
        message = str(kwargs.get("message") or "").strip()
        if not message:
            return bad(self.name,
                       "message 不能为空：请写清要补充/更正的具体内容。"
                       "空消息不会被投递，也不该占用一次工具调用。")
        result = await _manager_of(self).send(
            agent_id, message, session_id=subagents.current_session_id())
        return _from_manager(self.name, result)


# ═══════════════════════════════════════════════════════════
# subagent_stop —— 停止
# ═══════════════════════════════════════════════════════════


class SubagentStopTool(AbstractTool):
    """停止一个仍在运行的子智能体（幂等：已结束的会明确说明）。"""

    name = "subagent_stop"
    description = (
        "Cancel a running sub-agent. Make sure you no longer need its work — the "
        "partial output is kept in the result. Idempotent: if it already finished, "
        "the call reports that state instead of failing. Use subagent_list to find "
        "agent_id values."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent_id returned by subagent (e.g. sa3).",
            },
        },
        "required": ["agent_id"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    def __init__(self, manager: subagents.SubagentManager | None = None) -> None:
        self._manager = manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        _parent, failure = _require_parent(self.name)
        if failure is not None:
            return failure
        agent_id = str(kwargs.get("agent_id") or "").strip()
        if not agent_id:
            return bad(self.name,
                       "agent_id 不能为空：请填 subagent 返回的那个 id（例如 sa3）。"
                       "不确定有哪些在跑时先用 subagent_list 查。")
        result = await _manager_of(self).stop(
            agent_id, session_id=subagents.current_session_id())
        return _from_manager(self.name, result)


# ═══════════════════════════════════════════════════════════
# 注册入口（**不自动注册** —— 由 Agent 在 multi_agent 特性下调用）
# ═══════════════════════════════════════════════════════════

#: 四个工具类（顺序即建议的注册顺序；Lead 可在特性可用时直接实例化）
SUBAGENT_TOOL_CLASSES: tuple[type[AbstractTool], ...] = (
    SubagentTool,
    SubagentListTool,
    SubagentSendTool,
    SubagentStopTool,
)


def subagent_tools(manager: subagents.SubagentManager | None = None) -> list[AbstractTool]:
    """构造四个子智能体工具实例（**只构造，不注册**）。

    由 Agent 在 ``multi_agent`` 特性可用时统一注册::

        for tool in subagent_tools():
            self.tool_registry.register(tool)

    显式传入 ``manager`` 时四个工具共用它（测试/多会话隔离场景）；
    不传则都走进程级单例，保证"派发/列表/停止"看到同一份注册表。
    """
    return [cls(manager) for cls in SUBAGENT_TOOL_CLASSES]


__all__ = [
    "RESULT_PER_AGENT_CHARS",
    "RESULT_TOTAL_CHARS",
    "SUBAGENT_TOOL_CLASSES",
    "SubagentListTool",
    "SubagentSendTool",
    "SubagentStopTool",
    "SubagentTool",
    "subagent_tools",
]
