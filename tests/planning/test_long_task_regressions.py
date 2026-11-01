"""两条长任务回归（v1.11.21，用户明确要求）—— 锁住"不会 150/50、287 次调用"。

## 背景（真实事故形态）

1. **计划完成 + 验收未过**：主任务按计划跑完，收尾验收判"未过"，于是进入修复轮；
   修复轮复用了 ``ex.max_iterations``（50 步，v1.11.7 还会自动续步一次），
   一个已经交付的计划被重新跑成 150 步 / 25 分钟 / 7.7M token，最后仍报"验收未过"。
2. **反复验证无新产物**：模型换着 offset/limit 反复 ``file_read`` 同一份内容
   （"再看一眼确认一下"），动作指纹不同 → 旧的无进展判据全部失效，一路烧到
   步数上限；收尾还可能再拿这份半成品去跑审查/修复。

两个用例都用 **scripted/fake LLM**，不依赖真实模型、不真实等待（<5 秒断言）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from automind.agent import AutoMindAgent
from automind.core.config import AgentConfig
from automind.core.types import (
    Action,
    ExecutionMode,
    Goal,
    GoalStatus,
    HierarchicalPlan,
    InteractionMode,
    LLMResponse,
    PermissionTier,
    PlanStatus,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from automind.planning.plan_executor import StepResult

# ═══════════════════════════════════════════════════════════
# 脚本化替身
# ═══════════════════════════════════════════════════════════


class _ScriptedLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.usage = TokenUsage()
        self.counts = {"review": 0, "verify": 0, "fix_react": 0}

    def reset(self) -> None:
        self.calls = 0
        self.usage = TokenUsage()

    async def generate(self, messages, tools=None, **kw):
        text = str(messages)
        self.calls += 1
        self.usage.calls += 1
        if "修复第" in text:                       # 修复轮的 ReAct
            self.counts["fix_react"] += 1
            return LLMResponse(text="修复完成。")
        if "approved" in text:                     # 多 Agent 审查
            self.counts["review"] += 1
            return LLMResponse(text='{"approved": true, "issues": ""}')
        if "只输出 JSON" in text:                   # Loop 验收
            self.counts["verify"] += 1
            if self.counts["verify"] == 1:
                return LLMResponse(text='{"done": false, "reason": "缺少总结"}')
            return LLMResponse(text='{"done": true, "reason": ""}')
        return LLMResponse(text="ok")


class _PlanAgent:
    """一个把计划生成/执行都替身化的真实 Agent（不联网、不落外部状态）。"""

    def __init__(self, project_root: str) -> None:
        cfg = AgentConfig(project_root=project_root)
        cfg.execution.checkpoint_dir = f"{project_root}/ckpts"
        cfg.execution.auto_test = True
        cfg.execution.auto_review = True
        cfg.execution.auto_verify = True
        cfg.execution.auto_verify_max_rounds = 2
        cfg.execution.auto_verify_fix_max_iterations = 1
        self.agent = AutoMindAgent(cfg)
        self.agent._interaction = InteractionMode.WORK
        self.agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        self.llm = _ScriptedLLM()
        self.agent.llm = self.llm
        self.plan, self.steps = self._plan_and_steps()

        def _parse(text):
            return SimpleNamespace(raw_text=text, intent="", entities={},
                                   constraints=[], attached_files=[], images=[])

        async def _retrieve(_task, k=5):
            return []

        async def _store(_user, _assistant):
            return None

        async def _plan_exec(_task, _ctx):
            return self.plan, self.steps

        async def _absorb(_task, _ctx, text):
            return text

        self.agent.input_parser = SimpleNamespace(parse=_parse)
        self.agent.memory = SimpleNamespace(
            retrieve_relevant=_retrieve, store_interaction=_store)
        self.agent._build_context = lambda *_args, **_kw: ""
        self.agent._run_plan_execute = _plan_exec
        self.agent._build_result_text = lambda *_args, **_kw: "已完成总结"
        self.agent._absorb_interjections_after_plan = _absorb

    @staticmethod
    def _plan_and_steps():
        plan = HierarchicalPlan(task_description="写一份总结",
                                root_goal=Goal(id="root", description="root"))
        goal = Goal(id="g1", description="写文件", status=GoalStatus.COMPLETED,
                    assigned_action=Action(tool_name="file_write", parameters={}))
        plan.root_goal.children = [goal]
        plan.execution_order = ["g1"]
        plan.status = PlanStatus.COMPLETED
        steps = [StepResult(goal_id="g1", goal_description="写文件", success=True,
                            tool_result=ToolResult(tool_name="file_write",
                                                   success=True, output="ok"))]
        return plan, steps


class _ReadTool:
    """只读工具替身（file_read 的目标识别只看 path，不看 offset/limit）。"""

    name = "file_read"
    description = "read a file"
    permission_tier = PermissionTier.SAFE
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}},
                               "required": ["path"]}}


class _ReadRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_names(self) -> list[str]:
        return ["file_read"]

    def list_all(self):
        return [_ReadTool()]

    def get(self, name):
        return _ReadTool()

    async def dispatch(self, name, **kw) -> ToolResult:
        self.calls.append((name, dict(kw)))
        return ToolResult(tool_name=name, success=True, output="文件内容" * 20)


class _RepeatedReadLLM:
    """每一轮都读**同一个文件**，但换 offset（绕开逐字重复判据）。"""

    def __init__(self, n: int = 40) -> None:
        self.calls = 0
        self.script = [
            LLMResponse(text=f"再确认第 {i + 1} 段",
                        tool_calls=[ToolCall(id=str(i), name="file_read",
                                             arguments={"path": "big.py",
                                                        "offset": i * 100,
                                                        "limit": 100})])
            for i in range(n)
        ]

    async def generate(self, messages, tools=None, **kw):
        self.calls += 1
        return self.script.pop(0) if self.script else LLMResponse(text="完成")


# ═══════════════════════════════════════════════════════════
# ① 计划完成 + 验收未过：修复轮最多 1 次 / 每次 1 步
# ═══════════════════════════════════════════════════════════


class TestPlanCompleteVerifyFailed:
    #: 收尾阶段允许的 LLM 调用上限（审查 1 + 验收 2 + 修复轮 1）
    MAX_LLM_CALLS = 8
    #: 允许的执行步数上限（主任务 1 步 + 修复轮 1 步）
    MAX_STEPS = 2

    def test_fix_round_is_bounded_and_quality_is_reused(self, tmp_path):
        env = _PlanAgent(str(tmp_path))
        agent, llm = env.agent, env.llm

        t0 = time.perf_counter()
        result = asyncio.run(agent.run("把刚才的结论收成一份总结"))
        elapsed = time.perf_counter() - t0

        # ── 交付本身 ──
        assert result.success is True
        assert "修复完成" in result.output          # 修复轮输出成为最终交付
        assert elapsed < 5, f"脚本化场景不该耗时 {elapsed:.1f}s"

        # ── 修复轮：最多 1 次，每次 1 步，且不续步 ──
        assert llm.counts["fix_react"] == 1, "修复轮跑了不止 1 次"
        ex = agent.react_executor
        assert ex is not None
        assert ex.max_iterations == 1, "修复轮没有用独立步数上限（会复用 50 步）"
        assert ex.iteration_extensions_used == 0, "修复轮自动续步了（1 步变 2 步）"
        assert ex.iterations_used <= 1, f"修复轮跑了 {ex.iterations_used} 步"

        # ── 调用 / 步数 / 时间三条上限 ──
        assert result.llm_calls == llm.calls
        assert result.llm_calls <= self.MAX_LLM_CALLS, (
            f"收尾阶段 LLM 调用 {result.llm_calls} 次，超出上限")
        assert llm.counts["verify"] <= 2
        assert result.steps_executed == 1          # 计划只有 1 步
        assert result.steps_executed + ex.iterations_used <= self.MAX_STEPS

        # ── 结构化状态字段 ──
        assert result.stop_reason == "completed"
        assert result.error == ""
        assert result.budget_exhausted is False
        assert result.resumable is False

        # ── 复用验收结论：不再重复跑 quality_assessor / reflexion ──
        assert agent.quality_assessor.history == [], (
            "闭环已有验收结论，收尾又调了一次质量评估（重复计费）")
        assert agent.reflexion.reflections == [], "干净成功/复用结论时不该再反思"
        assert agent._verify_state["verify"]["done"] is True

    def test_partial_plan_with_no_steps_is_not_simple(self, tmp_path):
        """回归 Lead 审阅：空步骤列表（计划未执行）不得被判成简单任务。

        详细单测在 ``test_autonomy_budget.py``；这里从长任务视角再兜一次：
        收尾阶段若把"一步没跑"当成简单任务，会跳过闭环并可能误判 success。
        """
        agent = AutoMindAgent(AgentConfig(project_root=str(tmp_path)))
        agent._interaction = InteractionMode.WORK
        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        assert agent._is_simple_task([]) is False


# ═══════════════════════════════════════════════════════════
# ② 反复验证无新产物：读目标守卫触发 + 有界收尾
# ═══════════════════════════════════════════════════════════


class TestRepeatedVerificationNoNewArtifacts:
    MAX_LLM_CALLS = 12
    MAX_ITERATIONS = 12

    def _agent(self, tmp_path):
        cfg = AgentConfig(project_root=str(tmp_path))
        cfg.execution.checkpoint_dir = f"{tmp_path}/ckpts"
        cfg.execution.max_iterations = self.MAX_ITERATIONS
        cfg.execution.react_repeat_action_threshold = 99   # 只测读目标守卫
        cfg.execution.react_no_progress_limit = 6
        agent = AutoMindAgent(cfg)
        agent._interaction = InteractionMode.CODING
        agent._mode = ExecutionMode.REACT
        llm = _RepeatedReadLLM()
        registry = _ReadRegistry()
        agent.llm = llm
        agent.tool_registry = registry
        return agent, llm, registry

    def test_read_target_guard_stops_the_loop(self, tmp_path):
        agent, llm, registry = self._agent(tmp_path)

        t0 = time.perf_counter()
        text = asyncio.run(agent._run_react("反复确认这个文件", ""))
        elapsed = time.perf_counter() - t0
        ex = agent.react_executor

        # ── 读目标守卫确实触发（"同一目标第 N 次"被拦）──
        assert ex.progress.read_target_hits >= 1, "读目标守卫没有触发"
        assert ex.progress.read_target_blocked >= 1
        assert len(registry.calls) < llm.calls, "被拦截的读取仍在真实执行"

        # ── 有界：调用 / 迭代 / 时间都有明确上限 ──
        assert llm.calls <= self.MAX_LLM_CALLS, f"LLM 调用失控：{llm.calls} 次"
        assert ex.iterations_used <= self.MAX_ITERATIONS
        assert ex.stop_reason in ("no_progress", "max_iterations"), ex.stop_reason
        assert elapsed < 5, f"脚本化场景不该耗时 {elapsed:.1f}s"

        # ── 有部分交付，而不是空手/无限循环 ──
        assert "部分交付清单" in text, "提前收尾时必须交部分交付清单"
        assert "file_read" in text

    def test_partial_delivery_skips_llm_closure(self, tmp_path):
        """同一个场景的收尾：部分交付 → 跳过 LLM 审查/验收修复轮。"""
        agent, llm, _ = self._agent(tmp_path)
        text = asyncio.run(agent._run_react("反复确认这个文件", ""))
        before_calls = llm.calls

        out = asyncio.run(agent._autonomy_closure(
            "反复确认这个文件", text, "",
            main_partial=True,
            main_stop_reason=str(agent.react_executor.stop_reason)))

        assert llm.calls == before_calls, "部分交付后仍调用了 LLM 闭环"
        assert "主任务已部分交付" in out
        assert "跳过" in out
        assert agent._verify_state["skipped"] == "partial_main"


# ═══════════════════════════════════════════════════════════
# ③ 显式 max_iterations = 硬上限（修复轮 1 步的底层保证）
# ═══════════════════════════════════════════════════════════


class TestExplicitMaxIterationsIsHardCap:
    def test_one_iteration_cannot_be_extended(self, tmp_path):
        cfg = AgentConfig(project_root=str(tmp_path))
        cfg.execution.checkpoint_dir = f"{tmp_path}/ckpts"
        cfg.execution.max_iterations = 50
        agent = AutoMindAgent(cfg)
        agent._interaction = InteractionMode.CODING
        agent._mode = ExecutionMode.REACT
        agent.tool_registry = _ReadRegistry()

        class _ToolCallOnce:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, tools=None, **kw):
                self.calls += 1
                return LLMResponse(
                    text="读一下",
                    tool_calls=[ToolCall(id="1", name="file_read",
                                         arguments={"path": "a.py"})])

        llm = _ToolCallOnce()
        agent.llm = llm
        asyncio.run(agent._run_react("看一眼", "", max_iterations=1))
        ex = agent.react_executor

        assert llm.calls == 1, f"显式上限 1 步却调用了 {llm.calls} 次模型"
        assert ex.max_iterations == 1
        assert ex.max_iteration_extensions == 0, "修复轮的步数上限被自动续加了"
        assert ex.iterations_used == 1
        assert len(ex.actions) == 1, "1 步内不该出现多个动作"
