"""并行批次的**挂起**熔断 —— 异常屏障兜不住的那一半。

## 修的是什么

v1.6.4 给并行批次加了"单点异常屏障"（``_execute_goal_guarded``）：一个目标
抛异常就地转成它的失败结果，同批其它目标成果保留。这解决的是"**抛出来**"。

但 ``hang`` 不是异常：一个目标卡在网络读、子进程、审批等待上时，既不返回也不
抛错，``asyncio.gather`` 会一直等下去 —— 整批不返回，计划执行停滞，界面上
就是"没反应"。P0 级联会把这个问题放大：批次越大，撞上一个挂起步骤的概率越高。

现在每个目标有独立的执行上限（``goal_timeout``），批级还有一道兜底，
**保证 gather 一定返回**。
"""

from __future__ import annotations

import asyncio
import time

from automind.core.types import (
    Action,
    Goal,
    GoalStatus,
    HierarchicalPlan,
    PermissionDecision,
    PermissionTier,
    ToolResult,
)
from automind.planning.plan_executor import PlanExecutor
from automind.tools.permissions import PermissionEngine

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str, tier: PermissionTier = PermissionTier.SAFE) -> None:
        self.name = name
        self.description = name
        self.permission_tier = tier


class _Registry:
    """dispatch 由测试注入行为（正常 / 挂起 / 抛错）。"""

    def __init__(self, names: list[str]) -> None:
        self._tools = {n: _Tool(n) for n in names}
        self.calls: list[str] = []
        self.hang: set[str] = set()
        self.boom: set[str] = set()

    def get(self, name: str):
        return self._tools[name]

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    async def dispatch(self, name: str, **kw) -> ToolResult:
        self.calls.append(name)
        if name in self.hang:
            await asyncio.sleep(3600)
        if name in self.boom:
            raise RuntimeError("工具炸了")
        return ToolResult(tool_name=name, success=True, output={"ok": True})


def _plan(*tool_names: str) -> HierarchicalPlan:
    """一份"都是叶子、互不依赖"的计划 —— 收集时就绪 ⇒ 会并行成一批。"""
    plan = HierarchicalPlan(task_description="并行超时测试", root_goal=Goal(description="root"))
    goals = []
    for i, name in enumerate(tool_names):
        g = Goal(description=f"步骤 {i + 1}: {name}",
                 assigned_action=Action(tool_name=name, parameters={"path": f"f{i}"}))
        goals.append(g)
    plan.root_goal.children = goals
    plan.execution_order = [g.id for g in goals]
    return plan


def _executor(registry: _Registry, **kw) -> PlanExecutor:
    return PlanExecutor(None, registry,  # type: ignore[arg-type]
                        permission_engine=PermissionEngine(approval_mode="approve_all"),
                        auto_retry=False, **kw)


# ═══════════════════════════════════════════════════════════
# 1. 挂起不再拖死整批
# ═══════════════════════════════════════════════════════════


