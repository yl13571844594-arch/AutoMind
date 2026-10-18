"""子智能体委派运行时与工具 —— 用假父/子 Agent 验证生命周期，不依赖真实 LLM。

## 为什么用假 Agent

真实子智能体会调模型：跑一次要 API Key、几秒到几十秒、还按 token 计费。
而这个文件要验的恰恰是**编排**：并发上限、深度拒绝、事件顺序、追加/停止、
异常收敛、结果保留与长度预算 —— 它们全在运行时这一层，与模型说了什么无关。
所以这里让 ``FakeParent.clone_for_session()`` 返回 ``FakeChild``，由用例精确
控制子任务何时完成、何时报错、什么时候推一条进度。

## 这个文件盯住的关键结论

1. **派发立即返回**：start() 返回 agent_id 时子任务还没结束（父任务不能被
   "委派"变成同步等待）。子任务不结束，父任务就能继续拿到句柄做别的事。
2. **硬边界是拒绝而不是降级**：深度 > 0 / 并发已满时明确失败，并且消息里
   写清为什么、下一步怎么办 —— 静默截断成"再等等"是这里最坏的行为。
3. **终态必须发事件**：成功 / 失败 / 异常 / 取消四条路径都要有 ``subagent_done``，
   否则界面上的子智能体会永远停在"运行中"。
4. **收下 ≠ 生效**：send 的回执是 ``queued``，不能假装已并入子任务。
5. **结果保留 20 条、事件输出有上限**：不能因为委派把父上下文/WS 帧撑爆。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from automind.core.types import PermissionTier
from automind.planning import subagents
from automind.tools import subagents as tools_subagents
from automind.tools.subagents import (
    SUBAGENT_TOOL_CLASSES,
    SubagentListTool,
    SubagentSendTool,
    SubagentStopTool,
    SubagentTool,
)

# ═══════════════════════════════════════════════════════════
# 假 Agent / 测试辅助
# ═══════════════════════════════════════════════════════════


class FakeChild:
    """可控的子 Agent。

    ``gate`` 用于把子任务"挂住"（验证派发立即返回、追加/停止都发生在运行中）；
    ``events`` 会在阻塞前经 ``event_sink`` 推给管理器（验证进度事件）；
    ``exc`` 模拟子任务抛异常（验证异常收敛）。
    """

    def __init__(
        self,
        *,
        output: str = "子任务完成",
        success: bool = True,
        steps: int = 3,
        tokens: int = 120,
        error: str | None = None,
        exc: BaseException | None = None,
        events: tuple[dict[str, Any], ...] = (),
        gate: asyncio.Event | None = None,
        result: Any = None,
    ) -> None:
        self.output = output
        self.success = success
        self.steps = steps
        self.tokens = tokens
        self.error = error
        self.exc = exc
        self.events = list(events)
        self.gate = gate
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.interjections: list[str] = []
        self.interject_error: BaseException | None = None

    async def run_subtask(
        self,
        task: str,
        *,
        allowed_tools: list[str] | None = None,
        max_steps: int = 0,
        max_tokens: int = 0,
        event_sink: Any = None,
        approval_callback: Any = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "task": task,
            "allowed_tools": allowed_tools,
            "max_steps": max_steps,
            "max_tokens": max_tokens,
            "event_sink": event_sink,
            "approval_callback": approval_callback,
        })
        for payload in self.events:
            if event_sink is not None:
                await event_sink(payload)
        if self.gate is not None:
            await self.gate.wait()
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result
        return {"success": self.success, "output": self.output,
                "steps": self.steps, "tokens": self.tokens, "error": self.error}

    def interject(self, text: str) -> SimpleNamespace:
        if self.interject_error is not None:
            raise self.interject_error
        self.interjections.append(text)
        return SimpleNamespace(seq=len(self.interjections), text=text)


class FakeParent:
    """假父会话 Agent：只提供运行时真正会用的三样东西。

    ``clone_for_session()`` / ``event_sink`` 是管理器调用的公开面；
    ``approval_callback`` 用来验证"子任务继承父任务的审批通道"。
    """

    def __init__(self, child: FakeChild | None = None, *,
                 factory: Any = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.clones: list[FakeChild] = []
        self._child = child
        self._factory = factory
        self.approval_callback: Any = None
        self.event_sink: Any = self._sink

    async def _sink(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def clone_for_session(self) -> FakeChild:
        child = self._factory() if self._factory is not None else self._child
        if child is None:
            child = FakeChild()
        self.clones.append(child)
        return child


@pytest.fixture
async def make_manager():
    """管理器工厂：每个用例独立实例，teardown 时取消残留任务并清空状态。"""
    made: list[subagents.SubagentManager] = []

    def _make(**kwargs: Any) -> subagents.SubagentManager:
        manager = subagents.SubagentManager(**kwargs)
        made.append(manager)
        return manager

    yield _make
    for manager in made:
        await _settle(manager)


async def _settle(manager: subagents.SubagentManager) -> None:
    """用例结束时收尾：取消还没结束的子任务，避免任务泄漏到下一个用例。"""
    tasks = [record.task_obj for record in list(manager._index.values())
             if record.task_obj is not None and not record.task_obj.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    manager.reset_for_tests()


async def _tick() -> None:
    """让新建的后台任务真正跑起来（create_task 之后至少要让出一次控制权）。"""
    for _ in range(3):
        await asyncio.sleep(0)


async def _wait_terminal(manager: subagents.SubagentManager, agent_id: str, *,
                         session_id: str = "s-test", timeout: float = 2.0) -> dict[str, Any]:
    """等子智能体进入终态；超时仍 running 就失败（不掩盖挂住的任务）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snapshot = manager.get(agent_id, session_id=session_id)
        if snapshot is not None and snapshot["status"] != "running":
            return snapshot
        await asyncio.sleep(0.005)
    raise AssertionError(f"子智能体 {agent_id} 在 {timeout:.1f}s 内没有进入终态（仍在运行）")


