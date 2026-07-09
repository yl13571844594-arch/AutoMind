"""自主任务闭环测试 — 并行执行 / 子任务缓存 / TDD 内环 / 审查 / 验收 / 配置开关。"""

import asyncio
import time

from automind.core.config import AgentConfig, ExecutionConfig
from automind.core.types import (
    Action,
    Goal,
    HierarchicalPlan,
    PermissionTier,
    ToolResult,
)
from automind.planning.plan_executor import PlanExecutor
from automind.tools.base import AbstractTool, ToolRegistry

# ── 测试用假工具 ─────────────────────────────────────────

class _SlowTool(AbstractTool):
    """慢速 SAFE 工具 — 用于验证并行与缓存。"""

    name = "slow_probe"
    description = "slow read-only probe"
    parameters = {"type": "object", "properties": {"key": {"type": "string"}},
                  "required": ["key"]}
    permission_tier = PermissionTier.SAFE

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.exec_count = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def execute(self, **kwargs):
        self.exec_count += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(self.delay)
        self.concurrent -= 1
        return ToolResult(tool_name=self.name, success=True,
                          output={"key": kwargs.get("key")})


class _WriteTool(AbstractTool):
    """SENSITIVE 工具 — 验证写类调用绝不被缓存。"""

    name = "fake_write"
    description = "fake sensitive write"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"]}
    permission_tier = PermissionTier.SENSITIVE

    def __init__(self) -> None:
        self.exec_count = 0

    async def execute(self, **kwargs):
        self.exec_count += 1
        return ToolResult(tool_name=self.name, success=True, output={"ok": True})


def _make_plan(goals: list[Goal]) -> HierarchicalPlan:
    root = Goal(id="root", description="root", children=goals)
    return HierarchicalPlan(
        task_description="test", root_goal=root,
        execution_order=[g.id for g in goals],
    )


def _goal(gid: str, tool: str, params: dict) -> Goal:
    return Goal(id=gid, description=gid,
                assigned_action=Action(tool_name=tool, parameters=params))


# ── 配置开关默认值 ──────────────────────────────────────

class TestConfigDefaults:
    def test_all_autopilot_flags_default_on(self):
        ex = ExecutionConfig()
        assert ex.auto_review is True
        assert ex.auto_verify is True
        assert ex.auto_test is True
        assert ex.parallel_execution is True
        assert ex.subtask_cache is True
        assert ex.auto_verify_max_rounds >= 1

    def test_version_bumped(self):
        # 版本随迭代递增，只校验语义化格式且 >= 引入闭环的 0.3.0（不锁死具体号）
        import re

        import automind
        assert re.fullmatch(r"\d+\.\d+\.\d+", automind.__version__)
        assert tuple(int(x) for x in automind.__version__.split(".")) >= (0, 3, 0)


# ── 并行执行（§2.4）────────────────────────────────────

class TestParallelExecution:
    def test_independent_goals_run_concurrently(self):
        tool = _SlowTool(delay=0.15)
        reg = ToolRegistry()
        reg.register(tool)
        ex = PlanExecutor(llm=None, tool_registry=reg, parallel=True, use_cache=False)
        plan = _make_plan([
            _goal("g1", "slow_probe", {"key": "a"}),
            _goal("g2", "slow_probe", {"key": "b"}),
            _goal("g3", "slow_probe", {"key": "c"}),
        ])
        t0 = time.perf_counter()
        report = asyncio.run(ex.execute(plan))
        elapsed = time.perf_counter() - t0
        assert report.completed_steps == 3
        assert tool.max_concurrent >= 2          # 真正并发过
        assert elapsed < 0.15 * 3                 # 明显快于串行总和

    def test_serial_when_disabled(self):
        tool = _SlowTool(delay=0.05)
        reg = ToolRegistry()
        reg.register(tool)
        ex = PlanExecutor(llm=None, tool_registry=reg, parallel=False, use_cache=False)
        plan = _make_plan([
            _goal("g1", "slow_probe", {"key": "a"}),
            _goal("g2", "slow_probe", {"key": "b"}),
        ])
        report = asyncio.run(ex.execute(plan))
        assert report.completed_steps == 2
        assert tool.max_concurrent == 1           # 从未并发


# ── 子任务缓存 ──────────────────────────────────────────

