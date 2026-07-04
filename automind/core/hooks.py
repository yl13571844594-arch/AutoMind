"""Agent 生命周期钩子（§3.5）— 插件系统（§14.7）的基础设施。

`AgentHooks` 定义一组可选的生命周期回调；未设置的钩子会被跳过。
钩子既可以是同步函数也可以是协程函数，调用方（Agent）通过 `invoke_hook`
统一处理并吞掉钩子内部异常，**保证插件永远不会破坏主流程**。

多个插件各自提供一份 `AgentHooks`，`merge_hooks` 会把它们组合成一份，
同名钩子按注册顺序依次调用。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any

# 钩子字段名（供合并与校验复用）
HOOK_NAMES: tuple[str, ...] = (
    "before_run",
    "after_parse",
    "before_plan",
    "after_plan",
    "before_tool",
    "after_tool",
    "after_run",
    "on_error",
)


@dataclass
class AgentHooks:
    """Agent 生命周期钩子。所有钩子可选，未设置时跳过。

    约定的调用签名（均可同步或异步，返回值被忽略）：
        before_run(user_input: str)
        after_parse(parsed: InputMessage)
        before_plan(user_input: str)
        after_plan(plan: HierarchicalPlan)
        before_tool(tool_name: str, params: dict)
        after_tool(tool_name: str, result: ToolResult)
        after_run(result: AgentResult)
        on_error(error: Exception, user_input: str)
    """

    before_run: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    after_parse: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    before_plan: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    after_plan: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    before_tool: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    after_tool: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    after_run: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None
    on_error: Callable[..., Any] | Callable[..., Awaitable[Any]] | None = None


async def invoke_hook(hook: Callable[..., Any] | None, *args: Any) -> None:
    """调用单个钩子；同步/异步均可，异常被吞掉不影响主流程。"""
    if hook is None:
        return
    try:
        result = hook(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # 插件错误绝不冒泡到主流程
        pass


def merge_hooks(hooks_list: list[AgentHooks]) -> AgentHooks:
    """将多份 AgentHooks 合并为一份；同名钩子按顺序依次调用。"""
    merged = AgentHooks()
    for f in fields(AgentHooks):
        callables = [
            getattr(h, f.name) for h in hooks_list if getattr(h, f.name) is not None
        ]
        if not callables:
            continue
        if len(callables) == 1:
            setattr(merged, f.name, callables[0])
            continue

        async def _composite(*args: Any, _cbs: list = callables) -> None:
            for cb in _cbs:
                await invoke_hook(cb, *args)

        setattr(merged, f.name, _composite)
    return merged
