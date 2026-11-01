"""自主闭环预算治理（v1.11.21）—— 修复轮独立预算 / 部分交付跳过 / 结论复用。

## 修的是什么

长任务收尾阶段此前是"第二个完整任务"的预算：

* 主任务已经撞墙（步数 / 预算 / 断连 / 无进展）交了部分交付，收尾仍然
  调 LLM 做审查 + 验收，未过再进修复轮 —— 拿同一份半成品反复烧钱；
* 修复轮复用 ``ex.max_iterations``（50 步，v1.11.7 还会自动续步一次），
  一次"小修"可以再跑 150 步 / 25 分钟 / 数 M token；
* 闭环刚让模型判过"完成没有"，收尾又跑一次 quality_assessor + reflexion。

本文件锁住四件事：部分交付/简单任务跳过闭环、修复轮只有 ``fix_max`` 步且
不能续开预算窗口、修复结论被质量评估复用、0 表示关闭自动修复。
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from automind.agent import AutoMindAgent
from automind.core.config import AgentConfig, ExecutionConfig
from automind.core.types import (
    InteractionMode,
    PermissionTier,
    ToolResult,
)
from automind.planning.plan_executor import StepResult


@pytest.fixture
def agent(tmp_path):
    """真实 Agent（不联网：没有 API Key 时 llm 只是 None，这里换成替身）。"""
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpts")
    a = AutoMindAgent(cfg)
    a._interaction = InteractionMode.WORK
    # 闭环各环节只检查 `is not None`；审查/验收方法一律用替身接管
    a.llm = object()
    return a


def _stub_review(calls: dict):
    async def _review(task, output):
        calls["review"] += 1
        return {"approved": True, "available": True, "issues": "", "error": ""}
    return _review


def _stub_verify(calls: dict, verdicts: list[dict]):
    idx = {"i": 0}

    async def _verify(task, output):
        calls["verify"] += 1
        i = min(idx["i"], len(verdicts) - 1)
        idx["i"] += 1
        return dict(verdicts[i])
    return _verify


def _stub_react(calls: dict, fix_kwargs: list[dict]):
    async def _react(task, context, **kwargs):
        calls["fix"] += 1
        fix_kwargs.append(kwargs)
        return "修复后的结果"
    return _react


# ═══════════════════════════════════════════════════════════
# 1. 配置默认值
# ═══════════════════════════════════════════════════════════


class TestConfigDefault:
    def test_fix_max_iterations_defaults_to_one(self):
        """修复轮默认 **1 步**（用户明确要求）—— 不是主任务的 50 步。"""
        assert ExecutionConfig().auto_verify_fix_max_iterations == 1


# ═══════════════════════════════════════════════════════════
# 2. 部分交付 / 简单任务跳过 LLM 闭环
# ═══════════════════════════════════════════════════════════


class TestSkipClosure:
    def test_partial_main_skips_llm_closure(self, agent):
        calls = {"review": 0, "verify": 0, "fix": 0}
        fix_kwargs: list[dict] = []
        agent._review_result = _stub_review(calls)
        agent._loop_verify = _stub_verify(
            calls, [{"done": False, "available": True, "reason": "永远不满意"}])
        agent._run_react = _stub_react(calls, fix_kwargs)

        out = asyncio.run(agent._autonomy_closure(
            "任务", "半成品结果", "",
            main_partial=True, main_stop_reason="no_progress"))

        assert calls == {"review": 0, "verify": 0, "fix": 0}, (
            "主任务部分交付时仍调用了 LLM 环节 —— 拿半成品反复烧钱")
        assert "部分交付" in out and "跳过" in out
        assert "可续跑" in out
        assert agent._verify_state["skipped"] == "partial_main"
        assert agent._verify_state["stop_reason"] == "no_progress"
        # 半成品结果本身必须保留
        assert "半成品结果" in out

    def test_simple_task_skips_closure(self, agent):
        calls = {"review": 0, "verify": 0, "fix": 0}
        fix_kwargs: list[dict] = []
        agent._review_result = _stub_review(calls)
        agent._loop_verify = _stub_verify(
            calls, [{"done": True, "available": True, "reason": ""}])
        agent._run_react = _stub_react(calls, fix_kwargs)

        out = asyncio.run(agent._autonomy_closure("任务", "结果", "", simple=True))

        assert calls == {"review": 0, "verify": 0, "fix": 0}
        assert "简单任务" in out and "跳过" in out
        assert agent._verify_state["skipped"] == "simple"

    def test_simple_task_detection_read_only_plan(self, agent):
        """Plans 里全是只读 SAFE 工具且成功 → 简单任务。"""
        from automind.core.types import ExecutionMode

        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        step = StepResult(goal_id="g1", goal_description="读文件", success=True,
                          tool_result=ToolResult(tool_name="file_read", success=True))
        assert agent._is_simple_task([step]) is True

    def test_write_step_is_not_simple(self, agent):
        from automind.core.types import ExecutionMode

        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        step = StepResult(goal_id="g1", goal_description="写文件", success=True,
                          tool_result=ToolResult(tool_name="file_write", success=True))
        assert agent._is_simple_task([step]) is False

    def test_empty_plan_is_not_simple(self, agent):
        """空步骤列表不得被真空真判成"简单任务"（Lead 审阅发现）。

        ``all([])`` 恒为 True —— 计划生成后一步都没执行（被驳回 / 规划失败 /
        预算到点）若被当成简单任务，会跳过闭环，还可能让 success 误判。
        """
        from automind.core.types import ExecutionMode

        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        assert agent._is_simple_task([]) is False, (
            "Plan 模式没有步骤时被误判成简单任务（vacuous truth）")

    def test_failed_step_is_not_simple(self, agent):
        from automind.core.types import ExecutionMode

        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        step = StepResult(goal_id="g1", goal_description="读文件", success=False)
        assert agent._is_simple_task([step]) is False

    def test_non_safe_tool_is_not_simple(self, agent):
        from automind.core.types import ExecutionMode
        from automind.tools.base import AbstractTool

        class _SensitiveProbe(AbstractTool):
            name = "probe_sensitive"
            description = "sensitive probe"
            parameters = {"type": "object", "properties": {}}
            permission_tier = PermissionTier.SENSITIVE

            async def execute(self, **kwargs):
                return ToolResult(tool_name=self.name, success=True)

        agent.tool_registry.register(_SensitiveProbe())
        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        step = StepResult(goal_id="g1", goal_description="探测", success=True,
                          tool_result=ToolResult(tool_name="probe_sensitive",
                                                 success=True))
        assert agent._is_simple_task([step]) is False

    def test_react_with_actions_is_not_simple(self, agent):
        from types import SimpleNamespace

        from automind.core.types import ExecutionMode

        agent._mode = ExecutionMode.REACT
        agent.react_executor = SimpleNamespace(actions=[])
        assert agent._is_simple_task([]) is True
        agent.react_executor = SimpleNamespace(actions=[("tc", "result")])
        assert agent._is_simple_task([]) is False


# ═══════════════════════════════════════════════════════════
# 3. 修复轮独立预算
# ═══════════════════════════════════════════════════════════


class TestFixRoundBudget:
    def test_fix_round_uses_one_iteration(self, agent):
        """修复轮只调一次 `_run_react`，且显式要求 max_iterations=1 步。"""
        agent.config.execution.auto_verify_max_rounds = 1
        agent.config.execution.auto_verify_fix_max_iterations = 1
        calls = {"review": 0, "verify": 0, "fix": 0}
        fix_kwargs: list[dict] = []
        agent._review_result = _stub_review(calls)
        agent._loop_verify = _stub_verify(
            calls, [{"done": False, "available": True,
                     "source": "stub", "reason": "缺少边界处理"}])
        agent._run_react = _stub_react(calls, fix_kwargs)

        out = asyncio.run(agent._autonomy_closure("任务", "初版结果", ""))

        assert calls["fix"] == 1, f"修复轮应只跑 1 次，实际 {calls['fix']} 次"
        assert fix_kwargs and fix_kwargs[0].get("max_iterations") == 1, (
            "修复轮没有拿到独立步数上限（会复用主任务的 50 步）")
        assert "已修复 1 轮" in out
        assert "修复后的结果" in out

    def test_zero_fix_max_disables_fix_round(self, agent):
        agent.config.execution.auto_verify_fix_max_iterations = 0
        calls = {"review": 0, "verify": 0, "fix": 0}
        fix_kwargs: list[dict] = []
        agent._review_result = _stub_review(calls)
        agent._loop_verify = _stub_verify(
            calls, [{"done": False, "available": True, "reason": "不满意"}])
        agent._run_react = _stub_react(calls, fix_kwargs)

        out = asyncio.run(agent._autonomy_closure("任务", "初版", ""))

        assert calls["fix"] == 0, "fix_max=0 时仍进入了修复轮"
        assert "自动修复已关闭" in out

    def test_fix_round_cannot_open_new_budget_window(self, agent):
        """修复轮期间 `_roll_budget_window` 必须拒绝续窗；结束后恢复可用。"""
        agent.config.execution.budget_auto_continue = 2
        agent.config.execution.max_task_seconds = 60.0
        compress_calls = {"n": 0}

        async def _compress(force: bool = False):
            compress_calls["n"] += 1
            return {"ok": True}

        agent._compress_context = _compress
        agent._task_deadline = time.monotonic() + 60.0

        agent._closure_no_new_window = True
        rolled = asyncio.run(agent._roll_budget_window())
        assert rolled is False, "修复轮开出了新的预算窗口"
        assert agent._budget_windows_used == 0
        assert compress_calls["n"] == 0, "被拒绝的续窗不应先压缩上下文（白花钱）"

        # 守卫恢复后应当能正常续窗（回归：别把续窗功能一起关死）
        agent._closure_no_new_window = False
        rolled2 = asyncio.run(agent._roll_budget_window())
        assert rolled2 is True
        assert agent._budget_windows_used == 1
        assert agent._deadline_shift == pytest.approx(60.0)

    def test_pre_call_checks_the_closure_guard(self):
        """接线检查：`_pre_call` 里的续窗条件真的读了守卫（源码级断言）。"""
        src = inspect.getsource(AutoMindAgent._attach_usage_sink)
        assert "_closure_no_new_window" in src, (
            "修复轮的禁续窗守卫没有接到 _pre_call 的续窗判断上")
        assert "_roll_budget_window" in src


# ═══════════════════════════════════════════════════════════
# 4. 复用验收结论（token 优化）
# ═══════════════════════════════════════════════════════════


class TestQualityReuse:
    def test_quality_reuses_closure_verdict(self, agent):
        agent._verify_state = {
            "review": {"approved": True, "available": True, "issues": ""},
            "verify": {"done": True, "available": True, "reason": ""},
        }
        q = agent._quality_from_closure()
        assert q is not None and q.overall_pass is True
        assert q.issues == []

    def test_rejected_verdict_is_not_pass(self, agent):
        agent._verify_state = {
            "review": None,
            "verify": {"done": False, "available": True, "reason": "缺少总结"},
        }
        q = agent._quality_from_closure()
        assert q is not None and q.overall_pass is False
        assert "缺少总结" in "；".join(q.issues)

    def test_unavailable_stage_is_not_pass(self, agent):
        agent._verify_state = {
            "review": {"approved": True, "available": True},
            "verify": {"done": True, "available": False, "reason": ""},
        }
        q = agent._quality_from_closure()
        assert q is not None and q.overall_pass is False

    def test_no_evidence_returns_none(self, agent):
        agent._verify_state = {}
        assert agent._quality_from_closure() is None
        agent._verify_state = {"skipped": "simple"}
        assert agent._quality_from_closure() is None
        agent._verify_state = {"review": None, "verify": None}
        assert agent._quality_from_closure() is None

    def test_closure_evidence_resets_between_tasks(self, agent):
        """`_verify_state` 必须按任务清空（否则上一轮的通过会被本轮复用）。"""
        src = inspect.getsource(AutoMindAgent._run_impl_bound)
        assert "self._verify_state = {}" in src, (
            "任务入口没有清空上一轮的闭环证据 —— 质量评估会复用旧结论")