def _of_type(parent: FakeParent, kind: str) -> list[dict[str, Any]]:
    return [event for event in parent.events if event.get("type") == kind]


def _bind_context(monkeypatch: pytest.MonkeyPatch, *, parent: Any,
                  session: str = "s-test", depth: int = 0) -> None:
    """把工具的会话上下文来源换成假父 Agent（不依赖 Lead 的接线是否已落地）。"""
    monkeypatch.setattr(subagents, "current_agent", lambda: parent)
    monkeypatch.setattr(subagents, "current_depth", lambda: depth)
    monkeypatch.setattr(subagents, "current_session_id", lambda: session)


# ═══════════════════════════════════════════════════════════
# 1. 派发：立即返回 + 后台执行 + 参数透传
# ═══════════════════════════════════════════════════════════


class TestStart:
    async def test_start_returns_immediately_and_records_state(self, make_manager):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "调研 A 目录的构建脚本",
                                       title="调研 A", session_id="s-test")

        # 立即返回：子任务还被 gate 挂住，但 id 已经可用了
        assert agent_id.startswith("sa")
        assert child.gate is not None
        assert len(parent.clones) == 1, "每个子智能体必须是独立的会话克隆"

        await _tick()
        listed = manager.list(session_id="s-test")
        assert [item["agent_id"] for item in listed] == [agent_id]
        assert listed[0]["status"] == "running"
        assert listed[0]["title"] == "调研 A"

        started = _of_type(parent, "subagent_started")
        assert len(started) == 1
        assert started[0]["agent_id"] == agent_id
        assert started[0]["status"] == "running"
        assert started[0]["session_id"] == "s-test", "事件必须带会话标识，界面才知道挂哪个标签页"
        assert started[0]["task"] == "调研 A 目录的构建脚本"

        gate.set()                                      # 释放子任务
        snapshot = await _wait_terminal(manager, agent_id)
        assert snapshot["status"] == "done"
        assert snapshot["output"] == "子任务完成"
        assert snapshot["steps"] == 3
        assert snapshot["tokens"] == 120

    async def test_child_receives_declared_limits_and_callbacks(self, make_manager):
        child = FakeChild()
        parent = FakeParent(child)
        parent.approval_callback = lambda *_args, **_kwargs: None
        manager = make_manager()

        agent_id = await manager.start(
            parent, "扫描 src 目录", session_id="s-test",
            allowed_tools=["file_read", "file_search"],
            max_steps=7, max_tokens=1234)
        await _wait_terminal(manager, agent_id)

        call = child.calls[0]
        assert call["task"] == "扫描 src 目录"
        assert call["allowed_tools"] == ["file_read", "file_search"]
        assert call["max_steps"] == 7
        assert call["max_tokens"] == 1234
        assert call["approval_callback"] is parent.approval_callback, \
            "子任务要沿用父会话的审批通道，否则需要审批的工具会 fail-closed"
        assert callable(call["event_sink"]), "进度回传依赖子任务实现调用这个回调"

    async def test_unspecified_limits_fall_back_to_bounded_defaults(self, make_manager):
        child = FakeChild()
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "默认限额", session_id="s-test")
        await _wait_terminal(manager, agent_id)

        call = child.calls[0]
        assert call["allowed_tools"] is None, "None = 只读工具（白名单在 run_subtask 内落地）"
        assert call["max_steps"] == subagents.DEFAULT_MAX_STEPS
        assert call["max_tokens"] == 0, "0 = 不单独设限，仍受父任务预算约束"

    async def test_auto_title_derived_from_task(self, make_manager):
        parent = FakeParent(FakeChild())
        manager = make_manager()

        agent_id = await manager.start(parent, "  把\n 所有测试跑一遍并汇总失败项 ", session_id="s-test")
        await _wait_terminal(manager, agent_id)

        title = manager.list(session_id="s-test")[0]["title"]
        assert title == subagents.derive_title("把 所有测试跑一遍并汇总失败项")
        assert "\n" not in title

    async def test_empty_task_is_rejected(self, make_manager):
        parent = FakeParent(FakeChild())
        manager = make_manager()

        with pytest.raises(subagents.SubagentError) as excinfo:
            await manager.start(parent, "   ", session_id="s-test")

        assert "task" in str(excinfo.value) and "不能为空" in str(excinfo.value)
        assert parent.clones == [], "参数非法时不该浪费一次父会话克隆"