class TestHungGoalDoesNotStallTheBatch:
    def test_hanging_goal_times_out_and_others_still_succeed(self):
        reg = _Registry(["read_a", "read_b", "read_c"])
        reg.hang.add("read_b")
        ex = _executor(reg, goal_timeout=0.4)

        t0 = time.perf_counter()
        report = asyncio.run(ex.execute(_plan("read_a", "read_b", "read_c")))
        elapsed = time.perf_counter() - t0

        assert elapsed < 5, f"整批被一个挂起的目标拖住了（{elapsed:.1f}s）"
        assert ex.timed_out == 1
        by_tool = {s.goal_description.split(": ")[1]: s for s in report.steps}
        assert by_tool["read_a"].success, "同批的正常目标成果被弄丢了"
        assert by_tool["read_c"].success, "同批的正常目标成果被弄丢了"
        assert not by_tool["read_b"].success
        assert "秒" in by_tool["read_b"].error

    def test_timeout_message_distinguishes_timeout_from_failure(self):
        """超时必须说清"是超时中止"，不能与"做了但失败"混为一谈。"""
        reg = _Registry(["slow"])
        reg.hang.add("slow")
        ex = _executor(reg, goal_timeout=0.3)
        report = asyncio.run(ex.execute(_plan("slow")))
        err = report.steps[0].error
        assert "超时" in err or "未返回" in err, err
        assert "同批其它步骤不受影响" in err, err

    def test_goal_exception_is_still_contained(self):
        """v1.6.4 的异常屏障不能被超时改造弄丢。"""
        reg = _Registry(["ok_tool", "bad_tool"])
        reg.boom.add("bad_tool")
        ex = _executor(reg, goal_timeout=5)
        report = asyncio.run(ex.execute(_plan("ok_tool", "bad_tool")))
        by_tool = {s.goal_description.split(": ")[1]: s for s in report.steps}
        assert by_tool["ok_tool"].success
        assert not by_tool["bad_tool"].success

    def test_timeout_disabled_keeps_old_behaviour(self):
        """goal_timeout=0 → 不限（恢复旧行为，给排障留后路）。"""
        reg = _Registry(["a", "b"])
        ex = _executor(reg, goal_timeout=0)
        assert ex.goal_timeout == 0
        report = asyncio.run(ex.execute(_plan("a", "b")))
        assert report.completed_steps == 2

    def test_parallel_batch_still_runs_concurrently(self):
        """加了超时也不能把并行改成串行 —— 那是拿性能换稳定。"""
        reg = _Registry(["a", "b", "c", "d"])
        ex = _executor(reg, goal_timeout=10)

        async def _slow(name: str, **kw):
            reg.calls.append(name)
            await asyncio.sleep(0.2)
            return ToolResult(tool_name=name, success=True, output={"ok": True})
        reg.dispatch = _slow            # type: ignore[assignment]

        t0 = time.perf_counter()
        report = asyncio.run(ex.execute(_plan("a", "b", "c", "d")))
        elapsed = time.perf_counter() - t0
        assert report.completed_steps == 4
        assert elapsed < 0.7, f"四个 0.2s 的目标串行跑了 {elapsed:.2f}s"

    def test_serial_single_goal_timeout_also_applies(self):
        """批里只有一个目标时同样会挂 —— 那条路径也要有上限。"""
        reg = _Registry(["only"])
        reg.hang.add("only")
        ex = _executor(reg, goal_timeout=0.3)
        report = asyncio.run(ex.execute(_plan("only")))
        assert not report.steps[0].success


# ═══════════════════════════════════════════════════════════
# 2. 审批门控对**所有**动作生效（v1.7.0）
# ═══════════════════════════════════════════════════════════


