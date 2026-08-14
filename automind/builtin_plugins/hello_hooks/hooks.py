"""hello_hooks —— 示例插件：演示如何挂接全部生命周期钩子。

这是给第三方开发者抄的**完整模板**，覆盖 ``AgentHooks`` 的 8 个钩子字段
（``before_run`` / ``after_parse`` / ``before_plan`` / ``after_plan`` /
``before_tool`` / ``after_tool`` / ``after_run`` / ``on_error``）。

约定（见 ``automind/core/hooks.py``）：
- 钩子可以是同步函数或协程函数，返回值被忽略 —— 插件只做**副作用**，
  绝不影响主流程，也不会因抛异常而拖垮 Agent（异常会被框架吞掉）。
- 每个钩子的调用签名固定（详见 ``AgentHooks`` 的 docstring）。

写一个新插件只需三步：
1. 建目录 ``<plugin>/``，放本 ``plugin.json`` 与 ``hooks.py``；
2. ``hooks.py`` 里写 ``get_hooks() -> AgentHooks``，按需填你要的钩子；
3. 把目录放到插件目录（内置目录随包分发，用户目录为 ``~/.automind/plugins``）。
"""

from __future__ import annotations

import logging

from automind.core.hooks import AgentHooks

logger = logging.getLogger("automind.plugin.hello_hooks")


def get_hooks() -> AgentHooks:
    """返回本插件提供的生命周期钩子集合。"""

    async def before_run(user_input: str) -> None:
        logger.info("hello: before_run", task=(user_input or "")[:80])

    async def after_parse(parsed) -> None:
        logger.info("hello: after_parse", intent=getattr(parsed, "intent", ""))

    async def before_plan(user_input: str) -> None:
        logger.info("hello: before_plan")

    async def after_plan(plan) -> None:
        logger.info("hello: after_plan", steps=len(getattr(plan, "execution_order", []) or []))

    async def before_tool(tool_name: str, params: dict) -> None:
        logger.info("hello: before_tool", tool=tool_name)

    async def after_tool(tool_name: str, result) -> None:
        logger.info("hello: after_tool", tool=tool_name, success=getattr(result, "success", None))

    async def after_run(result) -> None:
        logger.info("hello: after_run", success=getattr(result, "success", None))

    async def on_error(error: Exception, user_input: str) -> None:
        logger.info("hello: on_error", error=type(error).__name__)

    return AgentHooks(
        before_run=before_run,
        after_parse=after_parse,
        before_plan=before_plan,
        after_plan=after_plan,
        before_tool=before_tool,
        after_tool=after_tool,
        after_run=after_run,
        on_error=on_error,
    )
