"""v1.11.11 子智能体与 Agent 的接线回归（专业版 multi_agent）。

纯 fake 的子智能体管理器测试（``tests/planning/test_subagents.py``）验证的是
编排逻辑；这里补的是最容易接错的一段：``AutoMindAgent.run_subtask`` 真的
在子执行作用域内绑定了「当前 Agent = 子 Agent、深度 = 父深度 + 1」，并且
默认只把只读工具交给子智能体。
"""

from __future__ import annotations

from types import SimpleNamespace

from automind.agent import AutoMindAgent
from automind.core import session_ctx
from automind.core.interject import InterjectionQueue
from automind.core.types import (
    LLMResponse,
    PermissionTier,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from automind.state.resource_manager import ResourceManager
from automind.tools.base import AbstractTool, ToolRegistry


class _ProbeTool(AbstractTool):
    name = "probe"
    description = "records execution context"
    parameters = {"type": "object", "properties": {}, "required": []}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        self.seen: list[tuple[object, int]] = []

    async def execute(self, **kwargs):
        self.seen.append((session_ctx.agent(), session_ctx.subagent_depth()))
        return ToolResult(tool_name=self.name, success=True, output="probe-ok")


class _WriterTool(AbstractTool):
    name = "writer"
    description = "would write files"
    parameters = {"type": "object", "properties": {}, "required": []}
    permission_tier = PermissionTier.SENSITIVE

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return ToolResult(tool_name=self.name, success=True, output="written")


class _LLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.seen_tools: list[list[str]] = []
        self.usage = TokenUsage()

    async def generate(self, messages, tools=None, stop=None):
        self.seen_tools.append([t.get("name", "") for t in (tools or [])])
        return self._script.pop(0) if self._script else LLMResponse(text="done")

    def reset(self) -> None:
        self.usage = TokenUsage()


def _agent(tmp_path, script: list[LLMResponse]):
    agent = object.__new__(AutoMindAgent)
    agent.llm = _LLM(script)
    agent.tool_registry = ToolRegistry()
    probe, writer = _ProbeTool(), _WriterTool()
    agent.tool_registry.register(probe)
    agent.tool_registry.register(writer)
    agent.permissions = None
    agent.config = SimpleNamespace(
        project_root=str(tmp_path),
        execution=SimpleNamespace(
            react_tool_budget=10, compact_keep_obs_chars=0,
            react_repeat_action_threshold=2, react_no_progress_limit=0,
            react_result_cache=True, react_tool_timeout_seconds=30.0,
            react_tool_timeout_max_seconds=60.0,
        ),
    )
    agent.resources = ResourceManager(token_budget=0)
    agent.session_id = "conv-sub"
    agent._subagent_depth = 0
    agent._interjections = InterjectionQueue()
    agent.event_sink = None
    agent._active_goal_id = None
    agent._deadline_shift = 0.0
    return agent, probe, writer


class TestRunSubtaskContext:
    async def test_child_binds_agent_and_depth(self, tmp_path):
        script = [
            LLMResponse(text="look", tool_calls=[ToolCall(id="1", name="probe",
                                                          arguments={})]),
            LLMResponse(text="done"),
        ]
        agent, probe, _ = _agent(tmp_path, script)

        result = await agent.run_subtask("probe the context", allowed_tools=["probe"],
                                         max_steps=3, max_tokens=1000)

        assert result["success"] is True, result
        assert result["output"].strip() == "done"
        assert probe.seen, "probe 工具没被调用"
        child, depth = probe.seen[0]
        assert child is agent, "子执行作用域内的 agent() 必须是子 Agent"
        assert depth == 1, f"子智能体深度应为 1，实际 {depth}"

    async def test_default_toolset_is_safe_only(self, tmp_path):
        """allowed_tools 省略 = 只读工具；写类工具不能默认交给子智能体。"""
        script = [
            LLMResponse(text="try write", tool_calls=[
                ToolCall(id="1", name="writer", arguments={})]),
            LLMResponse(text="done"),
        ]
        agent, probe, writer = _agent(tmp_path, script)

        result = await agent.run_subtask("do something", max_steps=3)

        assert result["success"] is True, result
        assert writer.calls == 0, "写工具没有被显式授权却执行了"
        offered = agent.llm.seen_tools[0]
        assert "probe" in offered and "writer" not in offered

    async def test_empty_allowlist_is_rejected(self, tmp_path):
        agent, _, _ = _agent(tmp_path, [LLMResponse(text="done")])
        result = await agent.run_subtask("x", allowed_tools=[])
        assert result["success"] is False
        assert "allowed_tools" in (result["error"] or "")