# ═══════════════════════════════════════════════════════════
# 2. 事件：started → update → done，且终态四条路径都发 done
# ═══════════════════════════════════════════════════════════


class TestEvents:
    async def test_progress_update_carries_tail_steps_and_tokens(self, make_manager):
        child = FakeChild(events=({"output": "已读到 3 个文件", "steps": 2,
                                   "tokens": 88},))
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "读文件", session_id="s-test")
        await _wait_terminal(manager, agent_id)

        assert [event["type"] for event in parent.events] == [
            "subagent_started", "subagent_update", "subagent_done"], \
            "顺序必须是 started → update → done，否则界面会先收到进度再收到派发"

        update = _of_type(parent, "subagent_update")[0]
        assert update["agent_id"] == agent_id
        assert update["output_tail"] == "已读到 3 个文件"
        assert update["steps"] == 2, "进度里的步数要能反映运行中的值"
        assert update["tokens"] == 88
        assert update["status"] == "running"
        assert update["session_id"] == "s-test"

        done = _of_type(parent, "subagent_done")[0]
        assert done["status"] == "done"
        assert done["output"] == "子任务完成"
        assert done["steps"] == 3 and done["tokens"] == 120
        assert "error" in done and done["error"] is None

    async def test_progress_sink_tolerates_sync_calls(self, make_manager):
        """接线方少写一个 await 时，进度也不能被吞掉（事件与记录都该更新）。"""
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "同步回调", session_id="s-test")
        await _tick()

        sink = child.calls[0]["event_sink"]
        assert callable(sink)
        returned = sink({"output": "同步更新", "steps": 1})    # 故意不 await
        await _tick()

        listed = manager.list(session_id="s-test")[0]
        assert listed["output_tail"] == "同步更新", "同步调用也必须立即更新记录"
        assert listed["steps"] == 1
        assert _of_type(parent, "subagent_update"), "同步调用也必须推出进度事件"
        if inspect.isawaitable(returned):
            await returned
        gate.set()
        await _wait_terminal(manager, agent_id)

    async def test_done_event_output_is_length_capped(self, make_manager):
        child = FakeChild(output="x" * 10000)
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "超长输出", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        done = _of_type(parent, "subagent_done")[0]
        assert "已截断" in done["output"], "截断要标注，不能静默变短"
        assert len(done["output"]) < 10000
        assert snapshot["output"].startswith("x" * 100)
        assert "已截断" in snapshot["output"] or len(snapshot["output"]) <= subagents.OUTPUT_KEEP_CHARS

    async def test_task_patch_is_capped_in_started_event(self, make_manager):
        parent = FakeParent(FakeChild())
        manager = make_manager()

        agent_id = await manager.start(parent, "长" * 5000, session_id="s-test")
        await _wait_terminal(manager, agent_id)

        started = _of_type(parent, "subagent_started")[0]
        assert len(started["task"]) <= subagents.EVENT_TASK_CHARS + 64

    async def test_event_sink_failure_does_not_break_the_subagent(self, make_manager):
        parent = FakeParent(FakeChild(output="结果"))

        async def broken_sink(event: dict[str, Any]) -> None:
            raise RuntimeError("WebSocket 已断开")

        parent.event_sink = broken_sink
        manager = make_manager()

        agent_id = await manager.start(parent, "发事件会失败", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "done", "推送失败只能丢事件，不能丢子任务"
        assert snapshot["output"] == "结果"

    async def test_no_event_sink_still_runs(self, make_manager, monkeypatch):
        parent = FakeParent(FakeChild(output="安静地跑完"))
        parent.event_sink = None
        monkeypatch.setattr(subagents, "current_event_sink", lambda: None)
        manager = make_manager()

        agent_id = await manager.start(parent, "没有事件出口", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "done"
        assert parent.events == []

    async def test_explicit_emit_override_wins_over_parent_sink(self, make_manager):
        events: list[dict[str, Any]] = []

        async def sink(event: dict[str, Any]) -> None:
            events.append(event)

        parent = FakeParent(FakeChild())
        manager = make_manager(emit=sink)

        agent_id = await manager.start(parent, "服务端统一广播", session_id="s-test")
        await _wait_terminal(manager, agent_id)

        assert [event["type"] for event in events] == [
            "subagent_started", "subagent_done"]
        assert parent.events == [], "显式注入的出口优先，避免同一事件被推两遍"


# ═══════════════════════════════════════════════════════════
# 3. 硬边界：并发上限 / 嵌套深度 / 会话隔离
# ═══════════════════════════════════════════════════════════


class TestLimits:
    def test_contract_constants_match_the_declared_budget(self):
        assert subagents.MAX_CONCURRENT == 4
        assert subagents.MAX_DEPTH == 1
        assert subagents.KEEP_RESULTS == 20

    async def test_concurrency_is_capped_per_parent_session(self, make_manager):
        gate = asyncio.Event()
        parent = FakeParent(factory=lambda: FakeChild(gate=gate))
        manager = make_manager()

        ids = [await manager.start(parent, f"并行任务 {i}", session_id="s-test")
               for i in range(subagents.MAX_CONCURRENT)]

        with pytest.raises(subagents.SubagentLimitError) as excinfo:
            await manager.start(parent, "第五个", session_id="s-test")
        message = str(excinfo.value)
        assert "上限" in message and str(subagents.MAX_CONCURRENT) in message
        assert "subagent_stop" in message, "拒绝时要告诉模型怎么腾出位置"

        # 上限是**按父会话**的：另一个会话不受影响（并发资源各自记账）
        other = await manager.start(parent, "另一个会话的任务", session_id="s-other")
        assert other not in ids

        gate.set()
        for agent_id in ids:
            await _wait_terminal(manager, agent_id)
        await _wait_terminal(manager, other, session_id="s-other")

    async def test_nested_depth_is_rejected(self, make_manager, monkeypatch):
        monkeypatch.setattr(subagents, "current_depth", lambda: 1)
        parent = FakeParent(FakeChild())
        manager = make_manager()

        with pytest.raises(subagents.SubagentLimitError) as excinfo:
            await manager.start(parent, "子智能体内部的再次派发", session_id="s-test")

        message = str(excinfo.value)
        assert "深度" in message and "上限" in message
        assert parent.clones == [], "拒绝必须发生在克隆之前"

    async def test_sessions_are_isolated_for_list_get_send_stop(self, make_manager):
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager()
        first = await manager.start(parent, "会话一的子任务", session_id="s1")
        second = await manager.start(parent, "会话二的子任务", session_id="s2")
        await _wait_terminal(manager, first, session_id="s1")
        await _wait_terminal(manager, second, session_id="s2")

        assert [item["agent_id"] for item in manager.list(session_id="s1")] == [first]
        assert [item["agent_id"] for item in manager.list(session_id="s2")] == [second]
        assert manager.get(first, session_id="s1") is not None
        assert manager.get(first, session_id="s2") is None, "跨会话查询必须当不存在"

        sent = await manager.send(first, "跨会话补充", session_id="s2")
        assert sent["ok"] is False and first in sent["error"]
        stopped = await manager.stop(first, session_id="s2")
        assert stopped["ok"] is False and first in stopped["error"]

    async def test_list_is_newest_first(self, make_manager):
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager()
        first = await manager.start(parent, "先派发", session_id="s-test")
        await _wait_terminal(manager, first)
        second = await manager.start(parent, "后派发", session_id="s-test")
        await _wait_terminal(manager, second)

        ids = [item["agent_id"] for item in manager.list(session_id="s-test")]
        assert ids == [second, first]

    async def test_cancel_session_stops_all_running_agents(self, make_manager):
        """父任务结束/会话关闭时要能一次收尾，否则孤儿子任务继续烧 token。"""
        gate = asyncio.Event()
        parent = FakeParent(factory=lambda: FakeChild(gate=gate))
        manager = make_manager()
        ids = [await manager.start(parent, f"并行任务 {index}", session_id="s-test")
               for index in range(2)]
        await _tick()

        assert await manager.cancel_session(session_id="s-test") == 2

        for agent_id in ids:
            assert manager.get(agent_id, session_id="s-test")["status"] == "cancelled"
        dones = _of_type(parent, "subagent_done")
        assert len(dones) == 2 and all(item["status"] == "cancelled" for item in dones)
        assert await manager.cancel_session(session_id="s-empty") == 0


# ═══════════════════════════════════════════════════════════
# 4. 追加消息 / 停止
# ═══════════════════════════════════════════════════════════


class TestSendAndStop:
    async def test_send_queues_message_into_the_child(self, make_manager):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "先做一半", session_id="s-test")
        await _tick()

        result = await manager.send(agent_id, "  只看 src 目录  ", session_id="s-test")

        assert result["ok"] is True
        assert result["queued"] is True, "收下 ≠ 生效：回执只能承诺排队"
        assert "收下" in result["note"] or "排队" in result["note"]
        assert child.interjections == ["只看 src 目录"], "首尾空白应清掉"

        gate.set()
        await _wait_terminal(manager, agent_id)

    async def test_send_unknown_id_is_actionable(self, make_manager):
        manager = make_manager()

        result = await manager.send("sa999", "补充", session_id="s-test")

        assert result["ok"] is False
        assert "sa999" in result["error"]
        assert "没有找到" in result["error"]

    async def test_send_after_finish_fails_clearly(self, make_manager):
        parent = FakeParent(FakeChild())
        manager = make_manager()
        agent_id = await manager.start(parent, "很快结束", session_id="s-test")
        await _wait_terminal(manager, agent_id)

        result = await manager.send(agent_id, "补充", session_id="s-test")

        assert result["ok"] is False
        assert "已经结束" in result["error"]
        assert "subagent" in result["error"], "要告诉模型改用派发新任务，而不是反复重试"

    async def test_send_rejection_from_child_is_reported(self, make_manager):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "运行中", session_id="s-test")
        await _tick()
        child.interject_error = ValueError("补充内容太长（5000 字符，上限 4000）")

        result = await manager.send(agent_id, "x" * 5000, session_id="s-test")

        assert result["ok"] is False
        assert "太长" in result["error"], "子 Agent 的拒绝原因要原样回给调用方"
        gate.set()
        await _wait_terminal(manager, agent_id)

    async def test_stop_cancels_and_emits_cancelled_done(self, make_manager):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "停掉我", session_id="s-test")
        await _tick()

        result = await manager.stop(agent_id, session_id="s-test")

        assert result["ok"] is True
        assert result["status"] == "cancelled"
        assert "stop_requested" in result
        done = _of_type(parent, "subagent_done")
        assert done and done[-1]["status"] == "cancelled", \
            "取消路径也必须发终态事件，否则界面永远停在运行中"
        assert manager.list(session_id="s-test")[0]["status"] == "cancelled"

        again = await manager.stop(agent_id, session_id="s-test")
        assert again["ok"] is True and again.get("already_finished") is True

    async def test_stop_immediately_after_start_still_settles(self, make_manager):
        """取消可能落在子任务协程体开始之前 —— 记录不能永远停在 running。"""
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "刚启动就停", session_id="s-test")
        # 故意不 _tick：这里取消的是"还没轮到执行"的后台任务
        result = await manager.stop(agent_id, session_id="s-test")

        assert result["ok"] is True
        assert result["status"] == "cancelled"
        assert manager.get(agent_id, session_id="s-test")["status"] == "cancelled"
        assert _of_type(parent, "subagent_done")[-1]["status"] == "cancelled"

    async def test_stop_unknown_id_is_actionable(self, make_manager):
        manager = make_manager()

        result = await manager.stop("sa999", session_id="s-test")

        assert result["ok"] is False
        assert "sa999" in result["error"]


