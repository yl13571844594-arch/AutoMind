"""v1.11.11 上下文压缩补强：窗口压力触发 + 溢出重试 + 手动 /compact。

原有压缩只看"任务累计 token 预算"（默认 50 万 × 80%），一个每步 prompt
都很大的任务可能先撞模型上下文窗口而失败。这里验证新增的三条安全网：
1. 单次请求接近窗口比例时主动压一次（并且不会每轮都压）；
2. 上下文溢出错误后压缩 + 只重试一次；
3. 手动 ``force_compact()`` 有稳定的返回账目（Web/CLI 都要展示）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from automind.core.exceptions import LLMContextTooLargeError
from automind.core.types import LLMResponse, PermissionTier, ToolResult
from automind.planning.react_executor import ReActExecutor


class _FakeExecutor:
    def __init__(self, folded: int = 2) -> None:
        self.running = True
        self.messages = [{"role": "user", "content": "x"}]
        self.folded = folded
        self.calls = 0

    def compact(self, keep_recent=None) -> dict:
        self.calls += 1
        return {"folded": self.folded, "chars_reclaimed": 1200,
                "est_tokens_saved": 340, "messages": 3}


def _agent(window: int = 1000, ratio: float = 0.5):
    from automind.agent import AutoMindAgent

    agent = object.__new__(AutoMindAgent)
    events: list[dict] = []

    async def sink(ev: dict) -> None:
        events.append(ev)

    agent.event_sink = sink
    agent.config = SimpleNamespace(
        memory=SimpleNamespace(short_term_max_tokens=window),
        execution=SimpleNamespace(context_compact_ratio=ratio),
    )
    agent._usage_total = {"last_prompt_tokens": 0}
    agent._last_context_compact_prompt = 0
    agent.react_executor = _FakeExecutor()
    return agent, events


def _feed(agent, used: int) -> None:
    agent._usage_total["last_prompt_tokens"] = used


class TestContextPressure:
    async def test_below_ratio_does_not_compact(self):
        agent, _ = _agent(window=1000, ratio=0.5)
        _feed(agent, 499)
        await agent._maybe_compact_for_context()
        assert agent.react_executor.calls == 0

    async def test_at_ratio_compacts_once(self):
        agent, events = _agent(window=1000, ratio=0.5)
        _feed(agent, 600)
        await agent._maybe_compact_for_context()
        assert agent.react_executor.calls == 1
        assert any(e.get("type") == "context_pressure" for e in events)
        assert any(e.get("type") == "context_compacted" for e in events)

    async def test_does_not_compact_every_call(self):
        """压一次后要再涨 5% 窗口才压第二次 —— 否则每轮都重置前缀缓存。"""
        agent, _ = _agent(window=1000, ratio=0.5)
        _feed(agent, 600)
        await agent._maybe_compact_for_context()
        _feed(agent, 620)
        await agent._maybe_compact_for_context()
        assert agent.react_executor.calls == 1
        _feed(agent, 660)
        await agent._maybe_compact_for_context()
        assert agent.react_executor.calls == 2

    async def test_only_when_react_is_running(self):
        agent, _ = _agent(window=1000, ratio=0.5)
        agent.react_executor.running = False
        _feed(agent, 900)
        await agent._maybe_compact_for_context()
        assert agent.react_executor.calls == 0

    async def test_force_compact_returns_accounting(self):
        agent, _ = _agent()
        stat = await agent.force_compact()
        assert stat["ok"] is True
        assert stat["scope"] == "react"
        assert stat["folded"] == 2 and stat["est_tokens_saved"] == 340


# ═══════════════════════════════════════════════════════════
# ReAct 溢出重试
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str = "noop") -> None:
        self.name = name
        self.description = "noop"
        self.parameters = {"properties": {}, "required": []}
        self.permission_tier = PermissionTier.SAFE

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []}}


class _Registry:
    def __init__(self) -> None:
        self._tool = _Tool()

    def list_names(self) -> list[str]:
        return [self._tool.name]

    def list_all(self) -> list[_Tool]:
        return [self._tool]

    def get(self, name: str) -> _Tool:
        return self._tool

    async def dispatch(self, name: str, **kw) -> ToolResult:
        return ToolResult(tool_name=name, success=True, output="ok")


class _OverflowOnceLLM:
    def __init__(self, times: int = 1) -> None:
        self.calls = 0
        self.times = times

    async def generate(self, messages, tools=None, stop=None):
        self.calls += 1
        if self.calls <= self.times:
            raise LLMContextTooLargeError("context too large")
        return LLMResponse(text="done")


class TestOverflowRetry:
    def test_compacts_then_retries_once(self):
        llm = _OverflowOnceLLM(times=1)
        ex = ReActExecutor(llm, _Registry(), max_iterations=3)
        text = asyncio.run(ex.run("任务"))
        assert llm.calls == 2, "溢出后应压缩并重试一次"
        assert ex.context_retry_used is True
        assert "done" in text

    def test_second_overflow_propagates(self):
        llm = _OverflowOnceLLM(times=99)
        ex = ReActExecutor(llm, _Registry(), max_iterations=3)
        with pytest.raises(LLMContextTooLargeError):
            asyncio.run(ex.run("任务"))
        assert llm.calls == 2, "只允许重试一次，不能无限循环"


# ═══════════════════════════════════════════════════════════
# 对话模式溢出裁剪
# ═══════════════════════════════════════════════════════════


class TestChatOverflow:
    async def test_keeps_system_and_second_half(self):
        from automind.agent import AutoMindAgent

        agent = object.__new__(AutoMindAgent)
        events: list[dict] = []

        async def sink(ev: dict) -> None:
            events.append(ev)

        agent.event_sink = sink
        messages = [{"role": "system", "content": "SYS"}]
        messages += [{"role": "user" if i % 2 == 0 else "assistant",
                      "content": str(i)} for i in range(10)]
        out = await agent._chat_retry_after_overflow(messages)
        assert out[0] == {"role": "system", "content": "SYS"}
        assert len(out) < len(messages)
        assert out[1]["role"] == "user"
        assert events and events[0]["scope"] == "chat"
        assert events[0]["cache_reset"] is True