class TestSubtaskCache:
    def test_safe_tool_same_params_cached(self):
        tool = _SlowTool(delay=0.01)
        reg = ToolRegistry()
        reg.register(tool)
        ex = PlanExecutor(llm=None, tool_registry=reg, parallel=False, use_cache=True)
        plan = _make_plan([
            _goal("g1", "slow_probe", {"key": "same"}),
            _goal("g2", "slow_probe", {"key": "same"}),   # 同参 → 命中缓存
            _goal("g3", "slow_probe", {"key": "other"}),  # 异参 → 真实执行
        ])
        report = asyncio.run(ex.execute(plan))
        assert report.completed_steps == 3
        assert tool.exec_count == 2
        assert ex.cache_hits == 1

    def test_sensitive_tool_never_cached(self):
        tool = _WriteTool()
        reg = ToolRegistry()
        reg.register(tool)
        ex = PlanExecutor(llm=None, tool_registry=reg, parallel=False, use_cache=True)
        plan = _make_plan([
            _goal("g1", "fake_write", {"path": "x"}),
            _goal("g2", "fake_write", {"path": "x"}),
        ])
        report = asyncio.run(ex.execute(plan))
        assert report.completed_steps == 2
        assert tool.exec_count == 2               # 写操作两次都真实执行
        assert ex.cache_hits == 0

    def test_cache_disabled_flag(self):
        tool = _SlowTool(delay=0.01)
        reg = ToolRegistry()
        reg.register(tool)
        ex = PlanExecutor(llm=None, tool_registry=reg, parallel=False, use_cache=False)
        plan = _make_plan([
            _goal("g1", "slow_probe", {"key": "k"}),
            _goal("g2", "slow_probe", {"key": "k"}),
        ])
        asyncio.run(ex.execute(plan))
        assert tool.exec_count == 2 and ex.cache_hits == 0


# ── TDD 内环（ReAct 自动语法验证）─────────────────────

class TestTddInnerLoop:
    def _executor(self, auto=True):
        from automind.planning.react_executor import ReActExecutor
        return ReActExecutor(llm=None, tool_registry=ToolRegistry(), auto_validate=auto)

    def test_syntax_ok_annotated(self, tmp_path):
        from automind.core.types import ToolCall
        f = tmp_path / "good.py"
        f.write_text("def ok():\n    return 1\n", encoding="utf-8")
        ex = self._executor()
        tc = ToolCall(id="1", name="file_write", arguments={"path": str(f)})
        r = ToolResult(tool_name="file_write", success=True, output={"path": str(f)})
        out = ex._auto_validate_result(tc, r)
        assert out.output["auto_validation"] == "syntax_check: OK"
        assert ex.validations[-1]["ok"] is True

    def test_syntax_error_flagged(self, tmp_path):
        from automind.core.types import ToolCall
        f = tmp_path / "bad.py"
        f.write_text("def broken(:\n  pass\n", encoding="utf-8")
        ex = self._executor()
        tc = ToolCall(id="1", name="file_edit", arguments={"path": str(f)})
        r = ToolResult(tool_name="file_edit", success=True, output={"path": str(f)})
        out = ex._auto_validate_result(tc, r)
        assert "FAILED" in out.output["auto_validation"]
        assert ex.validations[-1]["ok"] is False

    def test_non_py_and_disabled_skipped(self, tmp_path):
        from automind.core.types import ToolCall
        f = tmp_path / "x.txt"
        f.write_text("hello", encoding="utf-8")
        ex = self._executor()
        tc = ToolCall(id="1", name="file_write", arguments={"path": str(f)})
        r = ToolResult(tool_name="file_write", success=True, output={"path": str(f)})
        assert "auto_validation" not in (ex._auto_validate_result(tc, r).output or {})
        ex2 = self._executor(auto=False)
        f2 = tmp_path / "y.py"
        f2.write_text("bad(", encoding="utf-8")
        tc2 = ToolCall(id="2", name="file_write", arguments={"path": str(f2)})
        r2 = ToolResult(tool_name="file_write", success=True, output={"path": str(f2)})
        assert "auto_validation" not in (ex2._auto_validate_result(tc2, r2).output or {})


# ── 闭环流水线（审查 + 验收 + 修复轮）─────────────────

class _R:
    text = ""
    tool_calls = []


