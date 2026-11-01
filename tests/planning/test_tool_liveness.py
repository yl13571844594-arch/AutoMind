"""ReAct 工具执行可见性（v1.11.20）—— start 事件 + 执行期心跳。

## 修的是什么

此前界面上**只有工具跑完之后**才有 ``step_action``。执行 ``npm install`` /
完整测试这种一分钟起步的命令时，界面整整一分钟没有任何动静，用户分不清
"在跑"还是"卡死了"。v1.11.20 补两个事件：

* ``step_action_start``：dispatch 前立即推，含工具名/参数摘要/超时；
* ``tool_progress``：执行期间每 5 秒推一条耗时，直到工具结束。

## 边界（测试盯住的点）

* 事件顺序：start → ≥1 次 progress → action（结果）。
* 短工具不产生 progress（没必要刷屏，也不该有）。
* 回调是纯观测：它抛异常时**工具必须照常成功**，任务不受影响。
* 取消/超时后不能残留心跳 task（否则事件循环关闭时报 Task destroyed）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from automind.core.types import LLMResponse, PermissionTier, ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.permission_tier = PermissionTier.SAFE
        self.parameters = {"type": "object", "properties": {}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []}}


class _Registry:
    """dispatch 会先睡 ``delay`` 秒（模拟耗时工具），再返回成功结果。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    def list_names(self) -> list[str]:
        return ["slow_tool"]

    def list_all(self) -> list[_Tool]:
        return [_Tool("slow_tool")]

    def get(self, name: str) -> _Tool:
        return _Tool(name)

    async def dispatch(self, name: str, **kw: Any) -> ToolResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult(tool_name=name, success=True, output={"ok": True})


class _ScriptedLLM:
    """第一轮请求一次 slow_tool，之后收尾。"""

    def __init__(self, n_calls: int = 1) -> None:
        self.n = n_calls
        self.iterations = 0

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.iterations += 1
        if self.iterations <= self.n:
            return LLMResponse(
                text="调用工具",
                tool_calls=[ToolCall(id=str(self.iterations), name="slow_tool",
                                     arguments={"step": self.iterations})])
        return LLMResponse(text="完成")


def _executor(registry: _Registry, n_calls: int = 1) -> ReActExecutor:
    return ReActExecutor(_ScriptedLLM(n_calls=n_calls), registry, max_iterations=3)


# ═══════════════════════════════════════════════════════════
# 1. 正常路径：start → progress → action
# ═══════════════════════════════════════════════════════════


def test_event_order_and_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ReActExecutor, "TOOL_HEARTBEAT_INTERVAL", 0.03)
    events: list[tuple[str, dict]] = []

    async def on_start(ev: dict) -> None:
        events.append(("start", ev))

    async def on_progress(ev: dict) -> None:
        events.append(("progress", ev))

    async def on_action(tc, result) -> None:
        events.append(("action", {"success": result.success}))

    ex = _executor(_Registry(delay=0.15))
    asyncio.run(ex.run("干活", on_action_start=on_start,
                       on_action_progress=on_progress, on_action=on_action))

    kinds = [k for k, _ in events]
    assert kinds[0] == "start", f"第一个事件必须是 start：{kinds}"
    assert kinds[-1] == "action", f"最后一个事件必须是 action：{kinds}"
    assert "progress" in kinds, f"慢工具没有产生任何心跳：{kinds}"
    assert kinds.index("progress") < kinds.index("action")

    start_ev = events[0][1]
    assert start_ev["type"] == "step_action_start"
    assert start_ev["tool"] == "slow_tool"
    assert "timeout_s" in start_ev and isinstance(start_ev["args"], dict)

    progress_ev = next(ev for k, ev in events if k == "progress")
    assert progress_ev["type"] == "tool_progress"
    assert progress_ev["tool"] == "slow_tool"
    assert progress_ev["elapsed_s"] >= 0


