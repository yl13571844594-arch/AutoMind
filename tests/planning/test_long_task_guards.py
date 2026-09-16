"""长任务治理的三道新防线 —— 单步超时 / 阻塞调用挪出事件循环 / 整轮任务预算。

## 修的三件事

1. **ReAct 单步超时**：ReAct 路径此前**完全没有**单步上限（Plan 路径有
   ``goal_timeout``），而编程/工作模式走的恰恰是 ReAct —— 一个工具挂住
   （等网络、等子进程、等系统锁），整个任务就停在那里。
2. **同步阻塞调用挪出事件循环**：工具都是 ``async def``，但函数体里藏着
   ``pyperclip`` / ``subprocess.run``。它们等待期间不释放事件循环，后果不只是
   "这个工具慢"，而是**整个进程一起冻住**，且任何 ``asyncio.wait_for``
   都不会触发（定时器轮不到执行）—— 也就是第 1 条会形同虚设。
3. **整轮任务总时长预算**：单步超时管"某一步卡住"、无进展检测管"原地打转"，
   都管不住"每步都正常但整体就是跑不完"。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from automind.core.types import (
    Action,
    Goal,
    GoalStatus,
    HierarchicalPlan,
    LLMResponse,
    PermissionTier,
    ToolCall,
    ToolResult,
)
from automind.planning.plan_executor import PlanExecutor
from automind.planning.react_executor import ReActExecutor
from automind.tools._toolkit import run_blocking
from automind.tools.permissions import PermissionEngine

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str, tier: PermissionTier = PermissionTier.SAFE) -> None:
        self.name = name
        self.description = name
        self.permission_tier = tier
        self.parameters = {"properties": {}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []}}


class _SlowRegistry:
    """dispatch 挂住不返回 —— 模拟"工具卡住"（网络 / 子进程 / 系统锁）。"""

    def __init__(self, hang_seconds: float = 3600, mode: str = "async") -> None:
        self.hang = hang_seconds
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
        if self.mode == "async":
            await asyncio.sleep(self.hang)
        else:
            time.sleep(self.hang)
        return ToolResult(tool_name=name, success=True, output="too late")


class _OneCallLLM:
    """先请求若干次 stuck_tool（参数各异，**不触发无进展拦截**），再收尾。"""

    def __init__(self, n_calls: int = 1, args: dict | None = None,
                 vary: bool = False) -> None:
        self.n = n_calls
        self.args = args or {}
        self.vary = vary
        self.iterations = 0

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.iterations += 1
        if self.iterations <= self.n:
            args = dict(self.args)
            if self.vary:
                # 参数每次都不同 —— 否则会先撞上"重复动作"拦截，
                # 就测不到"总时长预算"这条线了
                args["step"] = self.iterations
            return LLMResponse(
                text="调用工具",
                tool_calls=[ToolCall(id=str(self.iterations), name="stuck_tool",
                                     arguments=args)])
        return LLMResponse(text="完成")


def _plan(*tools: str) -> HierarchicalPlan:
    plan = HierarchicalPlan(task_description="t", root_goal=Goal(description="r"))
    goals = [Goal(description=f"步骤 {i}: {t}",
                  assigned_action=Action(tool_name=t, parameters={}))
             for i, t in enumerate(tools)]
    plan.root_goal.children = goals
    plan.execution_order = [g.id for g in goals]
    return plan


def _plan_chain(n: int) -> HierarchicalPlan:
    """``n`` 个步骤的计划 —— 配合 ``parallel=False`` 强制一步一个。

    无依赖的目标会被 ``asyncio.gather`` 并行成一批一起跑完，那样"批次之间"
    的预算检查自然拦不住（不是漏判，而是它们本来就一起结束了）。
    要验证"到点不再开新步骤"，必须先让执行真的是一步一个。
    """
    plan = HierarchicalPlan(task_description="chain", root_goal=Goal(description="r"))
    goals = [Goal(description=f"步骤 {i}: stuck_tool",
                  assigned_action=Action(tool_name="stuck_tool", parameters={}))
             for i in range(n)]
    plan.root_goal.children = goals
    plan.execution_order = [g.id for g in goals]
    return plan


# ═══════════════════════════════════════════════════════════
# 1. ReAct 单步超时
# ═══════════════════════════════════════════════════════════


class TestReactPerStepTimeout:
    def test_hanging_tool_is_cut_off_and_the_loop_continues(self):
        """挂住的工具被掐掉，循环**继续**（而不是整个任务停在那里）。"""
        reg = _SlowRegistry()
        llm = _OneCallLLM(n_calls=1)
        ex = ReActExecutor(llm, reg, max_iterations=4, tool_timeout=0.4)

        t0 = time.perf_counter()
        text = asyncio.run(ex.run("干活"))
        elapsed = time.perf_counter() - t0

        assert elapsed < 5, f"任务被一个挂住的工具拖住了（{elapsed:.1f}s）"
        assert ex.tool_timeouts == 1
        assert llm.iterations == 2, "超时后没有继续下一轮，模型没机会换做法"
        assert "完成" in text

    def test_timeout_result_tells_the_model_what_to_do(self):
        """超时结果必须能指导下一步 —— 否则模型会原样再试一遍。"""
        reg = _SlowRegistry()
        ex = ReActExecutor(_OneCallLLM(), reg, max_iterations=3, tool_timeout=0.3)
        asyncio.run(ex.run("干活"))
        err = next(r.error for _, r in ex.actions if not r.success)
        assert "超过" in err and "已丢弃" in err, err
        assert "background=true" in err, "没告诉模型可以走后台通道"
        assert "timeout" in err, "没告诉模型可以申请更长的 timeout"
        assert "不要再原样重试" in err

    def test_timeout_result_is_marked_in_metadata(self):
        reg = _SlowRegistry()
        ex = ReActExecutor(_OneCallLLM(), reg, max_iterations=3, tool_timeout=0.3)
        asyncio.run(ex.run("干活"))
        r = next(r for _, r in ex.actions if not r.success)
        assert r.metadata.get("timeout") is True
        assert r.metadata.get("timeout_s") == pytest.approx(0.3, abs=0.01)

    def test_repeated_timeouts_trip_the_existing_breaker(self):
        """连续超时要复用已有的熔断链路 —— 否则模型会一直等到超时上限耗尽。"""
        reg = _SlowRegistry()
        ex = ReActExecutor(_OneCallLLM(n_calls=9), reg, max_iterations=9,
                           tool_timeout=0.25, repeat_threshold=99)
        asyncio.run(ex.run("干活"))
        # 同一个工具连续失败到阈值后，熔断会接手（不再真的去 dispatch）
        assert ex.tool_timeouts >= ReActExecutor.FAILURE_THRESHOLD
        assert reg.calls <= ex.tool_timeouts + 1

    def test_timeout_event_is_pushed_to_the_caller(self):
        reg = _SlowRegistry()
        seen: list[dict] = []

        async def on_timeout(ev):
            seen.append(ev)

        async def _go():
            ex = ReActExecutor(_OneCallLLM(), reg, max_iterations=3,
                               tool_timeout=0.25)
            await ex.run("干活", on_timeout=on_timeout)
            return ex

        ex = asyncio.run(_go())
        assert seen and seen[0]["type"] == "tool_timeout"
        assert seen[0]["tool"] == "stuck_tool"
        assert seen[0]["timeout_s"] == pytest.approx(0.25, abs=0.01)
        assert ex.token_report()["tool_timeouts"] == 1

    def test_model_can_request_a_longer_timeout(self):
        """模型显式给 timeout 时应当被尊重（否则"命令允许跑 600s、外层 300s 就掐"）。"""
        ex = ReActExecutor(_OneCallLLM(), _SlowRegistry(), tool_timeout=300.0,
                           tool_timeout_max=1800.0)
        assert ex._effective_timeout({}) == 300.0
        assert ex._effective_timeout({"timeout": 600}) == 600.0
        assert ex._effective_timeout({"timeout": 99999}) == 1800.0, "没有上限"
        assert ex._effective_timeout({"timeout": "abc"}) == 300.0, "乱填会放宽限制"
        assert ex._effective_timeout({"timeout": -1}) == 300.0

    def test_zero_disables_the_timeout(self):
        reg = _SlowRegistry(hang_seconds=0.2)
        ex = ReActExecutor(_OneCallLLM(), reg, max_iterations=3, tool_timeout=0)
        t0 = time.perf_counter()
        asyncio.run(ex.run("干活"))
        assert time.perf_counter() - t0 >= 0.2, "tool_timeout=0 应当不限（恢复旧行为）"
        assert ex.tool_timeouts == 0

    def test_timeout_shows_up_in_the_partial_report(self):
        reg = _SlowRegistry()
        ex = ReActExecutor(_OneCallLLM(n_calls=99), reg, max_iterations=2,
                           tool_timeout=0.2, repeat_threshold=99)
        asyncio.run(ex.run("干活"))
        rep = ex.partial_report("干活")
        assert rep["tool_timeouts"] >= 1
        text = ReActExecutor.render_manifest(rep)
        assert "单步超时" in text, text


# ═══════════════════════════════════════════════════════════
# 2. 阻塞调用挪出事件循环
# ═══════════════════════════════════════════════════════════


class TestBlockingCallsLeaveTheEventLoop:
    """核心断言：一个同步阻塞的工具在跑时，**其它协程仍在推进**。"""

    def test_other_coroutines_keep_running_during_a_blocking_tool(self):
        ticks: list[float] = []

        async def _heartbeat():
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 0.35:
                await asyncio.sleep(0.02)
                ticks.append(time.perf_counter())

        async def _scenario():
            # 2. 与心跳**并发**执行一个阻塞 0.3 秒的同步调用
            await asyncio.gather(_heartbeat(), run_blocking(time.sleep, 0.3))

        asyncio.run(_scenario())
        assert len(ticks) >= 8, (
            f"阻塞期间心跳只跳了 {len(ticks)} 次 —— 事件循环被占住了")

    def test_run_blocking_returns_the_value(self):
        assert asyncio.run(run_blocking(lambda a, b=0: a + b, 2, b=3)) == 5

    def test_run_blocking_propagates_exceptions(self):
        def _boom():
            raise ValueError("炸了")
        with pytest.raises(ValueError, match="炸了"):
            asyncio.run(run_blocking(_boom))

    def test_wait_for_timeout_works_when_the_blocking_call_is_offloaded(self):
        """这是第 1 条能成立的前提：阻塞被挪走后，wait_for 才真的会触发。"""
        async def _scenario():
            t0 = time.perf_counter()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(run_blocking(time.sleep, 5), timeout=0.3)
            return time.perf_counter() - t0

        elapsed = asyncio.run(_scenario())
        assert elapsed < 2, f"wait_for 没有按时触发（{elapsed:.1f}s）"

    @pytest.mark.parametrize("module,needle", [
        ("automind.tools.system_tools", "run_blocking"),
        ("automind.tools.collab_tools", "run_blocking"),
        ("automind.tools.media_tools", "run_blocking"),
    ])
    def test_tools_that_block_now_offload(self, module, needle):
        import importlib
        src = importlib.import_module(module).__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        assert needle in text, f"{module} 里的阻塞调用没有挪出事件循环"

    def test_no_bare_blocking_subprocess_calls_left_in_tools(self):
        """扫描式护栏：工具目录里不得再有"裸"跑的同步 subprocess。

        单点测试只能证明"我改过的地方还在"，拦不住**新加**的工具又写成同步的。
        判定规则（按 AST）：

          · 直接 ``subprocess.run(...)`` —— 只有当它所在的方法**整体**被
            ``run_blocking(self._xxx, ...)`` 调用时才算合规；
          · ``await run_blocking(subprocess.run, ...)`` —— 合规。
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "automind" / "tools"
        offenders: list[str] = []

        for p in root.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            #: 被 run_blocking(...) 显式调用的方法名（这些方法内部阻塞是安全的）
            offloaded: set[str] = set()
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "run_blocking"):
                    for arg in node.args:
                        if isinstance(arg, ast.Attribute):
                            offloaded.add(arg.attr)
            #: 每个函数体内的裸 subprocess 调用
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    f = node.func
                    if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                            and f.value.id == "subprocess"
                            and f.attr in ("run", "call", "check_call", "check_output")):
                        if fn.name not in offloaded:
                            offenders.append(
                                f"{p.relative_to(root.parent.parent)}:{node.lineno}"
                                f"（{fn.name}）")

        assert not offenders, (
            "以下同步 subprocess 调用会把事件循环一起等死，请改用 "
            "`await run_blocking(subprocess.run, ...)`（或把整个方法交给 "
            "`run_blocking`）：\n  " + "\n  ".join(offenders))

    def test_to_thread_helper_is_publicly_documented(self):
        from automind.tools import _toolkit
        assert "事件循环" in (_toolkit.run_blocking.__doc__ or "")