# ═══════════════════════════════════════════════════════════
# 5. 异常收敛：失败/异常都不能拖垮父任务或运行时
# ═══════════════════════════════════════════════════════════


class TestFailureConvergence:
    async def test_child_exception_becomes_error_status(self, make_manager):
        children = [FakeChild(exc=RuntimeError("模型连接失败")), FakeChild(output="正常")]
        parent = FakeParent(factory=lambda: children.pop(0))
        manager = make_manager()

        agent_id = await manager.start(parent, "会炸的任务", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "error"
        assert "RuntimeError" in snapshot["error"] and "模型连接失败" in snapshot["error"]
        done = _of_type(parent, "subagent_done")[0]
        assert done["status"] == "error"
        assert "模型连接失败" in str(done["error"])

        # 一个子任务失败不能让运行时失去工作能力
        second = await manager.start(parent, "下一个任务", session_id="s-test")
        second_snapshot = await _wait_terminal(manager, second)
        assert second_snapshot["status"] == "done"

    async def test_returned_failure_is_not_mistaken_for_success(self, make_manager):
        child = FakeChild(success=False, error="缺少 API Key", output="")
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "会失败", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "failed"
        assert snapshot["error"] == "缺少 API Key"
        assert _of_type(parent, "subagent_done")[0]["status"] == "failed"

    async def test_non_dict_result_is_reported_as_contract_violation(self, make_manager):
        child = FakeChild(result="我随便返回一句")
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "契约不符", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "failed"
        assert "dict" in snapshot["error"] and "契约" in snapshot["error"]

    async def test_failure_without_message_still_explains_itself(self, make_manager):
        child = FakeChild(success=False, error=None)
        parent = FakeParent(child)
        manager = make_manager()

        agent_id = await manager.start(parent, "失败但没原因", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "failed"
        assert snapshot["error"], "没有原因也要给一句可读的说明，不能留空"


# ═══════════════════════════════════════════════════════════
# 6. 结果保留
# ═══════════════════════════════════════════════════════════


class TestRetention:
    async def test_only_recent_results_are_kept(self, make_manager):
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager(max_results=3)

        ids: list[str] = []
        for index in range(5):
            agent_id = await manager.start(parent, f"任务 {index}", session_id="s-test")
            await _wait_terminal(manager, agent_id)
            ids.append(agent_id)

        kept = [item["agent_id"] for item in manager.list(session_id="s-test")]
        assert len(kept) == 3
        assert set(kept) == set(ids[-3:]), "保留的必须是最新的结果"
        assert manager.get(ids[0], session_id="s-test") is None
        assert manager.get(ids[-1], session_id="s-test") is not None

    async def test_running_records_are_never_pruned(self, make_manager):
        long_gate = asyncio.Event()
        handed_out: list[bool] = []

        def factory() -> FakeChild:
            # 第一个子任务长期挂住（不被保留上限影响），其余快速结束
            if not handed_out:
                handed_out.append(True)
                return FakeChild(gate=long_gate)
            return FakeChild()

        parent = FakeParent(factory=factory)
        manager = make_manager(max_results=1)

        running = await manager.start(parent, "长期运行", session_id="s-test")
        await _tick()
        for index in range(3):
            # 已结束的记录只能保留 1 条，但运行中的那条必须一直在
            done = await manager.start(parent, f"快速任务 {index}", session_id="s-test")
            await _wait_terminal(manager, done)

        kept = [item["agent_id"] for item in manager.list(session_id="s-test")]
        assert running in kept, "运行中的子智能体不能被结果保留上限清理"
        assert manager.get(running, session_id="s-test")["status"] == "running"

    def test_reset_for_tests_creates_a_fresh_default_manager(self):
        first = subagents.default_manager()
        try:
            subagents.reset_for_tests()
            assert subagents.default_manager() is not first
        finally:
            subagents.reset_for_tests()


# ═══════════════════════════════════════════════════════════
# 7. 工具层：名称/权限/参数校验与上下文桥接
# ═══════════════════════════════════════════════════════════


class TestToolContract:
    def test_tool_names_tiers_and_required_params(self):
        by_name = {cls.name: cls for cls in SUBAGENT_TOOL_CLASSES}
        assert set(by_name) == {"subagent", "subagent_list",
                                "subagent_send", "subagent_stop"}
        for cls in SUBAGENT_TOOL_CLASSES:
            assert cls.permission_tier is PermissionTier.SAFE
        assert by_name["subagent"].parameters["required"] == ["task"]
        assert by_name["subagent_send"].parameters["required"] == ["agent_id", "message"]
        assert by_name["subagent_stop"].parameters["required"] == ["agent_id"]
        assert by_name["subagent_list"].parameters["required"] == []

    async def test_factory_builds_four_tools_sharing_one_manager(self, make_manager):
        manager = make_manager()
        built = tools_subagents.subagent_tools(manager)

        assert [tool.name for tool in built] == [
            "subagent", "subagent_list", "subagent_send", "subagent_stop"]
        assert all(tool._manager is manager for tool in built)

class TestSubagentTool:
    async def test_spawns_via_context_agent(self, make_manager, monkeypatch):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        tool = SubagentTool(manager)

        result = await tool.execute(task="统计 tests 目录的用例数",
                                    allowed_tools=["file_read"], max_steps=5)

        assert result.success is True
        agent_id = result.output["agent_id"]
        assert agent_id.startswith("sa")
        assert result.output["status"] == "running"
        assert "subagent_list" in result.output["note"], "回执要告诉模型下一步怎么取结果"

        await _tick()
        call = child.calls[0]
        assert call["allowed_tools"] == ["file_read"]
        assert call["max_steps"] == 5
        assert call["max_tokens"] == 0

        gate.set()
        await _wait_terminal(manager, agent_id)

    async def test_derives_title_when_omitted(self, make_manager, monkeypatch):
        children: list[FakeChild] = []

        def factory() -> FakeChild:
            child = FakeChild()
            children.append(child)
            return child

        parent = FakeParent(factory=factory)
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        tool = SubagentTool(manager)

        result = await tool.execute(task="  把\n 所有测试跑一遍并汇总失败项 ")
        await _wait_terminal(manager, result.output["agent_id"])

        assert result.output["title"] == subagents.derive_title("把 所有测试跑一遍并汇总失败项")

    async def test_without_agent_returns_actionable_failure(self, make_manager, monkeypatch):
        monkeypatch.setattr(subagents, "current_agent", lambda: None)
        manager = make_manager()

        result = await SubagentTool(manager).execute(task="没有父会话")

        assert result.success is False
        assert "任务" in result.error and "Agent" in result.error
        assert manager._index == {}, "没有父 Agent 时绝不能凭空造一个子任务"

    async def test_nested_depth_is_refused_at_the_tool_layer(self, make_manager, monkeypatch):
        _bind_context(monkeypatch, parent=FakeParent(FakeChild()), depth=1)
        manager = make_manager()

        result = await SubagentTool(manager).execute(task="子智能体内部再派发")

        assert result.success is False
        assert "深度" in result.error and "上限" in result.error
        assert manager._index == {}

    @pytest.mark.parametrize("kwargs, marker", [
        ({"task": "   "}, "task 不能为空"),
        ({"task": "x", "allowed_tools": []}, "空列表"),
        ({"task": "x", "allowed_tools": 123}, "allowed_tools"),
        ({"task": "x", "max_steps": 0}, "max_steps"),
        ({"task": "x", "max_steps": 1000}, "max_steps"),
        ({"task": "x", "max_tokens": -1}, "max_tokens"),
    ])
    async def test_invalid_params_are_rejected_with_guidance(
            self, make_manager, monkeypatch, kwargs, marker):
        parent = FakeParent(FakeChild())
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()

        result = await SubagentTool(manager).execute(**kwargs)

        assert result.success is False
        assert marker in result.error
        assert manager._index == {}
        assert parent.clones == [], "参数校验必须先于克隆，避免白花一次 LLM 预算"


class TestListSendStopTools:
    async def test_list_returns_results_of_finished_agents(self, make_manager, monkeypatch):
        child = FakeChild(output="结论：共 37 个用例，2 个失败")
        parent = FakeParent(child)
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        spawn = await SubagentTool(manager).execute(task="数一下用例")
        await _wait_terminal(manager, spawn.output["agent_id"])

        result = await SubagentListTool(manager).execute()

        assert result.success is True
        payload = result.output
        assert payload["count"] == 1 and payload["running"] == 0
        assert payload["results"][spawn.output["agent_id"]] == "结论：共 37 个用例，2 个失败"
        assert "已结束" in payload["note"]

    async def test_list_shows_live_tail_of_running_agents(self, make_manager, monkeypatch):
        gate = asyncio.Event()
        child = FakeChild(gate=gate, events=({"output": "已扫描到 3/10 个文件",
                                              "steps": 2},))
        parent = FakeParent(child)
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        spawn = await SubagentTool(manager).execute(task="扫描目录")
        await _tick()
        await _tick()                       # 让子任务把进度推给管理器

        result = await SubagentListTool(manager).execute()

        payload = result.output
        assert payload["running"] == 1
        assert payload["agents"][0]["output_tail"] == "已扫描到 3/10 个文件"
        assert payload["agents"][0]["steps"] == 2
        assert "subagent_send" in payload["note"]
        assert "results" not in payload, "运行中的子智能体没有最终结果可回传"

        gate.set()
        await _wait_terminal(manager, spawn.output["agent_id"])

    async def test_list_of_empty_session_explains_how_to_start(self, make_manager, monkeypatch):
        _bind_context(monkeypatch, parent=FakeParent(FakeChild()))
        result = await SubagentListTool(make_manager()).execute()

        assert result.success is True
        assert result.output["count"] == 0
        assert "还没有派发" in result.output["note"]

    async def test_send_and_stop_tools_route_to_the_manager(self, make_manager, monkeypatch):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        spawn = await SubagentTool(manager).execute(task="先跑着")
        agent_id = spawn.output["agent_id"]
        await _tick()

        sent = await SubagentSendTool(manager).execute(
            agent_id=agent_id, message="顺便把编码改成 utf-8")
        assert sent.success is True
        assert sent.output["queued"] is True
        assert child.interjections == ["顺便把编码改成 utf-8"]

        stopped = await SubagentStopTool(manager).execute(agent_id=agent_id)
        assert stopped.success is True
        assert stopped.output["status"] == "cancelled"
        done = _of_type(parent, "subagent_done")
        assert done and done[-1]["status"] == "cancelled"

    async def test_send_and_stop_require_agent_id(self, make_manager, monkeypatch):
        _bind_context(monkeypatch, parent=FakeParent(FakeChild()))
        manager = make_manager()

        sent = await SubagentSendTool(manager).execute(agent_id="  ", message="补充")
        stopped = await SubagentStopTool(manager).execute(agent_id="  ")

        assert sent.success is False and "agent_id" in sent.error
        assert stopped.success is False and "agent_id" in stopped.error

    async def test_send_empty_message_is_rejected_locally(self, make_manager, monkeypatch):
        gate = asyncio.Event()
        parent = FakeParent(FakeChild(gate=gate))
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        spawn = await SubagentTool(manager).execute(task="运行中")
        await _tick()

        result = await SubagentSendTool(manager).execute(
            agent_id=spawn.output["agent_id"], message="   ")

        assert result.success is False
        assert "message" in result.error
        channel = manager.get_record(spawn.output["agent_id"], session_id="s-test")
        assert channel is not None and channel.child.interjections == []
        gate.set()
        await _wait_terminal(manager, spawn.output["agent_id"])