class TestApprovalGateCoversEveryAction:
    def _ask_executor(self, registry: _Registry, cb) -> PlanExecutor:
        return PlanExecutor(
            None, registry,  # type: ignore[arg-type]
            permission_engine=PermissionEngine(approval_mode="ask"),
            auto_retry=False, goal_timeout=5)

    def test_unknown_tool_is_gated_instead_of_skipping_the_check(self):
        """工具名写错/未注册时，权限检查**不能**被整个跳过。

        此前整段权限判断包在 `if tool is not None:` 里：`_get_tool` 返回 None
        时审批一起被跳过，动作直接进 dispatch —— 表现为"配了「询问」也不弹窗"。
        修复后未注册工具按最高风险等级送审，弹窗里能看到它的名字。
        """
        reg = _Registry(["file_read"])
        called: list[str] = []

        async def cb(goal, action):
            called.append(action.tool_name)
            return {"approved": True, "arguments": None, "comment": ""}

        ex = self._ask_executor(reg, cb)
        asyncio.run(ex.execute(_plan("totally_made_up_tool"), on_approval_needed=cb))

        assert called == ["totally_made_up_tool"], \
            "未注册的工具绕过了审批门控（没有弹窗）"

    def test_unknown_tool_is_reported_as_missing_when_approved(self):
        """用户批准了也只能得到"工具不存在"的明确结论，而不是底层报错。"""
        reg = _Registry(["file_read"])

        async def cb(goal, action):
            return {"approved": True, "arguments": None, "comment": ""}

        ex = self._ask_executor(reg, cb)
        report = asyncio.run(ex.execute(_plan("made_up"), on_approval_needed=cb))
        assert not report.steps[0].success
        assert "不存在" in report.steps[0].error or "未注册" in report.steps[0].error

    def test_rejected_unknown_tool_is_not_dispatched(self):
        """拒绝后不许再去 dispatch —— 那等于"问过了但照做"。"""
        reg = _Registry(["file_read"])

        async def cb(goal, action):
            return {"approved": False, "comment": "别执行这个"}

        ex = self._ask_executor(reg, cb)
        report = asyncio.run(ex.execute(_plan("made_up"), on_approval_needed=cb))
        assert not report.steps[0].success
        assert not reg.calls, "未注册的工具在用户拒绝后仍被执行"

    def test_runner_unknown_tool_is_not_dispatched_and_others_survive(self):
        """同批里一个未注册工具不该连坐 —— 别的目标照常完成。"""
        reg = _Registry(["file_read"])

        async def cb(goal, action):
            # 只在"看起来不像已知工具"时拒绝，模拟用户对臆造动作说不
            return {"approved": action.tool_name == "file_read"}

        ex = self._ask_executor(reg, cb)
        report = asyncio.run(
            ex.execute(_plan("file_read", "made_up"), on_approval_needed=cb))
        by_tool = {s.goal_description.split(": ")[1]: s for s in report.steps}
        assert by_tool["file_read"].success
        assert not by_tool["made_up"].success
        assert reg.calls == ["file_read"]

    def test_unknown_tool_without_channel_is_denied(self):
        """没有审批通道时，未注册工具同样 fail-closed。"""
        reg = _Registry(["file_read"])
        ex = self._ask_executor(reg, None)
        report = asyncio.run(ex.execute(_plan("made_up")))
        assert not report.steps[0].success
        assert "审批" in report.steps[0].error

    def test_known_safe_tool_is_not_gated_in_ask_mode(self):
        """「询问」模式只问非只读操作 —— 只读的别拿来烦人。"""
        reg = _Registry(["file_read"])
        called: list[str] = []

        async def cb(goal, action):
            called.append(action.tool_name)
            return {"approved": True}

        ex = self._ask_executor(reg, cb)
        report = asyncio.run(ex.execute(_plan("file_read"), on_approval_needed=cb))
        assert report.completed_steps == 1
        assert not called, "只读工具在询问模式下仍然弹了审批"

    def test_sensitive_tool_is_gated_in_ask_mode(self):
        """非只读操作必须弹窗，且用户拒绝后**不执行**。"""
        reg = _Registry(["file_read"])
        reg._tools["terminal"] = _Tool("terminal", PermissionTier.SENSITIVE)
        called: list[str] = []

        async def cb(goal, action):
            called.append(action.tool_name)
            return {"approved": False, "comment": "我不同意"}

        ex = self._ask_executor(reg, cb)
        report = asyncio.run(ex.execute(_plan("terminal"), on_approval_needed=cb))
        assert called == ["terminal"], "敏感操作没有弹审批"
        assert not report.steps[0].success
        assert not reg.calls, "用户拒绝了，动作却仍然执行了"

    def test_modified_approval_replaces_parameters(self):
        """「修改后批准」必须真的用改过的参数去执行。"""
        reg = _Registry(["terminal"])
        reg._tools["terminal"] = _Tool("terminal", PermissionTier.SENSITIVE)
        plan = _plan("terminal")

        async def cb(goal, action):
            return {"approved": True,
                    "arguments": {"path": "safe.txt"},
                    "comment": "改了路径"}

        ex = self._ask_executor(reg, cb)
        report = asyncio.run(ex.execute(plan, on_approval_needed=cb))
        assert report.completed_steps == 1
        assert reg.calls == ["terminal"]
        assert plan.root_goal.children[0].status == GoalStatus.COMPLETED

    def test_unknown_tool_is_treated_as_dangerous(self):
        """未注册的工具按**最高**风险等级送审，而不是"找不到就放行"。

        与 ReAct 路径（``react_executor._gate`` 的 ``tier=SENSITIVE`` 兜底）
        保持同一安全姿态：模型臆造出来的工具名是最需要人看一眼的形态。
        """

        class _Recorder:
            def __init__(self) -> None:
                self.seen: list[tuple] = []

            def check(self, name, tier, params=None):
                self.seen.append((name, tier))
                return PermissionDecision.ASK_USER, "需批准"

        rec = _Recorder()
        reg = _Registry(["file_read"])
        ex = PlanExecutor(None, reg,  # type: ignore[arg-type]
                          permission_engine=rec, auto_retry=False, goal_timeout=5)

        async def cb(goal, action):
            return {"approved": True, "arguments": None, "comment": ""}

        asyncio.run(ex.execute(_plan("phantom_tool"), on_approval_needed=cb))
        assert rec.seen, "未注册的工具连权限检查都没走到"
        assert rec.seen[0][1] == PermissionTier.DANGEROUS, \
            f"未注册工具按 {rec.seen[0][1]} 送审，应当是 DANGEROUS"
