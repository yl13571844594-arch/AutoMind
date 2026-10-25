"""执行治理参数真的通电（v1.11.16）。

## 修的是什么

「设置 → 执行治理」里的 26 个参数（5 个自主闭环开关 + 4 个布尔附加项 +
15 个数值 + 2 个策略）此前有一部分是"保存了但不生效"或"只对新实例生效"：

  · ``trace_enabled`` / ``trace_max_runs``：轨迹记录器懒构造 + 读的是
    ``ExecutionConfig()`` 出厂默认值，设置里改了永远到不了记录器；
  · ``tool_timeout_seconds`` / ``tool_timeout_max_seconds`` /
    ``terminal_background_enabled``：终端工具在 Agent 构造时就把超时抄成
    实例属性，之后不再更新；
  · ``write_conflict_policy``：写冲突守卫每次新造一份 ``ExecutionConfig()``
    读策略，改成 block 仍然按 warn 放行；
  · ``approval_timeout_seconds`` / ``approval_timeout_action``：任务开始时
    读一次，任务中途改设置对后续审批不生效且无提示；
  · ``task_token_budget``：预算在 Agent/克隆构造时写进 ResourceManager，
    设置里改完旧实例一直用旧预算；
  · 其余按任务读取的参数（max_iterations / react_* / tool_output_max_chars /
    max_task_seconds / isolate_workspace / auto_* …）本身是"下次任务生效"，
    但此前界面没有任何提示，用户以为改完立刻生效。

本文件守住三件事：
  1. **26 个参数全部**通过 API 可改、可持久化、同步到已存在会话克隆，
     并在响应里带 ``effective``（生效时机 + 中文说明）；
  2. 关键参数有**行为级**断言（改完 → 下一次调用/写入/审批/任务真的变了）；
  3. 诚实边界：标注"下次任务生效"的参数对进行中的任务不追溯；
     非法值仍然 ``rejected``；重启后配置恢复。

全部用桩 Agent / 临时目录，不构造真实 LLM、不联网、不污染仓库 .automind。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from automind.core import session_ctx
from automind.core import trace as trace_mod
from automind.core.config import ExecutionConfig
from automind.core.types import (
    Action,
    Goal,
    HierarchicalPlan,
    InteractionMode,
    PermissionTier,
    ToolResult,
)
from automind.planning.plan_executor import PlanExecutor
from automind.tools import file_editor, write_guard
from automind.tools.base import AbstractTool, ToolRegistry
from automind.tools.terminal import TerminalTool

ALL_FLAGS = ("auto_review", "auto_verify", "auto_test",
             "parallel_execution", "subtask_cache")
ALL_BOOL_EXTRAS = ("isolate_workspace", "terminal_background_enabled",
                   "release_slot_on_approval_wait", "trace_enabled")
ALL_NUMS = ("tool_timeout_seconds", "tool_timeout_max_seconds",
            "approval_timeout_seconds", "tool_output_max_chars",
            "auto_verify_max_rounds", "trace_max_runs",
            "goal_timeout_seconds", "react_tool_timeout_seconds",
            "max_task_seconds", "react_tool_budget",
            "react_repeat_action_threshold", "react_no_progress_limit",
            "task_token_budget", "max_iterations", "budget_auto_continue")
ALL_STRS = ("approval_timeout_action", "write_conflict_policy")
ALL_KEYS = ALL_FLAGS + ALL_BOOL_EXTRAS + ALL_NUMS + ALL_STRS
assert len(ALL_KEYS) == 26, "执行治理参数清单必须是 26 个（与设置面板一致）"

#: 每个参数一个"改了之后能观察到"的代表值（参数字典见 server._AUTOPILOT_NUMS）
SAMPLE_VALUES: dict[str, object] = {
    "auto_review": False,
    "auto_verify": False,
    "auto_test": False,
    "parallel_execution": False,
    "subtask_cache": False,
    "isolate_workspace": True,
    "terminal_background_enabled": False,
    "release_slot_on_approval_wait": False,
    "trace_enabled": False,
    "tool_timeout_seconds": 42,
    "tool_timeout_max_seconds": 91,
    "approval_timeout_seconds": 0.25,
    "tool_output_max_chars": 500,
    "auto_verify_max_rounds": 1,
    "trace_max_runs": 2,
    "goal_timeout_seconds": 30,
    "react_tool_timeout_seconds": 45,
    "max_task_seconds": 600,
    "react_tool_budget": 3,
    "react_repeat_action_threshold": 4,
    "react_no_progress_limit": 5,
    "task_token_budget": 123456,
    "max_iterations": 7,
    "budget_auto_continue": 2,
    "approval_timeout_action": "approve",
    "write_conflict_policy": "block",
}


# ── 桩对象：只提供 API/消费端真正用到的属性（不构造真 Agent）──────────


class _StubPlanExecutor:
    def __init__(self) -> None:
        self.parallel = True
        self.use_cache = True
        self.goal_timeout = 900.0


class _StubTokens:
    def __init__(self) -> None:
        self.budget = 500_000


class _StubResources:
    def __init__(self) -> None:
        self.tokens = _StubTokens()


class _StubAgent:
    """足够让 /api/config/autopilot 与运行时下发工作的最小 Agent。"""

    def __init__(self, execution: ExecutionConfig | None = None) -> None:
        self.config = SimpleNamespace(execution=execution or ExecutionConfig(),
                                      llm=SimpleNamespace(max_tokens=8192))
        self.plan_executor = _StubPlanExecutor()
        self.resources = _StubResources()


@pytest.fixture
def gov(tmp_path, monkeypatch):
    """隔离 server 配置/轨迹/写冲突状态，并把 _agent 换成桩 Agent。"""
    import automind.server as srv

    srv._store.config_file = tmp_path / "cfg.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._store.chats_dir = tmp_path / "chats"
    srv._store.session_histories.clear()
    srv._AUTH_TOKEN = ""
    srv._session_clones.clear()
    stub = _StubAgent()
    srv._agent = stub
    trace_mod.set_config_provider(None)
    trace_mod.reset_for_tests(root=tmp_path / "traces", enabled=True)
    write_guard.reset_for_tests()
    yield SimpleNamespace(srv=srv, agent=stub, tmp=tmp_path)
    srv._session_clones.clear()
    srv._agent = None
    trace_mod.set_config_provider(None)
    trace_mod.reset_for_tests(root=tmp_path / "traces_teardown", enabled=True)
    write_guard.reset_for_tests()


def _post(gov, payload: dict) -> dict:
    return asyncio.run(gov.srv.api_set_autopilot(payload))


def _get(gov) -> dict:
    return asyncio.run(gov.srv.api_get_autopilot())


# ═══════════════════════════════════════════════════════════
# 1. 26 个参数：可改 / 可持久化 / 同步克隆 / 带生效时机
# ═══════════════════════════════════════════════════════════


class TestGovernanceMatrix:
    def test_all_26_params_have_effective_timing(self, gov):
        snap = _get(gov)
        for key in ALL_KEYS:
            assert key in snap, f"设置面板读不到参数 {key}"
            info = snap["effective"].get(key)
            assert info, f"参数 {key} 缺少生效时机说明"
            assert info["label"] and info["note"]
            assert info["timing"] in (
                "immediate", "next_task", "next_approval", "next_call",
                "next_write", "next_event",
            ), f"{key} 的生效时机非法：{info['timing']}"
        assert set(snap["effective"]) == set(ALL_KEYS)
        # 全部参数都不应该要求重启（本次修复的目标：要么即时，要么下一任务/下一步）
        assert "restart" not in {v["timing"] for v in snap["effective"].values()}

    @pytest.mark.parametrize("key", ALL_KEYS)
    def test_param_applies_and_syncs_to_existing_clone(self, gov, key):
        value = SAMPLE_VALUES[key]
        clone = _StubAgent()
        gov.srv._session_clones["s_live"] = clone
        r = _post(gov, {key: value})
        assert key in r["applied"], f"{key} 没有被应用：{r.get('rejected')}"
        assert r.get("rejected", {}) == {}
        assert r["effective"][key]["label"]
        assert r["sessions_synced"] >= 1, "已存在的会话克隆没有同步"
        expected = r["applied"][key]
        # 全局 agent / 已存在克隆 / GET 快照 / 持久化配置 四处一致
        assert getattr(gov.agent.config.execution, key) == expected
        assert getattr(clone.config.execution, key) == expected
        assert _get(gov)[key] == expected
        stored = gov.srv._read_config()["autopilot"][key]
        assert stored == expected, f"{key} 没有写进配置文件（重启后会丢）"

    def test_invalid_values_are_rejected_not_silently_saved(self, gov):
        r = _post(gov, {
            "tool_timeout_seconds": 0,        # 0 会让命令立刻超时，不是"不限"
            "tool_timeout_max_seconds": -1,
            "react_no_progress_limit": -3,
            "max_iterations": 0,
            "write_conflict_policy": "yolo",
            "approval_timeout_action": "maybe",
            "task_token_budget": "abc",
        })
        assert set(r["rejected"]) == {
            "tool_timeout_seconds", "tool_timeout_max_seconds",
            "react_no_progress_limit", "max_iterations",
            "write_conflict_policy", "approval_timeout_action",
            "task_token_budget",
        }
        assert r["applied"] == {}
        stored = gov.srv._read_config().get("autopilot", {})
        for key in r["rejected"]:
            assert key not in stored, f"非法值 {key} 被持久化了"
        # 被拒绝的参数保持默认值
        assert gov.agent.config.execution.max_iterations == 50

    def test_restart_restores_persisted_values(self, gov):
        _post(gov, {"max_iterations": 9, "trace_enabled": False,
                    "write_conflict_policy": "block",
                    "approval_timeout_seconds": 12.5})
        fresh = ExecutionConfig()
        restored = gov.srv._restore_autopilot_into_execution(fresh)
        assert restored["max_iterations"] == 9
        assert restored["trace_enabled"] is False
        assert restored["write_conflict_policy"] == "block"
        assert restored["approval_timeout_seconds"] == 12.5
        assert fresh.max_iterations == 9
        assert fresh.trace_enabled is False

    def test_restart_ignores_legacy_invalid_values(self, gov):
        cfg = gov.srv._read_config()
        cfg["autopilot"] = {"max_iterations": "not-a-number",
                            "write_conflict_policy": "yolo",
                            "approval_timeout_action": "maybe"}
        gov.srv._write_config(cfg)
        fresh = ExecutionConfig()
        restored = gov.srv._restore_autopilot_into_execution(fresh)
        assert restored == {}
        assert fresh.max_iterations == 50
        assert fresh.write_conflict_policy == "warn"

    def test_sync_execution_config_refreshes_stale_clone(self, gov):
        stale = _StubAgent()
        stale.config.execution.max_iterations = 3
        stale.config.execution.parallel_execution = True
        gov.agent.config.execution.max_iterations = 11
        gov.agent.config.execution.parallel_execution = False
        gov.srv._sync_execution_config(stale, gov.agent)
        assert stale.config.execution.max_iterations == 11
        assert stale.config.execution.parallel_execution is False
        assert stale.plan_executor.parallel is False


# ═══════════════════════════════════════════════════════════
# 2. 轨迹：开关与保留数即时生效（记录器是进程级单例）
# ═══════════════════════════════════════════════════════════


class TestTraceGovernance:
    def test_trace_enabled_and_max_runs_apply_to_live_recorder(self, gov):
        rec = trace_mod.get_recorder()
        _post(gov, {"trace_enabled": False})
        assert rec.enabled is False
        assert rec.record("s1", "run-disabled", {"type": "task_start"}) == ""
        assert not rec.path_for("s1", "run-disabled").exists()

        _post(gov, {"trace_enabled": True, "trace_max_runs": 2})
        assert rec.enabled is True and rec.max_runs == 2
        path = rec.record("s1", "run-enabled", {"type": "task_start"})
        assert path and rec.path_for("s1", "run-enabled").exists()

    def test_trace_max_runs_prunes_on_next_rotation(self, gov):
        rec = trace_mod.get_recorder()
        _post(gov, {"trace_max_runs": 2})
        for i in range(4):
            rec.record("prune-session", f"run{i}", {"type": "task_start"})
            rec.finish("prune-session", f"run{i}", {"i": i})
            time.sleep(0.005)          # 文件 mtime 排序需要可区分
        assert len(rec.list_runs("prune-session")) == 2


# ═══════════════════════════════════════════════════════════
# 3. 工具类参数：下一次调用即生效（工具实例是共享的，不能只在构造时读）
# ═══════════════════════════════════════════════════════════


class TestToolGovernance:
    def test_terminal_reads_live_timeouts_and_background_switch(self, gov):
        tool = TerminalTool(workdir=gov.tmp, timeout=300.0, max_timeout=1800.0)
        with session_ctx.bind_agent(gov.agent):
            _post(gov, {"tool_timeout_seconds": 42,
                        "tool_timeout_max_seconds": 90,
                        "terminal_background_enabled": False})
            result = asyncio.run(tool.execute(command="echo hi", background=True))
        assert result.success is False
        assert "后台通道已在配置中关闭" in (result.error or "")
        assert tool.timeout == 42
        assert tool.max_timeout >= 90
        assert tool.background_enabled is False
        assert tool._effective_timeout(None)[0] == 42

    def test_write_conflict_policy_block_blocks_next_write(self, gov, tmp_path):
        target = tmp_path / "shared.txt"
        target.write_text("v1", encoding="utf-8")
        with session_ctx.bind_agent(gov.agent):
            _post(gov, {"write_conflict_policy": "block"})
            # 另一个会话先写了它
            with session_ctx.bind_session("other"):
                writer = file_editor._write_guard()
                writer.note_write(str(target), tool="file_write")
            with session_ctx.bind_session("mine"):
                guard = file_editor._write_guard()
                decision = guard.check(str(target))
            assert guard.policy == "block"
            assert decision["ok"] is False, "block 策略下冲突写入必须被拒绝"
            assert "另一个会话" in decision["reason"]

            _post(gov, {"write_conflict_policy": "warn"})
            with session_ctx.bind_session("mine"):
                guard_warn = file_editor._write_guard()
                decision_warn = guard_warn.check(str(target))
            assert guard_warn.policy == "warn"
            assert decision_warn["ok"] is True, "warn 策略只提示、不拒绝写入"


# ═══════════════════════════════════════════════════════════
# 4. 审批参数：下一次审批即生效（含超时处置）
# ═══════════════════════════════════════════════════════════


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class TestApprovalGovernance:
    def test_timeout_and_action_are_read_per_approval(self, gov, monkeypatch):
        async def _no_broadcast(*_a, **_k):
            return 0

        monkeypatch.setattr(gov.srv, "_broadcast_to_session", _no_broadcast)
        monkeypatch.setattr(gov.srv, "_record_and_notify", lambda *_a, **_k: None)
        ws = _FakeWS()
        cb = gov.srv.make_approval_callback(ws, "client", "sess", "chan", gov.agent)

        _post(gov, {"approval_timeout_seconds": 0.05,
                    "approval_timeout_action": "reject"})
        started = time.monotonic()
        first = asyncio.run(cb("terminal", {"command": "x"}, "sensitive", "测试"))
        elapsed = time.monotonic() - started
        assert first["approved"] is False
        assert 0.02 <= elapsed < 2.0
        payload = next(p for p in ws.sent if p["type"] == "approval_request")
        assert payload["timeout_s"] == 0.05
        assert payload["on_timeout"] == "reject"

        # 同一个回调、任务中途改设置：**下一次审批**必须按新值（旧实现读一次就固定）
        _post(gov, {"approval_timeout_seconds": 0.1,
                    "approval_timeout_action": "approve"})
        started = time.monotonic()
        second = asyncio.run(cb("terminal", {"command": "x"}, "sensitive", "测试"))
        elapsed = time.monotonic() - started
        assert second["approved"] is True, "approval_timeout_action=approve 必须生效"
        assert elapsed >= 0.08
        payload2 = [p for p in ws.sent if p["type"] == "approval_request"][-1]
        assert payload2["timeout_s"] == 0.1
        assert payload2["on_timeout"] == "approve"

    def test_release_slot_flag_is_read_live(self, gov):
        _post(gov, {"release_slot_on_approval_wait": False})
        assert gov.srv._release_slot_on_approval_wait(gov.agent) is False
        _post(gov, {"release_slot_on_approval_wait": True})
        assert gov.srv._release_slot_on_approval_wait(gov.agent) is True


# ═══════════════════════════════════════════════════════════
# 5. 计划执行器：并行 / 子任务缓存 / 单步超时（即时生效）
# ═══════════════════════════════════════════════════════════


class _ProbeTool(AbstractTool):
    name = "gov_probe"
    description = "governance probe"
    parameters = {"type": "object", "properties": {"key": {"type": "string"}},
                  "required": ["key"]}
    permission_tier = PermissionTier.SAFE

    def __init__(self, delay: float = 0.05) -> None:
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


def _plan(*keys: str) -> HierarchicalPlan:
    goals = [Goal(id=f"g{i}", description=f"g{i}",
                  assigned_action=Action(tool_name="gov_probe",
                                         parameters={"key": key}))
             for i, key in enumerate(keys)]
    root = Goal(id="root", description="root", children=goals)
    return HierarchicalPlan(task_description="gov", root_goal=root,
                            execution_order=[goal.id for goal in goals])


class TestPlanExecutorGovernance:
    def test_parallel_execution_switch_changes_scheduling(self, gov):
        tool = _ProbeTool(delay=0.06)
        registry = ToolRegistry()
        registry.register(tool)

        _post(gov, {"parallel_execution": False})
        serial = PlanExecutor(llm=None, tool_registry=registry, parallel=False)
        asyncio.run(serial.execute(_plan("a", "b")))
        assert tool.max_concurrent == 1, "parallel_execution=False 必须串行"

        tool.max_concurrent = 0
        _post(gov, {"parallel_execution": True})
        parallel = PlanExecutor(llm=None, tool_registry=registry, parallel=True)
        asyncio.run(parallel.execute(_plan("a", "b")))
        assert tool.max_concurrent >= 2, "parallel_execution=True 必须并行"

    def test_subtask_cache_switch_reuses_read_only_results(self, gov):
        registry = ToolRegistry()
        tool = _ProbeTool(delay=0.0)
        registry.register(tool)
        goal = Goal(id="g_cached", description="g",
                    assigned_action=Action(tool_name="gov_probe",
                                           parameters={"key": "same"}))

        async def _run_twice(executor: PlanExecutor) -> None:
            await executor._execute_goal(goal)
            await executor._execute_goal(goal)

        _post(gov, {"subtask_cache": True})
        cached = PlanExecutor(llm=None, tool_registry=registry,
                              parallel=False, use_cache=True)
        asyncio.run(_run_twice(cached))
        assert tool.exec_count == 1, "开启缓存后同一步骤的重复只读调用应复用"
        assert cached.cache_hits == 1

        tool.exec_count = 0
        _post(gov, {"subtask_cache": False})
        uncached = PlanExecutor(llm=None, tool_registry=registry,
                                parallel=False, use_cache=False)
        asyncio.run(_run_twice(uncached))
        assert tool.exec_count == 2, "关闭缓存后同参调用必须各自执行"

    def test_goal_timeout_switch_bounds_hung_step(self, gov):
        registry = ToolRegistry()
        tool = _ProbeTool(delay=0.3)
        registry.register(tool)
        _post(gov, {"goal_timeout_seconds": 0.05})
        executor = PlanExecutor(llm=None, tool_registry=registry, parallel=False,
                                goal_timeout=0.05)
        started = time.monotonic()
        report = asyncio.run(executor.execute(_plan("slow")))
        elapsed = time.monotonic() - started
        assert executor.timed_out >= 1, "超过单步上限必须记为超时"
        assert not report.steps[0].success
        assert elapsed < 0.3, "不能真的等满慢工具的 0.3 秒"


# ═══════════════════════════════════════════════════════════
# 6. ReAct 参数：下一次任务按新配置构造执行器
# ═══════════════════════════════════════════════════════════


def _bare_agent(config) -> object:
    """object.__new__ 造一个只带消费端所需属性的 Agent（不跑重初始化）。"""
    import automind.agent as agent_mod

    agent = object.__new__(agent_mod.AutoMindAgent)
    agent.config = SimpleNamespace(execution=config.execution,
                                   project_root=str(getattr(config, "project_root", ".")))
    agent.llm = SimpleNamespace()
    agent.tool_registry = ToolRegistry()
    agent.permissions = None
    agent.approval_callback = None
    agent._interaction = InteractionMode.CHAT
    agent.prefer_tools = []
    agent._task_deadline = None
    agent.drain_interjections = lambda: None
    agent._react_callbacks = lambda: (None, None)
    agent.event_sink = None
    agent._workspace_plan = None

    async def _emit(_event):
        return None

    agent._emit = _emit
    return agent


class TestReactGovernance:
    def test_run_react_consumes_updated_knobs(self, gov, monkeypatch):
        import automind.agent as agent_mod

        captured: dict = {}

        class _SpyExecutor:
            def __init__(self, *_args, **kwargs) -> None:
                captured.update(kwargs)

            async def run(self, *_args, **_kwargs):
                return "ok"

            def token_report(self):
                return {}

        monkeypatch.setattr(agent_mod, "ReActExecutor", _SpyExecutor)
        clone = _StubAgent()
        gov.srv._session_clones["s_react"] = clone
        _post(gov, {
            "max_iterations": 7,
            "react_tool_budget": 3,
            "react_tool_timeout_seconds": 45,
            "tool_timeout_max_seconds": 91,
            "tool_output_max_chars": 500,
            "react_repeat_action_threshold": 4,
            "react_no_progress_limit": 5,
            "auto_test": False,
        })
        agent = _bare_agent(clone.config)
        asyncio.run(agent._run_react("任务", "上下文"))
        assert captured["max_iterations"] == 7
        assert captured["tool_budget"] == 3
        assert captured["tool_timeout"] == 45
        assert captured["tool_timeout_max"] == 91
        assert captured["repeat_threshold"] == 4
        assert captured["no_progress_limit"] == 5
        assert captured["auto_validate"] is False
        assert captured["output_limits"]["max_chars"] == 500

    def test_max_task_seconds_builds_task_deadline(self, gov):
        # v1.11.16 起"每个任务一份预算"统一走 _begin_task_budget（multi/loop/
        # 续跑也复用它）—— 这里直接验证它读**实时**配置算墙钟截止。
        agent = _bare_agent(gov.agent.config)
        agent.resources = None
        agent.llm = None
        agent._reset_usage_total = lambda: None
        agent._reset_budget_windows = lambda: None
        agent._BUDGET_WARN_AT = 0.8
        agent._budget_warned = False
        agent._next_compact_at = 0.8
        agent._plan_rejected = ""
        agent._task_deadline = None
        _post(gov, {"max_task_seconds": 2})
        agent._begin_task_budget(InteractionMode.CHAT)
        assert agent._task_deadline is not None
        remaining = agent._task_deadline - time.monotonic()
        assert 1.0 < remaining <= 2.0, "整轮墙钟预算应按新配置计算"

        _post(gov, {"max_task_seconds": 0})
        agent._task_deadline = 123.0
        agent._begin_task_budget(InteractionMode.CHAT)
        assert agent._task_deadline is None, "0 = 不限（旧行为）"

    def test_task_token_budget_syncs_to_resources(self, gov):
        clone = _StubAgent()
        gov.srv._session_clones["s_budget"] = clone
        _post(gov, {"task_token_budget": 123456})
        assert gov.agent.resources.tokens.budget == 123456
        assert clone.resources.tokens.budget == 123456

    def test_budget_auto_continue_read_live(self, gov):
        agent = _bare_agent(gov.agent.config)
        _post(gov, {"budget_auto_continue": 2})
        assert agent._budget_auto_continue() == 2
        _post(gov, {"budget_auto_continue": 0})
        assert agent._budget_auto_continue() == 0


# ═══════════════════════════════════════════════════════════
# 7. 目录级隔离：下次任务生效（对进行中任务不追溯）
# ═══════════════════════════════════════════════════════════


class TestWorkspaceGovernance:
    def test_isolate_workspace_applies_on_next_task_prepare(self, gov, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "a.txt").write_text("hello", encoding="utf-8")
        agent = _bare_agent(gov.agent.config)
        agent.config.project_root = str(project)

        _post(gov, {"isolate_workspace": False})
        shared = agent._prepare_workspace("s_shared")
        assert shared.isolated is False
        assert str(shared.path) == str(project.resolve())

        _post(gov, {"isolate_workspace": True})
        isolated = agent._prepare_workspace("s_isolated")
        assert isolated.isolated is True
        path = str(isolated.path)
        assert ".automind" in path and "workspaces" in path
        assert Path(path).is_dir()
        assert (Path(path) / "a.txt").is_file(), "隔离副本应包含项目文件快照"


# ═══════════════════════════════════════════════════════════
# 8. 自主闭环开关：收尾阶段按新值执行（工作/编程模式）
# ═══════════════════════════════════════════════════════════


class TestAutonomyClosureGovernance:
    def _agent(self, gov):
        agent = _bare_agent(gov.agent.config)
        calls = {"review": 0, "verify": 0, "tdd": 0, "fix_rounds": 0}
        agent.llm = SimpleNamespace()

        async def _review(_task, _output):
            calls["review"] += 1
            return {"approved": True, "available": True, "issues": "", "error": ""}

        async def _loop_verify(_task, _output):
            calls["verify"] += 1
            return {"done": False, "available": True, "source": "stub",
                    "reason": "未完成"}

        async def _project_tests():
            calls["tdd"] += 1
            return {"passed": True, "detail": "1 passed"}

        async def _fix_react(_task, _context):
            calls["fix_rounds"] += 1
            return "修复结果"

        agent._review_result = _review
        agent._loop_verify = _loop_verify
        agent._run_project_tests = _project_tests
        agent._run_react = _fix_react
        agent._verify_state = {}
        return agent, calls

    def test_auto_review_and_auto_test_follow_live_switch(self, gov):
        agent, calls = self._agent(gov)
        agent._interaction = InteractionMode.WORK
        _post(gov, {"auto_review": False, "auto_verify": False, "auto_test": False})
        asyncio.run(agent._autonomy_closure("任务", "结果", ""))
        assert calls["review"] == 0

        _post(gov, {"auto_review": True})
        asyncio.run(agent._autonomy_closure("任务", "结果", ""))
        assert calls["review"] == 1, "auto_review=True 必须真的执行审查阶段"

        agent._interaction = InteractionMode.CODING
        _post(gov, {"auto_test": True, "auto_verify": False})
        asyncio.run(agent._autonomy_closure("任务", "结果", ""))
        assert calls["tdd"] == 1, "auto_test=True 必须执行项目测试环节"

    def test_auto_verify_max_rounds_bounds_fix_rounds(self, gov):
        agent, calls = self._agent(gov)
        agent._interaction = InteractionMode.WORK
        _post(gov, {"auto_review": False, "auto_test": False, "auto_verify": True,
                    "auto_verify_max_rounds": 1, "max_iterations": 50})
        asyncio.run(agent._autonomy_closure("任务", "结果", ""))
        assert calls["verify"] == 2, "验收失败后应再验一次"
        assert calls["fix_rounds"] == 1, "auto_verify_max_rounds=1 只能修复 1 轮"

        calls["fix_rounds"] = 0
        calls["verify"] = 0
        _post(gov, {"auto_verify_max_rounds": 0})
        asyncio.run(agent._autonomy_closure("任务", "结果", ""))
        assert calls["fix_rounds"] == 0, "0 = 验收不通过也不自动修复"

