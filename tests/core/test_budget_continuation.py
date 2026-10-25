"""v1.11.16：任务预算连续性 —— 每个执行入口都拿到一份全新预算。

## 修的是什么

v1.11.0 给普通任务加了"预算窗口自动续开"，但**只有 ``_run_impl_bound``
（ReAct / Plan 路径）会重置预算**：

* ``run_multi`` / ``run_loop`` 只 reset 了 LLM 的显示计数，ResourceManager
  的已用量与预算窗口还是上一个任务留下的 —— 同一个 Web 会话里先跑一个长
  任务、再切协同/循环模式，**第一个模型调用就可能直接撞预算**；
* 子智能体 ``run_subtask`` 直接改写父 Agent 共享 ResourceManager 的 budget，
  父/子并发时互相踩，恢复也不完整；
* 断点续跑沿用已用尽的额度，用户点了"继续"第一次调用又失败。

本文件验的是"每个任务/每次续跑一份预算"这条契约在**每个入口**都成立：
普通任务、会话克隆、子智能体、multi、loop，以及 ``budget_auto_continue``
为 0 / N 时窗口续开行为不变。multi/loop 的可续跑检查点与断点续跑见
``pro/tests/test_multi_loop_budget_resume.py``。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from automind.agent import AutoMindAgent
from automind.core.config import AgentConfig
from automind.core.exceptions import BudgetExhaustedError
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


class _FakeLLM:
    """预算链路需要的 LLM 替身（不联网）。"""

    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.usage_sink = None
        self.pre_call_hook = None
        self.heartbeat_hook = None
        self.call_timeout = 0.0
        self.reset_calls = 0
        self.generate_error: Exception | None = None
        self.agent: AutoMindAgent | None = None
        self.seen_budgets: list[int] = []

    def reset(self) -> None:
        self.reset_calls += 1
        self.usage = TokenUsage()

    async def generate(self, messages, tools=None, stop=None) -> LLMResponse:
        if self.agent is not None and getattr(self.agent, "resources", None) is not None:
            self.seen_budgets.append(self.agent.resources.tokens.budget)
        if self.generate_error is not None:
            raise self.generate_error
        return LLMResponse(text="ok")


class _ProbeTool(AbstractTool):
    name = "probe"
    description = "records contexts"
    parameters = {"type": "object", "properties": {}, "required": []}
    permission_tier = PermissionTier.SAFE

    async def execute(self, **kwargs):
        return ToolResult(tool_name=self.name, success=True, output="probe-ok")


def _fake_agent(tmp_path, *, budget: int = 1000, auto_continue: int = 2,
                max_seconds: float = 0.0,
                interaction: InteractionMode = InteractionMode.WORK
                ) -> tuple[AutoMindAgent, list[dict]]:
    """不用 ``AutoMindAgent(...)`` 全量构造（那要探测环境/建记忆库），
    但保留产品代码真正会碰到的那些属性与方法绑定。"""
    agent = object.__new__(AutoMindAgent)
    agent.llm = _FakeLLM()
    agent.llm.agent = agent
    agent.resources = ResourceManager(token_budget=budget, rate_limit=1e6)
    agent.config = SimpleNamespace(
        project_root=str(tmp_path),
        execution=SimpleNamespace(
            max_task_seconds=max_seconds,
            budget_auto_continue=auto_continue,
            loop_max_iterations=8,
            max_iterations=10,
            llm_call_timeout_seconds=5.0,
            react_tool_budget=10,
            compact_keep_obs_chars=0,
            react_repeat_action_threshold=2,
            react_no_progress_limit=0,
            react_result_cache=True,
            react_tool_timeout_seconds=30.0,
            react_tool_timeout_max_seconds=60.0,
            checkpoint_enabled=True,
        ),
        memory=SimpleNamespace(short_term_max_tokens=128000),
    )
    agent._usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "calls": 0}
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
    agent._workspace_plan = None
    agent._resume_origin = None
    agent._current_plan = None
    agent._agent_state = AgentState()
    agent._interaction = interaction
    agent._mode = ExecutionMode.REACT
    agent.session_id = "s_budget"
    agent._subagent_depth = 0
    agent._interjections = InterjectionQueue()
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register(_ProbeTool())
    agent.permissions = None
    agent.approval_callback = None
    agent.react_executor = None
    agent.plan_executor = SimpleNamespace(deadline_shift=0.0)
    agent.context_mgr = SimpleNamespace(get_context=lambda: [])
    agent.checkpoint_mgr = CheckpointManager(tmp_path / "ck")
    agent.orchestrator = None
    agent.workflow = SimpleNamespace()
    agent.prefer_tools = []
    agent.hooks = SimpleNamespace()
    agent.env = SimpleNamespace()

    def _drain() -> list:
        return []

    agent.drain_interjections = _drain
    events: list[dict] = []

    async def _sink(ev):
        events.append(ev)

    agent.event_sink = _sink
    return agent, events


async def _burn(agent, prompt: int, completion: int = 0) -> None:
    await agent.llm.usage_sink({
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion})


# ═══════════════════════════════════════════════════════════
# 1. 统一预算引导：一个入口把该重置的全重置
# ═══════════════════════════════════════════════════════════


async def test_begin_task_budget_resets_every_axis(tmp_path):
    agent, _ = _fake_agent(tmp_path, budget=1000, max_seconds=60)
    agent.resources.tokens.track_usage(700, 100)
    agent._budget_windows_used = 3
    agent._budget_spent_prior = 5000
    agent._deadline_shift = 120.0
    agent._budget_warned = True
    agent._next_compact_at = 0.99
    agent._usage_total["calls"] = 7
    agent._plan_rejected = "被驳回"
    before = agent.llm.reset_calls

    agent._begin_task_budget()

    assert agent.resources.tokens.tokens_used.total == 0, "ResourceManager 用量没归零"
    assert agent._budget_windows_used == 0 and agent._budget_spent_prior == 0
    assert agent._deadline_shift == 0.0
    assert agent._budget_warned is False
    assert agent._next_compact_at == AutoMindAgent._BUDGET_WARN_AT
    assert agent._usage_total["calls"] == 0
    assert agent._plan_rejected == ""
    assert agent.llm.reset_calls == before + 1, "LLM 显示计数也要按任务重置"
    assert agent._task_deadline is not None
    assert agent._task_deadline > time.monotonic(), "墙钟预算要按任务重设"


async def test_begin_task_budget_without_deadline_config(tmp_path):
    agent, _ = _fake_agent(tmp_path, max_seconds=0)
    agent._task_deadline = time.monotonic() + 999
    agent._begin_task_budget()
    assert agent._task_deadline is None, "max_task_seconds=0 表示不设墙钟上限"


async def test_consecutive_tasks_do_not_inherit_usage(tmp_path):
    """同一 Agent 连续跑多个任务：第二个任务从 0 起算，不背第一个任务的账。"""
    agent, _ = _fake_agent(tmp_path, budget=1000)
    for _ in range(2):
        agent._begin_task_budget()
        assert agent.resources.tokens.tokens_used.total == 0
        agent.resources.tokens.track_usage(600, 0)
    agent._begin_task_budget()
    agent.resources.tokens.track_usage(100, 0)
    assert agent.resources.tokens.tokens_used.total == 100
    assert agent._budget_windows_used == 0


def test_begin_task_budget_is_called_by_every_entry_point():
    """静态护栏：所有执行入口都必须调用统一预算引导。

    这类遗漏（"只在一个入口重置"）没有崩溃可看，只表现为"换个模式跑就撞
    预算"，所以用源码检查把它钉住。
    """
    import inspect

    from automind import agent as agent_mod
    from automind.planning import subagents as sub_mod

    src = inspect.getsource(agent_mod)
    required = {
        "普通任务": "async def _run_impl_bound",
        "multi": "async def run_multi",
        "loop": "async def run_loop",
        "子任务": "async def run_subtask",
        "计划续跑": "async def _resume_plan",
        "multi/loop 续跑": "async def _resume_run_checkpoint",
    }
    missing = []
    for label, marker in required.items():
        start = src.find(marker)
        assert start >= 0, f"找不到 {label} 入口（{marker}）"
        end = src.find("\n    async def ", start + 10)
        body = src[start:end if end > 0 else len(src)]
        if "_begin_task_budget" not in body:
            missing.append(label)
    assert not missing, f"这些入口没有调用 _begin_task_budget：{missing}"
    # 子智能体管理器必须把预算耗尽记成可重试结局，而不是静默当成功
    sub_src = inspect.getsource(sub_mod)
    assert "budget_exhausted" in sub_src
    assert "resumable" in sub_src


# ═══════════════════════════════════════════════════════════
# 2. 会话克隆：预算各自一份，互不影响
# ═══════════════════════════════════════════════════════════


def test_clone_gets_its_own_budget(tmp_path):
    """会话克隆必须独享 ResourceManager：父会话烧掉的额度不该算到克隆头上。"""
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpts")
    cfg.memory.chroma_persist_dir = str(tmp_path / "chroma")
    parent = AutoMindAgent(cfg)
    try:
        parent.session_id = "s_clone"
        clone = parent.clone_for_session()
        try:
            assert clone.resources is not parent.resources
            parent.resources.tokens.track_usage(800, 0)
            clone._begin_task_budget()
            assert clone.resources.tokens.tokens_used.total == 0
            assert parent.resources.tokens.tokens_used.total == 800, \
                "克隆的预算引导不能清掉父会话的用量"
        finally:
            import asyncio
            asyncio.run(clone.close())
    finally:
        import asyncio
        asyncio.run(parent.close())


# ═══════════════════════════════════════════════════════════
# 3. 子智能体：自己的预算 + 耗尽如实返回
# ═══════════════════════════════════════════════════════════


async def test_subtask_uses_own_budget_and_keeps_parent_resources(tmp_path):
    agent, _ = _fake_agent(tmp_path, budget=1000)
    parent_resources = agent.resources
    agent.resources.tokens.track_usage(900, 0)     # 父任务几乎用光
    agent.llm.generate_error = BudgetExhaustedError(
        "Token 预算已用尽", code="BUDGET_EXHAUSTED",
        details={"used": 1000, "budget": 1000})

    result = await agent.run_subtask("做一件事", allowed_tools=["probe"],
                                     max_steps=2, max_tokens=1234)

    assert agent.resources is parent_resources, "子任务不能替换掉父 Agent 的资源对象"
    assert parent_resources.tokens.tokens_used.total == 900, "父任务预算被改写了"
    assert agent.llm.seen_budgets and agent.llm.seen_budgets[0] == 1234, \
        "子任务必须用 max_tokens 作为自己的预算"
    assert result["success"] is False
    assert result["stop_reason"] == "budget_exhausted"
    assert result["resumable"] is True and result["budget_exhausted"] is True
    assert result["steps"] >= 1
    assert "预算" in result["error"]


async def test_subtask_success_path_unchanged(tmp_path):
    agent, _ = _fake_agent(tmp_path, budget=1000)
    agent.llm.generate_error = None
    result = await agent.run_subtask("做一件事", allowed_tools=["probe"],
                                     max_steps=2, max_tokens=1234)
    assert result["success"] is True, result
    assert result["stop_reason"] in ("no_more_tools", "done")


async def test_subtask_auto_continue_windows_apply(tmp_path):
    """子智能体也吃 ``budget_auto_continue``：一个窗口用满会压缩后自动续开。"""
    agent, events = _fake_agent(tmp_path, budget=1000, auto_continue=2)
    agent._attach_usage_sink()

    async def _noop_compress():
        return None

    agent._compress_context = _noop_compress
    llm = agent.llm
    calls = {"n": 0}

    async def _generate(messages, tools=None, stop=None):
        calls["n"] += 1
        await llm.pre_call_hook()          # 预算准入（含续窗）
        await llm.usage_sink({"prompt_tokens": 60, "completion_tokens": 0,
                              "total_tokens": 60})
        return LLMResponse(text=f"第 {calls['n']} 步",
                           tool_calls=[ToolCall(id=str(calls["n"]), name="probe",
                                                arguments={"i": calls["n"]})])

    llm.generate = _generate

    result = await agent.run_subtask("做一件事", allowed_tools=["probe"],
                                     max_steps=20, max_tokens=100)

    assert result["stop_reason"] == "budget_exhausted", result
    rolls = [e for e in events if e.get("type") == "budget_window"]
    assert len(rolls) == 2, f"子任务应续开 2 个窗口（auto_continue=2），实际 {len(rolls)}"


# ═══════════════════════════════════════════════════════════
# 4. multi / loop：入口自己重置预算（窗口不跨任务累计）
# ═══════════════════════════════════════════════════════════


class _FakeOrchestrator:
    def __init__(self, agent) -> None:
        self.agent = agent
        self.seen: list[int] = []

    async def run(self, task, context="", on_event=None, resume=None):
        self.seen.append(self.agent.resources.tokens.tokens_used.total)
        self.agent.resources.tokens.track_usage(500, 0)   # 假装烧掉半个窗口
        return {
            "output": "协同完成",
            "plan": [{"role": "planner", "subtask": "a"}],
            "steps": [{"role": "planner", "label": "规划", "subtask": "a",
                       "output": "ok"}],
            "scratch": "",
        }


class _FakeLoopEngine:
    def __init__(self, agent) -> None:
        self.agent = agent
        self.seen: list[int] = []

    async def run(self, agent, task, on_event=None, max_iterations=None,
                  resume=None):
        self.seen.append(agent.resources.tokens.tokens_used.total)
        agent.resources.tokens.track_usage(300, 0)
        return {"output": "循环完成", "iterations": 1, "success": True,
                "stop_reason": "completed",
                "progress": {"iterations": 1, "last_output": "循环完成",
                             "feedback": "", "max_iterations": 8}}


def _inject_feature(monkeypatch, name: str, impl) -> None:
    from automind.core import edition

    monkeypatch.setitem(edition._state["features"], name, impl)
    monkeypatch.setitem(edition._state, "loaded", True)


async def test_multi_task_gets_fresh_budget(tmp_path, monkeypatch):
    agent, _ = _fake_agent(tmp_path, budget=1000,
                           interaction=InteractionMode.MULTI)
    orchestrator = _FakeOrchestrator(agent)
    _inject_feature(monkeypatch, "multi_agent",
                    SimpleNamespace(create=lambda _llm: orchestrator))

    first = await agent.run_multi("任务一")
    second = await agent.run_multi("任务二")

    assert orchestrator.seen == [0, 0], "第二个 multi 任务继承了上一个任务的用量"
    assert first["success"] is True and second["success"] is True
    assert first["checkpoint_id"] and second["checkpoint_id"]
    state = agent.checkpoint_mgr.load_sync(second["checkpoint_id"])
    assert state.metadata["run_mode"] == "multi"
    assert state.metadata["status"] == "completed"
    assert state.metadata["progress"]["steps_done"] == 1


async def test_loop_task_gets_fresh_budget(tmp_path, monkeypatch):
    agent, _ = _fake_agent(tmp_path, budget=1000,
                           interaction=InteractionMode.LOOP)
    engine = _FakeLoopEngine(agent)
    _inject_feature(monkeypatch, "loop_engine", engine)

    first = await agent.run_loop("任务一")
    second = await agent.run_loop("任务二")

    assert engine.seen == [0, 0], "第二个 loop 任务继承了上一个任务的用量"
    assert first["success"] is True and second["success"] is True
    state = agent.checkpoint_mgr.load_sync(second["checkpoint_id"])
    assert state.metadata["run_mode"] == "loop"
    assert state.metadata["status"] == "completed"
    assert state.metadata["progress"]["iterations"] == 1


# ═══════════════════════════════════════════════════════════
# 5. 预算窗口：auto_continue=0 / N 的行为不变，续开后事件照发
# ═══════════════════════════════════════════════════════════


async def test_auto_continue_n_rolls_window_and_continues(tmp_path):
    agent, events = _fake_agent(tmp_path, budget=100, auto_continue=2)
    agent._attach_usage_sink()

    async def _noop():
        events.append({"type": "_compressed"})

    agent._compress_context = _noop
    await _burn(agent, 100)
    await agent.llm.pre_call_hook()          # 不抛：续开了新窗口

    types = [e["type"] for e in events]
    assert "budget_window" in types
    assert "budget_exceeded" not in types
    assert agent.resources.tokens.tokens_used.total == 0
    assert agent._budget_windows_used == 1


async def test_auto_continue_zero_refuses_but_next_task_has_fresh_budget(tmp_path):
    agent, events = _fake_agent(tmp_path, budget=100, auto_continue=0)
    agent._attach_usage_sink()

    async def _noop():
        return None

    agent._compress_context = _noop
    await _burn(agent, 100)
    with pytest.raises(BudgetExhaustedError):
        await agent.llm.pre_call_hook()
    assert any(e.get("type") == "budget_exceeded" for e in events)
    assert not [e for e in events if e.get("type") == "budget_window"]

    # 新任务：同一 Agent 重新引导预算后必须能继续调用
    agent._begin_task_budget()
    await _burn(agent, 10)
    await agent.llm.pre_call_hook()          # 不再抛