# ═══════════════════════════════════════════════════════════
# 3. 整轮任务总时长预算
# ═══════════════════════════════════════════════════════════


class TestTaskBudget:
    """预算到点 = 不再开新步骤，但**已完成的工作全部保留**。

    与"重复动作"要分开测：两个机制都会提前收尾，所以这里让参数每轮不同，
    否则会先撞上无进展拦截，根本测不到预算这条线。
    """

    def test_react_stops_opening_new_steps_at_the_deadline(self):
        reg = _SlowRegistry(hang_seconds=0.15)   # 每步都慢，但都会正常完成
        llm = _OneCallLLM(n_calls=99, vary=True)
        ex = ReActExecutor(llm, reg, max_iterations=50, tool_timeout=0,
                           repeat_threshold=99)

        deadline = time.monotonic() + 0.5
        asyncio.run(ex.run("长活", deadline=deadline))

        assert ex.stop_reason == "task_budget", ex.stop_reason
        assert llm.iterations < 50, f"预算到点却仍跑了 {llm.iterations} 轮"
        assert ex.iterations_used < 50

    def test_budget_stop_reason_is_explained_in_the_manifest(self):
        reg = _SlowRegistry(hang_seconds=0.1)
        ex = ReActExecutor(_OneCallLLM(n_calls=99, vary=True), reg,
                           max_iterations=50, tool_timeout=0, repeat_threshold=99)
        text = asyncio.run(ex.run("长活", deadline=time.monotonic() + 0.35))
        assert "总时长预算" in text, text
        assert "已完成的工作全部保留" in text

    def test_no_deadline_means_no_limit(self):
        reg = _SlowRegistry(hang_seconds=0.05)
        ex = ReActExecutor(_OneCallLLM(n_calls=3, vary=True), reg, max_iterations=6,
                           tool_timeout=0, repeat_threshold=99)
        asyncio.run(ex.run("活"))
        assert ex.stop_reason == "no_more_tools"

    def test_plan_executor_skips_remaining_goals_at_the_deadline(self):
        """串行执行（``parallel=False``）时，预算到点就不该再开新步骤。"""
        reg = _SlowRegistry(hang_seconds=0.4)
        ex = PlanExecutor(None, reg,  # type: ignore[arg-type]
                          permission_engine=PermissionEngine(approval_mode="approve_all"),
                          auto_retry=False, goal_timeout=30, parallel=False)
        report = asyncio.run(ex.execute(
            _plan_chain(3), deadline=time.monotonic() + 0.25))

        skipped = [s for s in report.steps if "预算已到点" in s.error]
        assert skipped, "预算到点后剩余目标没有被标记"
        assert any("max_task_seconds" in s.error for s in skipped), \
            "没告诉用户怎么放开这个限制"
        assert report.completed_steps + len(skipped) == 3

    def test_plan_executor_keeps_completed_work_when_budget_hits(self):
        """预算到点不能把已经做完的步骤一起丢掉。"""
        reg = _SlowRegistry(hang_seconds=0.12)
        ex = PlanExecutor(None, reg,  # type: ignore[arg-type]
                          permission_engine=PermissionEngine(approval_mode="approve_all"),
                          auto_retry=False, goal_timeout=30, parallel=False)
        report = asyncio.run(ex.execute(
            _plan_chain(6), deadline=time.monotonic() + 0.4))
        assert report.completed_steps >= 1, "已完成的成果被预算搞丢了"
        assert report.total_steps < 6, "预算到点后仍在开新步骤"

    # ── 顺带发现的真 bug：子任务缓存跨目标误判 ──────────────

    def test_plan_read_cache_does_not_dedupe_across_different_goals(self):
        """两个**不同**的目标做同一次只读调用，不能算作"已完成"。

        ``_subtask_cache`` 的键此前是"工具名 + 参数"，不含目标身份。于是两个
        语义完全不同的目标只要调了同一个工具、同一组参数（例如都要读同一份
        配置或同一个文件），第二个就会被**当成缓存命中直接判成功** ——
        它其实一步都没执行，报告里却记成"已完成"。

        这个 bug 是在给"总时长预算"写测试时撞出来的：6 个步骤只真的跑了 1 次。
        """
        reg = _SlowRegistry(hang_seconds=0.05)
        ex = PlanExecutor(None, reg,  # type: ignore[arg-type]
                          permission_engine=PermissionEngine(approval_mode="approve_all"),
                          auto_retry=False, goal_timeout=30, parallel=False)
        report = asyncio.run(ex.execute(_plan_chain(3)))

        assert reg.calls == 3, (
            f"3 个不同的目标只执行了 {reg.calls} 次 —— 后面的被缓存顶掉了"
            f"（cache_hits={ex.cache_hits}）")
        assert report.completed_steps == 3

    def test_plan_cache_still_helps_within_one_goal(self):
        """同一个目标内的重试/重复调用仍应命中缓存（别把缓存一起改废了）。"""
        reg = _SlowRegistry(hang_seconds=0.02)
        ex = PlanExecutor(None, reg,  # type: ignore[arg-type]
                          permission_engine=PermissionEngine(approval_mode="approve_all"),
                          auto_retry=False, goal_timeout=30)
        plan = _plan_chain(1)
        goal = plan.root_goal.children[0]

        async def _twice():
            await ex._execute_goal(goal)
            goal.status = GoalStatus.PENDING          # 模拟纠错后重跑同一目标
            return await ex._execute_goal(goal)

        asyncio.run(_twice())
        assert reg.calls == 1, f"同一目标内的重复只读调用没有复用（{reg.calls} 次）"
        assert ex.cache_hits == 1

    def test_agent_exposes_the_deadline_to_both_executors(self):
        """接线检查：agent 必须把预算真的传下去，否则配置是个摆设。"""
        import inspect

        from automind import agent as agent_mod
        src = inspect.getsource(agent_mod.AutoMindAgent._run_impl_bound)
        assert "_task_deadline" in src and "max_task_seconds" in src
        src_react = inspect.getsource(agent_mod.AutoMindAgent._run_react)
        assert "deadline=self._task_deadline" in src_react
        src_plan = inspect.getsource(agent_mod.AutoMindAgent._run_plan_execute)
        assert "deadline=self._task_deadline" in src_plan