def test_short_tool_produces_no_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """秒级工具不产生心跳 —— 否则每步都刷一条，进度反而被淹没。"""
    monkeypatch.setattr(ReActExecutor, "TOOL_HEARTBEAT_INTERVAL", 0.2)
    seen: list[str] = []

    async def on_progress(ev: dict) -> None:
        seen.append(ev["type"])

    ex = _executor(_Registry(delay=0.0))
    asyncio.run(ex.run("干活", on_action_progress=on_progress))

    assert seen == [], f"短工具不该有心跳：{seen}"
    assert ex.actions and ex.actions[0][1].success


# ═══════════════════════════════════════════════════════════
# 2. 回调是纯观测：异常不得影响工具
# ═══════════════════════════════════════════════════════════


def test_callback_errors_do_not_break_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ReActExecutor, "TOOL_HEARTBEAT_INTERVAL", 0.03)

    async def boom_start(ev: dict) -> None:
        raise RuntimeError("start 回调炸了")

    async def boom_progress(ev: dict) -> None:
        raise RuntimeError("心跳回调炸了")

    reg = _Registry(delay=0.12)
    ex = _executor(reg)
    text = asyncio.run(ex.run("干活", on_action_start=boom_start,
                              on_action_progress=boom_progress))

    assert reg.calls == 1
    assert ex.actions and ex.actions[0][1].success is True, (
        "回调异常把工具执行弄失败了")
    assert ex.stop_reason == "no_more_tools"
    assert "完成" in text


# ═══════════════════════════════════════════════════════════
# 3. 取消后不留 pending 心跳 task
# ═══════════════════════════════════════════════════════════


def test_cancel_leaves_no_heartbeat_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ReActExecutor, "TOOL_HEARTBEAT_INTERVAL", 0.02)
    started = asyncio.Event()

    async def on_start(ev: dict) -> None:
        started.set()

    async def _scenario():
        ex = _executor(_Registry(delay=5.0))
        task = asyncio.create_task(ex.run("长活", on_action_start=on_start))
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.08)          # 至少跳一次心跳
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)          # 给收尾留一点时间
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks()
                   if t is not current and not t.done()]
        return pending, ex

    pending, ex = asyncio.run(_scenario())
    assert not pending, f"取消后残留了未结束的任务：{pending}"
    assert ex.stop_reason == "cancelled"


# ═══════════════════════════════════════════════════════════
# 4. Agent 接线：回调 → 事件流
# ═══════════════════════════════════════════════════════════


def _bare_agent():
    """不跑重初始化的 Agent 实例（只带回调所需的最小属性）。"""
    from automind.agent import AutoMindAgent

    agent = object.__new__(AutoMindAgent)
    agent.event_sink = None
    agent._active_goal_id = None
    return agent


def test_agent_emits_step_action_start_and_tool_progress() -> None:
    agent = _bare_agent()
    events: list[dict] = []

    async def sink(ev: dict) -> None:
        events.append(ev)

    agent.event_sink = sink
    on_start, on_progress = agent._react_tool_callbacks()

    asyncio.run(on_start({"type": "step_action_start", "tool": "terminal",
                          "args": {"command": "pip install x"}, "timeout_s": 330.0}))
    asyncio.run(on_progress({"type": "tool_progress", "tool": "terminal",
                             "elapsed_s": 5.0}))

    assert [e["type"] for e in events] == ["step_action_start", "tool_progress"]
    start, progress = events
    assert start["tool"] == "terminal"
    assert start["args"]["command"] == "pip install x"
    assert start["timeout_s"] == 330.0
    assert progress["tool"] == "terminal"
    assert progress["elapsed_s"] == 5.0


def test_agent_summarizes_big_base64_args() -> None:
    """start 事件里的 base64 参数用占位符，别把一屏乱码推给前端。"""
    agent = _bare_agent()
    summarized = agent._summarize_args({
        "screenshot_base64": "A" * 2000,
        "command": "echo hi",
    })
    assert summarized["screenshot_base64"] == "<base64>"
    assert summarized["command"] == "echo hi"


def test_agent_keeps_the_legacy_two_callback_contract() -> None:
    """既有 2 元组契约不能被破坏（多处测试/调用按 2 个解包）。"""
    agent = _bare_agent()
    assert len(agent._react_callbacks()) == 2
    assert len(agent._react_tool_callbacks()) == 2