class TestAutonomyClosure:
    def _agent(self, tmp_path):
        from automind.agent import AutoMindAgent
        return AutoMindAgent(AgentConfig(project_root=str(tmp_path)))

    def test_closure_pass_first_round(self, tmp_path):
        from automind.core.types import InteractionMode
        agent = self._agent(tmp_path)
        agent._interaction = InteractionMode.WORK

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _R()
                text = str(messages)
                if "approved" in text:
                    r.text = '{"approved": true, "issues": ""}'
                else:
                    r.text = '{"done": true, "reason": ""}'
                return r
        agent.llm = LLM()
        out = asyncio.run(agent._autonomy_closure("任务", "结果", ""))
        assert "审查通过" in out and "验收通过" in out

    def test_closure_fix_round_then_pass(self, tmp_path):
        from automind.core.types import InteractionMode
        agent = self._agent(tmp_path)
        agent._interaction = InteractionMode.CODING  # 跳过审查（仅工作模式）
        agent.config.execution.auto_test = False     # 无测试目录，聚焦验收路径
        state = {"verify_calls": 0, "fix_calls": 0}

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _R()
                state["verify_calls"] += 1
                # 第一次验收不通过，之后通过
                if state["verify_calls"] == 1:
                    r.text = '{"done": false, "reason": "缺少边界处理"}'
                else:
                    r.text = '{"done": true, "reason": ""}'
                return r
        agent.llm = LLM()

        async def fake_react(task, context):
            state["fix_calls"] += 1
            assert "修复第 1 轮" in task and "缺少边界处理" in task
            return "修复后的结果"
        agent._run_react = fake_react

        out = asyncio.run(agent._autonomy_closure("任务", "初版结果", ""))
        assert state["fix_calls"] == 1            # 触发了一轮自动修复
        assert "验收通过" in out
        assert "修复后的结果" in out              # 输出被修复轮更新

    def test_closure_gives_up_after_max_rounds(self, tmp_path):
        from automind.core.types import InteractionMode
        agent = self._agent(tmp_path)
        agent._interaction = InteractionMode.CODING
        agent.config.execution.auto_test = False
        agent.config.execution.auto_verify_max_rounds = 1

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _R()
                r.text = '{"done": false, "reason": "永远不满意"}'
                return r
        agent.llm = LLM()

        async def fake_react(task, context):
            return "还是不行的结果"
        agent._run_react = fake_react

        out = asyncio.run(agent._autonomy_closure("任务", "初版", ""))
        assert "验收未过" in out                  # 有界，不会无限循环

    def test_review_reads_tools(self, tmp_path):
        """审阅者可调用只读工具核实（MCP 工具共享同一 registry）。"""
        from automind.core.types import InteractionMode, ToolCall
        agent = self._agent(tmp_path)
        agent._interaction = InteractionMode.WORK
        probe = _SlowTool(delay=0)
        agent.tool_registry.register(probe)
        state = {"calls": 0}

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _R()
                state["calls"] += 1
                if state["calls"] == 1:
                    assert tools is not None      # 只读工具已共享给审阅者
                    r.tool_calls = [ToolCall(id="1", name="slow_probe",
                                             arguments={"key": "check"})]
                    r.text = "核实中"
                else:
                    r.tool_calls = []
                    r.text = '{"approved": true, "issues": ""}'
                return r
        agent.llm = LLM()
        rv = asyncio.run(agent._review_result("任务", "结果"))
        assert rv["approved"] is True
        assert probe.exec_count == 1              # 审阅者真实调用了只读工具

    def test_code_generate_tool_registered(self, tmp_path):
        agent = self._agent(tmp_path)
        assert "code_generate" in agent.tool_registry
        # 经技能真实生成文件（无 LLM 走模板兜底）
        out_file = tmp_path / "gen.py"
        result = asyncio.run(agent.tool_registry.dispatch(
            "code_generate", specification="helper", output_file=str(out_file)))
        assert result.success and out_file.exists()


# ── Web API 开关 ────────────────────────────────────────

class TestAutopilotApi:
    def test_get_and_toggle(self, tmp_path):
        from fastapi.testclient import TestClient

        import automind.server as srv
        srv._store.config_file = tmp_path / "cfg.json"
        srv._AUTH_TOKEN = ""
        srv._agent = None
        c = TestClient(srv.app)

        flags = c.get("/api/config/autopilot").json()
        assert all(flags[f] is True for f in flags)

        r = c.post("/api/config/autopilot", json={"auto_review": False}).json()
        assert r["auto_review"] is False and r["auto_verify"] is True
        # 持久化生效
        assert c.get("/api/config/autopilot").json()["auto_review"] is False
        # 恢复
        c.post("/api/config/autopilot", json={"auto_review": True})
