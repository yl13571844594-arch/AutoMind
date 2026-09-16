"""「任务还会不会卡住」的边界 —— v1.7.0 修复前后各是什么样。

## 两个已经查清并修掉的缺口

v1.7.0 的第一轮修掉了三类最常见的卡死（重复动作空转、并行批挂起、审批等待
挂死），但留了两个缺口，当时用"记录缺口的失败性断言"钉在这里：

    1. ReAct 路径没有单步超时 —— 工具挂住就是真的挂住（Plan 路径有）。
    2. 同步阻塞调用会把事件循环一起占死，连 ``asyncio.wait_for`` 都不触发。

**这两个缺口在 v1.7.0 的收尾里已经补上**，因此下面翻成了正面断言：
ReAct 现在有单步超时，阻塞调用已经挪出事件循环。

保留这段历史的原因：这些边界当初是"没人知道"的状态，写下来之后才被修掉。
将来若要放宽（例如把 ``react_tool_timeout_seconds`` 默认改成 0），
这几条会失败 —— 那正是提醒"你在重新打开一个已知的卡死路径"。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from automind.core.types import (
    Action,
    Goal,
    HierarchicalPlan,
    LLMResponse,
    PermissionTier,
    ToolCall,
    ToolResult,
)
from automind.planning.plan_executor import PlanExecutor
from automind.planning.react_executor import ReActExecutor
from automind.tools.permissions import PermissionEngine

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.permission_tier = PermissionTier.SAFE
        self.parameters = {"properties": {}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []}}


class _Reg:
    """dispatch 永不返回 —— 模拟"工具卡住"（网络读、子进程、系统锁）。"""

    def __init__(self, mode: str = "async_hang") -> None:
        self.mode = mode
        self.calls = 0

    def list_names(self):
        return ["stuck_tool"]

    def list_all(self):
        return [_Tool("stuck_tool")]

    def get(self, name):
        return _Tool(name)

    async def dispatch(self, name, **kw):
        self.calls += 1
        if self.mode == "async_hang":
            await asyncio.sleep(3600)          # 挂在 await 点上
        else:
            time.sleep(20)                     # 同步阻塞（曾占死事件循环）
        return ToolResult(tool_name=name, success=True, output="never")


class _OneCallLLM:
    def __init__(self, n_calls: int = 1, vary: bool = False) -> None:
        self.n = n_calls
        self.vary = vary
        self.iterations = 0

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.iterations += 1
        if self.iterations <= self.n:
            args: dict = {"step": self.iterations} if self.vary else {}
            return LLMResponse(text="调用工具",
                               tool_calls=[ToolCall(id=str(self.iterations),
                                                    name="stuck_tool", arguments=args)])
        return LLMResponse(text="完成")


def _plan(*tools: str) -> HierarchicalPlan:
    plan = HierarchicalPlan(task_description="t", root_goal=Goal(description="r"))
    goals = [Goal(description=f"步骤 {i}: {t}",
                  assigned_action=Action(tool_name=t, parameters={}))
             for i, t in enumerate(tools)]
    plan.root_goal.children = goals
    plan.execution_order = [g.id for g in goals]
    return plan


# ═══════════════════════════════════════════════════════════
# 缺口 1（已修）：ReAct 现在有单步超时
# ═══════════════════════════════════════════════════════════


class TestReActNowHasAPerStepTimeout:
    """ReAct 是编程/工作模式真正走的路径 —— 它现在有 ``tool_timeout``。

    修之前：ReAct 里任一工具调用不返回，整个任务就停在那里（实测挂住 > 6 秒
    且没有任何收尾）。现在超时会转成该动作的失败结果喂回模型，循环继续。
    """

    def test_react_no_longer_stalls_forever(self):
        ex = ReActExecutor(_OneCallLLM(), _Reg("async_hang"), max_iterations=3,
                           tool_timeout=0.3)
        t0 = time.perf_counter()
        text = asyncio.run(asyncio.wait_for(ex.run("干活"), timeout=6))
        assert time.perf_counter() - t0 < 6, "仍然会无限期挂住"
        assert ex.tool_timeouts == 1
        assert "完成" in text, "超时后循环没有继续"

    def test_plan_path_also_has_a_timeout(self):
        """对照面：Plan 路径的 ``goal_timeout``（v1.7.0 第一轮新增）依然有效。"""
        ex = PlanExecutor(None, _Reg("async_hang"),  # type: ignore[arg-type]
                          permission_engine=PermissionEngine(approval_mode="approve_all"),
                          auto_retry=False, goal_timeout=0.5)
        report = asyncio.run(ex.execute(_plan("stuck_tool")))
        assert not report.steps[0].success
        assert ex.timed_out == 1


# ═══════════════════════════════════════════════════════════
# 缺口 2（已修）：阻塞调用已挪出事件循环
# ═══════════════════════════════════════════════════════════


class TestBlockingCallsNoLongerFreezeTheLoop:
    """`asyncio.wait_for` 靠事件循环的定时器触发；循环被同步调用占死时它不触发。

    修之前实测：``goal_timeout=2`` 配一个 ``time.sleep(20)`` 的同步工具，
    实际耗时 20.0 秒、超时**未生效**。

    现在两处都改掉了：``run_blocking`` 把它们挪进线程池，
    于是"这一步有上限"才真正成立。``tests/planning/test_long_task_guards.py``
    里有正向的行为验证（阻塞期间心跳仍在跳）。
    """

    def test_goal_timeout_is_not_defeated_by_an_offloaded_call(self):
        """超时对**异步等待**有效 —— 而所有会阻塞的调用现在都是异步等待了。"""
        async def _scenario():
            t0 = time.perf_counter()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.to_thread(time.sleep, 5), timeout=0.3)
            return time.perf_counter() - t0

        assert asyncio.run(_scenario()) < 2

    def test_clipboard_tool_no_longer_blocks_the_loop(self):
        """剪贴板是当年最典型的"事件循环占用点"，现在改走 run_blocking。"""
        import inspect

        from automind.tools import system_tools
        src = inspect.getsource(system_tools.ClipboardTool.execute)
        assert "pyperclip" in src
        assert "run_blocking" in src, "剪贴板又变回同步阻塞调用了"

    def test_no_bare_blocking_subprocess_calls_anywhere_in_tools(self):
        """扫描式护栏（与 test_long_task_guards 同一条约束，这里独立复核）。"""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "automind" / "tools"
        offenders: list[str] = []
        for p in root.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            offloaded = {
                arg.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "run_blocking"
                for arg in node.args if isinstance(arg, ast.Attribute)
            }
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                if fn.name in offloaded:
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "subprocess"
                            and node.func.attr in ("run", "call", "check_call",
                                                   "check_output")):
                        offenders.append(f"{p.name}:{node.lineno}（{fn.name}）")
        assert not offenders, "仍有裸的同步 subprocess 调用：\n  " + "\n  ".join(offenders)
