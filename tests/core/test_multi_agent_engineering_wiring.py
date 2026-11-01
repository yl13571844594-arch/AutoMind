"""v1.11.22 工程协同执行接口：核心侧接线、角色预算与多智能体记录字段。

## 这个文件盯住什么

工程协同编排器（``automind_pro.multiagent.EngineeringOrchestrator``）要让角色
"真正动手"，依赖核心侧三个稳定接口：

1. ``run_multi`` 创建编排器时必须把**父 Agent** 传进去（``feature.create(...,
   agent=self)``）—— 编排器要用 ``clone_for_session()`` / 工具注册表 /
   ``_review_result`` / ``_loop_verify``；但旧 Feature 与测试替身只接受 llm，
   所以 ``TypeError`` 必须回退成 ``create(llm)``，不能把"签名不兼容"变成
   "协同模式直接不可用"；
2. ``_finish_run_mode`` 收尾时**不能覆盖**编排器聚合的 ``token_usage``，也不能
   吞掉 ``verification`` / ``metrics`` 这类验收证据字段（否则历史页只剩父会话
   的调用数，多角色真实成本对不上账、验证结论也看不见）；
3. ``run_subtask`` 新增 ``max_iteration_extensions``（角色独立步数预算需要
   ``0`` = 不自动续步，否则"角色最多 N 步"会被 ReAct 自动续加放大成 2N 步），
   并把本子任务自己的 ``usage`` / ``llm_calls`` 交回编排器做成本聚合；不传该
   参数时既有子智能体行为一字不变。

另外锁住 ``ExecutionConfig`` 的 multi_agent_* 默认值与 server 多智能体记录
（``/api/run`` 与 ``_ws_run`` 两条路径）的字段口径。全部使用 fake LLM /
fake Feature，不联网、不调真实模型。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from automind.agent import AutoMindAgent
from automind.core import edition
from automind.core.config import AgentConfig, ExecutionConfig
from automind.core.interject import InterjectionQueue
from automind.core.types import (
    AgentState,
    ExecutionMode,
    InteractionMode,
    LLMResponse,
    PermissionTier,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from automind.state.checkpoint import CheckpointManager
from automind.state.resource_manager import ResourceManager
from automind.tools.base import AbstractTool, ToolRegistry

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _ProbeTool(AbstractTool):
    """只读探针：每次调用都成功（用来触发 ReAct 的"仍在推进"续步判定）。"""

    name = "probe"
    description = "records executions"
    parameters = {"type": "object", "properties": {}, "required": []}
    permission_tier = PermissionTier.SAFE

    async def execute(self, **kwargs):
        return ToolResult(tool_name=self.name, success=True, output="probe-ok")


class _ScriptedLLM:
    """按脚本返回响应的 LLM 替身；**真实累计** usage（子任务成本断言靠它）。"""

    def __init__(self, script: list[LLMResponse] | None = None) -> None:
        self.script = list(script or [])
        self.usage = TokenUsage()
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1
        self.usage = TokenUsage()

    async def generate(self, messages, tools=None, stop=None):
        self.usage.calls += 1
        self.usage.prompt_tokens += 10
        self.usage.completion_tokens += 5
        if self.script:
            return self.script.pop(0)
        return LLMResponse(text="完成")


class _AlwaysToolLLM(_ScriptedLLM):
    """每步都返回一次 probe 调用：角色"仍在推进"，会触发续步判定。"""

    async def generate(self, messages, tools=None, stop=None):
        await super().generate(messages, tools=tools, stop=stop)
        index = self.usage.calls
        return LLMResponse(
            text=f"第 {index} 步",
            tool_calls=[ToolCall(id=str(index), name="probe",
                                 arguments={"i": index})],
        )


class _FakeOrchestrator:
    """最小编排器替身：记录调用、可选地在"运行期间"改写父用量、返回预置结果。"""

    def __init__(self, result: dict, on_run=None) -> None:
        self.result = result
        self.on_run = on_run
        self.calls: list[dict] = []

    async def run(self, task, context="", on_event=None, resume=None):
        self.calls.append({"task": task, "context": context,
                           "on_event": on_event, "resume": resume})
        if self.on_run is not None:
            self.on_run()
        return dict(self.result)


class _NewFeature:
    """新版 Feature：``create(llm, agent=None)`` —— 首选路径。"""

    def __init__(self, result: dict, on_run=None) -> None:
        self.result = result
        self.on_run = on_run
        self.created: list[dict] = []

    def create(self, llm, agent=None):
        self.created.append({"llm": llm, "agent": agent})
        return _FakeOrchestrator(self.result, self.on_run)


class _LegacyFeature:
    """旧 Feature 的真实签名：**只接受 llm**（传 agent= 由 Python 抛 TypeError）。

    这正是社区老版本 / 既有测试替身的形态：核心必须回退，而不是让协同模式
    因签名不兼容直接报错。
    """

    def __init__(self, result: dict) -> None:
        self.result = result
        self.created: list[dict] = []

    def create(self, llm):
        self.created.append({"llm": llm})
        return _FakeOrchestrator(self.result)


def _inject_feature(monkeypatch: pytest.MonkeyPatch, feature) -> None:
    """把 ``multi_agent`` 特性替身注入 edition 注册表（不加载真实商业包）。"""
    monkeypatch.setitem(edition._state["features"], "multi_agent", feature)
    monkeypatch.setitem(edition._state, "loaded", True)


# ── 两种 fake Agent：run_multi（模式级）与 run_subtask（执行器级）──────────


def _mode_agent(tmp_path) -> tuple[AutoMindAgent, list[dict]]:
    """``run_multi`` 级测试用的"真实例 + 假依赖" Agent（不建记忆库/不探环境）。"""
    agent = object.__new__(AutoMindAgent)
    agent.llm = _ScriptedLLM()
    agent.resources = ResourceManager(token_budget=10_000, rate_limit=1e6)
    agent.config = SimpleNamespace(
        project_root=str(tmp_path),
        execution=SimpleNamespace(
            max_task_seconds=0.0, budget_auto_continue=2,
            loop_max_iterations=8, max_iterations=10,
            checkpoint_enabled=True,
        ),
        memory=SimpleNamespace(short_term_max_tokens=128000),
    )
    agent._usage_total = {}
    agent._budget_warned = False
    agent._next_compact_at = agent._BUDGET_WARN_AT
    agent._budget_windows_used = 0
    agent._budget_spent_prior = 0
    agent._deadline_shift = 0.0
    agent._last_context_compact_prompt = 0
    agent._task_deadline = None
    agent._plan_rejected = ""
    agent._last_checkpoint_at = 0.0
    agent._checkpoint_saves = 0
    agent._verify_state = {}
    agent._resume_origin = None
    agent._current_plan = None
    agent._agent_state = AgentState()
    agent._interaction = InteractionMode.MULTI
    agent._mode = ExecutionMode.REACT
    agent.session_id = "s_multi"
    agent._subagent_depth = 0
    agent._interjections = InterjectionQueue()
    agent.orchestrator = None
    agent.context_mgr = SimpleNamespace(get_context=lambda: [])
    agent.checkpoint_mgr = CheckpointManager(tmp_path / "ck")
    agent.react_executor = None
    agent.plan_executor = SimpleNamespace(deadline_shift=0.0)
    agent.workflow = SimpleNamespace()
    agent.prefer_tools = []
    agent.hooks = SimpleNamespace()
    agent.env = SimpleNamespace()
    agent.drain_interjections = lambda: []
    events: list[dict] = []

    async def _sink(ev):
        events.append(ev)

    agent.event_sink = _sink
    return agent, events


def _subtask_agent(tmp_path, llm) -> AutoMindAgent:
    """``run_subtask`` 级测试用的最小 Agent（只提供该方法真正会碰的属性）。"""
    agent = object.__new__(AutoMindAgent)
    agent.llm = llm
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register(_ProbeTool())
    agent.permissions = None
    agent.approval_callback = None
    agent.config = SimpleNamespace(
        project_root=str(tmp_path),
        execution=SimpleNamespace(
            max_task_seconds=0.0,
            react_tool_budget=10,
            compact_keep_obs_chars=0,
            react_repeat_action_threshold=2,
            react_no_progress_limit=0,
            react_result_cache=True,
            react_tool_timeout_seconds=30.0,
            react_tool_timeout_max_seconds=60.0,
        ),
        memory=SimpleNamespace(short_term_max_tokens=128000),
    )
    agent.resources = ResourceManager(token_budget=10_000, rate_limit=1e6)
    agent.session_id = "s_subtask"
    agent._subagent_depth = 0
    agent._interjections = InterjectionQueue()
    agent.react_executor = None
    agent.plan_executor = SimpleNamespace(deadline_shift=0.0)
    agent.event_sink = None
    agent.drain_interjections = lambda: []
    return agent


# ═══════════════════════════════════════════════════════════
# 1. run_multi → 编排器创建：父 Agent 传递与旧签名回退
# ═══════════════════════════════════════════════════════════


class TestRunMultiFeatureWiring:
    async def test_new_feature_receives_parent_agent(self, tmp_path, monkeypatch):
        """新 Feature 必须拿到父 Agent（角色才能 clone / 用工具 / 走验收）。"""
        agent, _ = _mode_agent(tmp_path)
        feature = _NewFeature({"output": "协同完成", "plan": [
            {"role": "planner", "subtask": "a"}], "steps": []})
        _inject_feature(monkeypatch, feature)

        res = await agent.run_multi("做一个功能")

        assert res["success"] is True
        assert len(feature.created) == 1
        created = feature.created[0]
        assert created["llm"] is agent.llm
        assert created["agent"] is agent, \
            "工程协同编排器必须拿到父 Agent（clone_for_session/工具表/验收方法都在它上面）"
        assert isinstance(agent.orchestrator, _FakeOrchestrator)

    async def test_legacy_feature_falls_back_to_llm_only(self, tmp_path, monkeypatch):
        """旧 Feature 只接受 llm：首调用抛 TypeError 后回退，协同模式照常可用。"""
        agent, _ = _mode_agent(tmp_path)
        feature = _LegacyFeature({"output": "旧编排器完成", "plan": [],
                                  "steps": []})
        _inject_feature(monkeypatch, feature)

        res = await agent.run_multi("纯文本任务")

        assert res["success"] is True, "旧签名不该让协同模式整体不可用"
        assert len(feature.created) == 1, "回退后只应成功构造一次编排器"
        assert isinstance(agent.orchestrator, _FakeOrchestrator)

    async def test_orchestrator_is_reused_across_runs(self, tmp_path, monkeypatch):
        """编排器只构造一次（第二次 run_multi 复用，不丢角色状态/缓存）。"""
        agent, _ = _mode_agent(tmp_path)
        feature = _NewFeature({"output": "完成", "plan": [], "steps": []})
        _inject_feature(monkeypatch, feature)

        await agent.run_multi("任务一")
        await agent.run_multi("任务二")

        assert len(feature.created) == 1
        assert agent.orchestrator.calls[-1]["task"] == "任务二"


# ═══════════════════════════════════════════════════════════
# 2. run_multi 收尾：聚合 token_usage 与 verification/metrics 保留
# ═══════════════════════════════════════════════════════════


class TestRunMultiAggregation:
    async def test_aggregated_usage_and_engineering_fields_are_preserved(
            self, tmp_path, monkeypatch):
        """编排器聚合的 token_usage / verification / metrics 不能被收尾覆盖。"""
        agent, _ = _mode_agent(tmp_path)
        aggregated = TokenUsage(prompt_tokens=120, completion_tokens=30, calls=3)
        verification = {"tests": {"available": True, "passed": True},
                        "llm": {"available": True, "done": True},
                        "done": True}
        metrics = {"duration_ms": 1234.5, "roles": 2, "llm_calls": 3,
                   "parallel_batches": 1}
        plan = [{"id": "s1", "role": "coder", "subtask": "写文件",
                 "depends_on": [], "write_paths": ["a.py"]}]
        steps = [{"id": "s1", "role": "coder", "output": "ok",
                  "success": True, "tokens": 150}]
        feature = _NewFeature({
            "output": "工程协同完成", "plan": plan, "steps": steps,
            "scratch": "白板", "success": True, "stop_reason": "completed",
            "token_usage": aggregated,
            "verification": verification, "metrics": metrics,
        })
        _inject_feature(monkeypatch, feature)

        res = await agent.run_multi("实现一个功能")

        assert res["token_usage"] is aggregated, \
            "收尾不能用父 llm.usage 覆盖编排器聚合的各角色用量"
        assert res["verification"] == verification
        assert res["metrics"] == metrics
        assert res["plan"] == plan and res["steps"] == steps
        assert res["success"] is True and res["checkpoint_id"]

    async def test_explicit_failure_is_not_rewritten_to_success(
            self, tmp_path, monkeypatch):
        """工程协同验证未通过（无异常、success=False）不能被收尾改写成成功。

        "跑完了" ≠ "做成了"：验证闭环如实返回失败时，历史记录/前端必须看到
        ``success=False`` 与 ``verification_failed``，同时保留验证证据字段。
        """
        agent, _ = _mode_agent(tmp_path)
        verification = {"tests": {"available": True, "passed": False},
                        "llm": {"available": True, "done": False},
                        "done": False}
        feature = _NewFeature({
            "output": "验证未通过：2 个测试失败", "success": False,
            "stop_reason": "verification_failed",
            "plan": [{"id": "s1", "role": "coder", "subtask": "写代码"}],
            "steps": [{"id": "s1", "role": "coder", "output": "改了 a.py",
                       "success": True}],
            "verification": verification,
            "metrics": {"roles": 1, "verify_rounds": 1},
        })
        _inject_feature(monkeypatch, feature)

        res = await agent.run_multi("实现一个功能")

        assert res["success"] is False, "验证失败的协同任务不能报成功"
        assert res["stop_reason"] == "verification_failed"
        assert "验证未通过" in res["output"]
        assert res["verification"] == verification
        assert res["metrics"] == {"roles": 1, "verify_rounds": 1}
        assert res["checkpoint_id"], "失败也要留下可诊断的断点/进度"
        assert res["resumable"] is True
        assert agent.checkpoint_mgr.load_sync(
            res["checkpoint_id"]).metadata["status"] == "partial"

    @pytest.mark.parametrize("payload", [
        {},
        {"token_usage": None},
    ])
    async def test_missing_usage_falls_back_to_agent_llm_usage(
            self, tmp_path, monkeypatch, payload):
        """旧编排器没有 token_usage 时回退本 Agent 用量（行为向后兼容）。"""
        agent, _ = _mode_agent(tmp_path)
        result = {"output": "旧编排器完成", "plan": [], "steps": []}
        result.update(payload)

        def _mark_agent_usage() -> None:
            # 编排器运行期间父 Agent 的真实用量（_begin_task_budget 已 reset 过）
            agent.llm.usage = TokenUsage(prompt_tokens=7, completion_tokens=3,
                                         calls=2)

        feature = _NewFeature(result, on_run=_mark_agent_usage)
        _inject_feature(monkeypatch, feature)

        res = await agent.run_multi("任务")

        assert res["token_usage"] is agent.llm.usage
        assert res["token_usage"].total == 10
        assert res["token_usage"].calls == 2


# ═══════════════════════════════════════════════════════════
# 3. run_subtask：角色步数预算（不自动续步）与既有默认行为
# ═══════════════════════════════════════════════════════════


class TestRunSubtaskRoleBudget:
    def test_signature_exposes_extension_budget(self):
        """pro-collab 依赖这个接口：关键字参数、默认 None（不改变旧行为）。"""
        sig = inspect.signature(AutoMindAgent.run_subtask)
        param = sig.parameters["max_iteration_extensions"]
        assert param.default is None
        assert param.kind is inspect.Parameter.KEYWORD_ONLY

    async def test_default_executor_still_extends_iterations(self, tmp_path):
        """不传参数 = 旧行为：仍在推进时自动续加一个步数窗口（2 步）。"""
        agent = _subtask_agent(tmp_path, _AlwaysToolLLM())

        result = await agent.run_subtask(
            "一直干活", allowed_tools=["probe"], max_steps=1, max_tokens=1000)

        assert result["steps"] == 2, \
            "默认子智能体行为必须保持 v1.11.7 的续步语义（角色预算才需要显式关闭）"

    async def test_zero_extensions_stops_at_role_step_limit(self, tmp_path):
        """``max_iteration_extensions=0``：角色步数预算是硬上限（1 步即收尾）。"""
        agent = _subtask_agent(tmp_path, _AlwaysToolLLM())

        result = await agent.run_subtask(
            "一直干活", allowed_tools=["probe"], max_steps=1, max_tokens=1000,
            max_iteration_extensions=0)

        assert result["steps"] == 1, "角色预算必须到上限就收尾，不能被自动续步放大"
        assert result["success"] is False
        assert result["stop_reason"] == "max_iterations"

    async def test_negative_extensions_is_clamped_to_zero(self, tmp_path,
                                                          monkeypatch):
        """负数按 0 处理，不会把执行器的续步计数变成负数语义。"""
        from automind.planning import react_executor as re_mod

        captured: dict = {}
        real_cls = re_mod.ReActExecutor

        class _SpyExecutor(real_cls):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["executor"] = self

        monkeypatch.setattr(re_mod, "ReActExecutor", _SpyExecutor)
        agent = _subtask_agent(tmp_path, _AlwaysToolLLM())

        await agent.run_subtask(
            "一直干活", allowed_tools=["probe"], max_steps=1, max_tokens=1000,
            max_iteration_extensions=-5)

        assert captured["executor"].max_iteration_extensions == 0


# ═══════════════════════════════════════════════════════════
# 4. run_subtask：返回自己的 usage / llm_calls（编排器成本聚合）
# ═══════════════════════════════════════════════════════════


class TestRunSubtaskUsage:
    async def test_result_carries_usage_and_llm_calls(self, tmp_path):
        agent = _subtask_agent(tmp_path, _ScriptedLLM([
            LLMResponse(text="look", tool_calls=[
                ToolCall(id="1", name="probe", arguments={})]),
            LLMResponse(text="done"),
        ]))

        result = await agent.run_subtask(
            "探针一下", allowed_tools=["probe"], max_steps=3, max_tokens=1000)

        assert result["success"] is True, result
        assert result["usage"] is agent.llm.usage, \
            "返回的必须是本子任务自己的 TokenUsage 对象（供编排器聚合）"
        assert result["llm_calls"] == 2 == agent.llm.usage.calls
        assert result["tokens"] == agent.llm.usage.total == 30
        # 既有字段一个不少（旧调用方不受影响）
        for key in ("success", "output", "steps", "tokens",
                    "stop_reason", "error"):
            assert key in result

    async def test_bad_args_result_also_has_usage_shape(self, tmp_path):
        """参数校验失败提前返回时也保持同一返回形状（0 次调用）。"""
        agent = _subtask_agent(tmp_path, _ScriptedLLM())

        result = await agent.run_subtask("x", allowed_tools=[])

        assert result["success"] is False and "allowed_tools" in result["error"]
        assert result["usage"] is agent.llm.usage
        assert result["llm_calls"] == 0


# ═══════════════════════════════════════════════════════════
# 5. ExecutionConfig：多智能体工程协同默认参数
# ═══════════════════════════════════════════════════════════


class TestExecutionConfigEngineeringKnobs:
    def test_defaults_match_v11122_contract(self):
        ex = ExecutionConfig()
        assert ex.multi_agent_max_parallel == 2
        assert ex.multi_agent_role_max_steps == 30
        assert ex.multi_agent_role_max_tokens == 300_000
        assert ex.multi_agent_role_timeout_seconds == 300
        assert ex.multi_agent_max_rework == 1
        assert ex.multi_agent_verify is True
        assert ex.multi_agent_review_writes is True

    def test_overridable_without_breaking_old_configs(self):
        ex = ExecutionConfig(multi_agent_role_max_steps=5,
                             multi_agent_verify=False)
        assert ex.multi_agent_role_max_steps == 5
        assert ex.multi_agent_verify is False
        # 旧配置（没写这些键）反序列化仍可用：全部有默认值
        cfg = AgentConfig(project_root=".")
        assert cfg.execution.multi_agent_role_max_tokens == 300_000


# ═══════════════════════════════════════════════════════════
# 6. server：/api/run 与 _ws_run 的多智能体记录字段
# ═══════════════════════════════════════════════════════════


class TestServerMultiRecordFields:
    def _server(self):
        return pytest.importorskip("automind.server")

    def test_engineering_fields_written_only_when_present(self):
        srv = self._server()
        ma = {
            "stop_reason": "completed",
            "progress": {"steps_done": 2, "steps_total": 2},
            "verification": {"done": True, "tests": {"passed": True}},
            "metrics": {"roles": 2, "llm_calls": 3},
            "output": "完成",
        }

        fields = srv._multi_record_optional_fields(ma)

        assert fields == {
            "stop_reason": "completed",
            "progress": {"steps_done": 2, "steps_total": 2},
            "verification": {"done": True, "tests": {"passed": True}},
            "metrics": {"roles": 2, "llm_calls": 3},
        }, "记录里必须带验证/指标摘要"

        # 旧文本编排器（没有这两个键）：字段形状保持不变，不写空值
        legacy = srv._multi_record_optional_fields(
            {"stop_reason": "completed", "output": "完成"})
        assert legacy == {"stop_reason": "completed"}
        assert "verification" not in legacy and "metrics" not in legacy

    def test_helper_tolerates_non_dict(self):
        srv = self._server()
        assert srv._multi_record_optional_fields(None) == {}
        assert srv._multi_record_optional_fields("x") == {}

    def test_both_multi_record_paths_use_the_helper_and_aggregated_usage(self):
        """静态护栏：两条记录构造路径（/api/run 与 _ws_run）口径必须一致。

        server 里 /api/run 与 _ws_run 各写一份 multi record，此前是同一段
        dict 推导式手抄两遍 —— 漏改一条的表现是"历史页有验证摘要、实时推送
        没有"。这里把它钉住。
        """
        srv = self._server()
        src = inspect.getsource(srv)
        assert src.count("**_multi_record_optional_fields(ma)") == 2, \
            "/api/run 与 _ws_run 两条 multi 记录路径都必须带 verification/metrics"
        assert src.count('usage = ma.get("token_usage")') == 2, \
            "usage 必须取编排器聚合值（ma['token_usage']），而不是父 llm.usage"
        assert "_MULTI_RECORD_OPTIONAL_KEYS" in src
