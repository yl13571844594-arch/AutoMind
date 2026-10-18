"""`agent` 步骤的真实执行器（v1.8.0，R-106）。

执行器（executor.py）只认一个注入的 ``agent_runner(step, goal) -> dict``，
自己不碰 ReAct、不碰模型 —— 这样它仍然可以离线测试、离线 dry-run。本模块提供
生产用的那个 runner：用一个 **ReActExecutor** 把子目标跑完，同时把自主性关进
三道评审方看得见的边界里：

1. **工具集**（D1）= 工作流声明的 ``allowed_tools`` ∩ 运行时实际可用的工具。
   没声明时只给只读（SAFE 级）工具。越界调用不会执行，并如实写进报告
   （``denied_tools``）—— 评审方要能看到"它试图做允许范围之外的事"。
2. **步数** = ``max_steps``（ReAct 的迭代上限）。
3. **token** = ``token_budget``（本步骤单独记账；0 = 不单独设限，仍受整个任务的
   预算约束）。耗尽时走 ReAct 既有的"部分交付"路径，不会丢掉已做完的动作。

步骤内部的工具审批与工作流 ``human`` 步骤共用同一条审批链路（同一张待决表、
同一套超时处置、同一个外部回执端点），但审批请求里带上
``workflow_run_id`` / ``step_id``，界面据此显示"来自工作流 X 第 N 步"。

输出结构（D6）：``{answer, artifacts[], actions_ok, actions_failed, stop_reason}``，
另附 ``denied_tools`` / ``allowed_tools`` / ``iterations`` / ``tokens`` 供报告使用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from automind.core.exceptions import BudgetExhaustedError
from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import ToolRegistry, resolve_name

logger = get_logger("automind.workflow.agent_step")

#: 给 Agent 的补充说明（附在任务上下文里）。刻意写成"约束 + 交付要求"两段：
#: 工作流里的 agent 步骤产出要被**后续步骤**读，结论必须能被程序化引用。
_CONTEXT_TEMPLATE = (
    "你正在执行工作流「{workflow}」中的一个步骤（id={step_id}）。\n"
    "约束：\n"
    "* 只能使用以下工具：{tools}。其它工具的调用会被拒绝并记入审计报告。\n"
    "* 最多 {max_steps} 步；做完即停，不要做与目标无关的事。\n"
    "交付：完成后用简洁的文字给出结论；若产出了文件，写明相对路径。"
    "你的最终回答会被后续步骤以 {{{{ steps.{step_id}.output.answer }}}} 引用。"
)


class RestrictedToolRegistry(ToolRegistry):
    """只暴露 ``allowed`` 那几个工具的注册表视图（工具实例与原注册表共享）。

    为什么子类化而不是新建一个空注册表再挨个 register：``dispatch`` 需要知道
    "这个名字在原注册表里存不存在" —— 存在但不在白名单里是**越权**，要给出
    明确的拒绝原因；根本不存在是**拼错**，走原有的"未知工具 + 建议"。
    两种情形对模型的指引完全不同。
    """

    def __init__(self, base: ToolRegistry, allowed: list[str] | None) -> None:
        super().__init__()
        self.base = base
        names = base.list_names()
        if allowed is None:
            chosen = [n for n in names if _tier_of(base, n) == PermissionTier.SAFE]
            self.policy = "safe_only"
        else:
            chosen = [n for n in allowed if n in names]
            self.policy = "declared"
        for name in chosen:
            self._tools[name] = base.get(name)
        #: 声明了、但运行时不存在的工具（MCP 没挂上等）—— 报告里要能看到
        self.missing = [n for n in (allowed or []) if n not in names]

    async def dispatch(self, tool_name: str, **kwargs: Any) -> ToolResult:
        own, _ = resolve_name(tool_name, self.list_names())
        if own is None:
            real, _ = resolve_name(tool_name, self.base.list_names())
            if real is not None:
                logger.warning("workflow_agent_tool_denied", tool=real)
                return ToolResult(
                    tool_name=real, success=False, error=denied_message(real, self),
                    metadata={"workflow_denied": True})
        return await super().dispatch(tool_name, **kwargs)


def denied_message(tool: str, registry: RestrictedToolRegistry) -> str:
    allowed = "、".join(registry.list_names()) or "（无）"
    why = ("该步骤未声明 allowed_tools，只提供只读工具" if registry.policy == "safe_only"
           else "它不在本步骤声明的 allowed_tools 里")
    return (f"工具 {tool} 被工作流拒绝：{why}。本步骤可用的工具：{allowed}。"
            "请只用这些工具完成目标；做不到就直接说明缺什么。")


def _tier_of(registry: ToolRegistry, name: str) -> Any:
    try:
        return getattr(registry.get(name), "permission_tier", None)
    except Exception:                                   # pragma: no cover - 防御性
        return None


class StepBudgetLLM:
    """给单个 agent 步骤单独记 token 账的 LLM 包装。

    超出 ``budget`` 时抛 ``BudgetExhaustedError`` —— ReAct 循环对它有专门的
    收尾分支（stop_reason = task_budget，交部分交付清单），因此预算用尽不会
    让已做完的动作丢失。其余属性全部透传给原 LLM（用量回调、预算钩子照常生效，
    整个任务的总预算依旧约束着这一步）。
    """

    def __init__(self, backend: Any, budget: int = 0) -> None:
        self._backend = backend
        self.budget = max(0, int(budget or 0))
        self.used = 0

    async def generate(self, messages: Any, tools: Any = None, stop: Any = None) -> Any:
        if self.budget and self.used >= self.budget:
            raise BudgetExhaustedError(
                f"本步骤的 token 预算已用尽（已用 {self.used} / 预算 {self.budget}）")
        resp = await self._backend.generate(messages, tools=tools, stop=stop)
        try:
            self.used += int(getattr(resp, "prompt_tokens", 0) or 0) \
                + int(getattr(resp, "completion_tokens", 0) or 0)
        except (TypeError, ValueError):                 # pragma: no cover - 假对象
            pass
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)


ApprovalFactory = Callable[[Any], Callable[..., Awaitable[Any]] | None]


def make_agent_runner(
    agent: Any,
    *,
    workflow: str = "",
    approval_for_step: ApprovalFactory | None = None,
    on_event: Callable[[str, dict[str, Any]], Any] | None = None,
) -> Callable[[Any, str], Awaitable[dict[str, Any]]]:
    """构造 ``agent_runner``：拿会话 Agent 的模型、权限引擎与工具注册表跑一个子目标。

    ``approval_for_step(step)`` 返回该步骤内部工具审批用的回调
    （签名同 ReAct：``async (tool, args, tier, reason) -> bool|dict``）；
    返回 None = 没有审批通道，需要审批的动作一律按拒绝处理（fail-closed）。
    """

    async def _runner(step: Any, goal: str) -> dict[str, Any]:
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.output_budget import limits_from_config

        if getattr(agent, "llm", None) is None:
            raise RuntimeError("模型未初始化：请先在「API Keys」面板配置当前提供商的 Key")
        registry = RestrictedToolRegistry(agent.tool_registry, step.allowed_tools)
        allowed = registry.list_names()
        base_cb = approval_for_step(step) if approval_for_step is not None else None

        async def _approval(tool_name: str, args: Any, tier: str, reason: str) -> Any:
            if tool_name not in allowed:
                # 越权的调用连审批都不该弹：批了也不会执行，只会让人白点一次
                return {"approved": False, "arguments": None,
                        "comment": denied_message(tool_name, registry)}
            if base_cb is None:
                return {"approved": False, "arguments": None,
                        "comment": "工作流运行没有可用的审批通道，已按拒绝处理"}
            return await base_cb(tool_name, args, tier, reason)

        ex = agent.config.execution
        llm = StepBudgetLLM(agent.llm, step.token_budget)
        react = ReActExecutor(
            llm, registry,
            max_iterations=int(step.max_steps),
            permissions=getattr(agent, "permissions", None),
            approval_cb=_approval,
            auto_validate=bool(getattr(ex, "auto_test", True)),
            tool_budget=0,                      # 工具已被白名单收窄，全部下发
            output_limits=limits_from_config(ex),
            repeat_threshold=int(getattr(ex, "react_repeat_action_threshold", 2) or 2),
            result_cache=bool(getattr(ex, "react_result_cache", True)),
            no_progress_limit=int(getattr(ex, "react_no_progress_limit", 12) or 0),
            tool_timeout=float(getattr(ex, "react_tool_timeout_seconds", 300.0) or 0),
            tool_timeout_max=float(getattr(ex, "tool_timeout_max_seconds", 1800.0) or 0),
        )
        context = _CONTEXT_TEMPLATE.format(
            workflow=workflow or "未命名", step_id=step.id,
            tools="、".join(allowed) or "（无）", max_steps=step.max_steps)

        async def _on_action(tc: Any, result: Any) -> None:
            if on_event is None:
                return
            try:
                ret = on_event("agent_action", {
                    "id": step.id, "tool": getattr(tc, "name", ""),
                    "ok": bool(getattr(result, "success", False)),
                    "error": str(getattr(result, "error", "") or "")[:200]})
                if asyncio.iscoroutine(ret):
                    await ret
            except asyncio.CancelledError:
                raise
            except Exception:                         # pragma: no cover - 观测兜底
                pass

        answer = ""
        try:
            answer = await react.run(goal, context, on_action=_on_action)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 已做过的动作不能因为一次异常就从报告里消失：照常交出进度
            logger.warning("workflow_agent_step_error", step=step.id,
                           error=f"{type(exc).__name__}: {exc}")
            answer = f"执行中断：{type(exc).__name__}: {exc}"
            react.stop_reason = react.stop_reason or "error"
        report = react.partial_report(goal)
        attempted = [getattr(tc, "name", "") for tc, _ in react.actions]
        denied = sorted({n for n in attempted if n and n not in allowed})
        return {
            "answer": answer,
            "artifacts": list(report.get("artifacts") or []),
            "actions_ok": int(report.get("completed_actions") or 0),
            "actions_failed": int(report.get("failed_actions") or 0),
            "stop_reason": react.stop_reason or "unknown",
            "denied_tools": denied,
            "allowed_tools": allowed,
            "missing_tools": registry.missing,
            "iterations": react.iterations_used,
            "tokens": llm.used,
        }

    return _runner


__all__ = [
    "RestrictedToolRegistry",
    "StepBudgetLLM",
    "denied_message",
    "make_agent_runner",
]
