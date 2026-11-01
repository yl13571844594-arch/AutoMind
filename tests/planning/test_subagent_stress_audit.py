"""v1.11.23 审计：子智能体委派（subagents）压力与生命周期。

## 这个文件盯什么

``tests/planning/test_subagents.py`` 已经把**功能正确性**覆盖得比较全（事件顺序、
单次派发、参数透传、异常收敛）。这一份是**压力/审计**视角的补充，专门回答四个
上线前必须确认的问题：

1. **并发上限是硬拒绝，不是降级**：MAX_CONCURRENT=4，第 5 个必须明确拒绝；
   子任务结束**必须真的释放槽位**（否则长会话跑几次就再也派发不出去）。
2. **长期运行不增长**：连续 50 次派发后，结果只保留最近 KEEP_RESULTS=20 条、
   全局索引与每会话表都不得无限增长，旧结果要能被"淘汰"（get 不到、错误可读）。
3. **生命周期闭环**：list / get（点名单条）/ send / stop 全链路；stop 还没完成时
   **如实说"未完成"**，不能谎称已停止；终态之后不得残留 running 记录或后台任务。
4. **子任务预算与输出**：max_steps/max_tokens 的默认值与上限、超长输出在记录 /
   详情 / 事件三处都被截断、预算耗尽记为可重派、事件 sink 的失败/同步调用/缺失
   都不影响子任务与父任务。

全部用 fake parent/clone/child/manager，不调真实模型、不联网；并断言每个场景的
墙钟上限（脚本化场景 < 5 秒），避免"测试绿是因为它根本跑得慢/挂住了"。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from automind.planning import subagents
from automind.tools.subagents import (
    RESULT_TOTAL_CHARS,
    SubagentListTool,
    SubagentSendTool,
    SubagentStopTool,
    SubagentTool,
)

# ═══════════════════════════════════════════════════════════
# 假 Agent（与现有测试同构，但集中在本文件，便于压力场景定制）
# ═══════════════════════════════════════════════════════════


class FakeChild:
    """可控子 Agent。

    - ``gate``：把子任务挂住，用于验证"运行中"的各种操作；
    - ``stubborn``：**吞掉一次取消**，模拟"取消要到下一个可中断点才生效"，
      用于验证 stop() 超时后必须如实说"还没结束"；
    - ``events``：在阻塞前经 event_sink 推进度。
    """

    def __init__(
        self,
        *,
        output: str = "子任务完成",
        success: bool = True,
        steps: int = 3,
        tokens: int = 120,
        error: str | None = None,
        result: Any = None,
        events: tuple[dict[str, Any], ...] = (),
        gate: asyncio.Event | None = None,
        stubborn: bool = False,
        release: asyncio.Event | None = None,
    ) -> None:
        self.output = output
        self.success = success
        self.steps = steps
        self.tokens = tokens
        self.error = error
        self.result = result
        self.events = list(events)
        self.gate = gate
        self.stubborn = stubborn
        self.release = release
        self.calls: list[dict[str, Any]] = []
        self.interjections: list[str] = []
        self.started = asyncio.Event()

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
        self.started.set()
        for payload in self.events:
            if event_sink is not None:
                await event_sink(payload)
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.stubborn or self.release is None:
                    raise
                # 模拟"收尾过程不可打断"：吞掉这次取消，等用例放行后正常返回
                await self.release.wait()
        if self.result is not None:
            return self.result
        return {"success": self.success, "output": self.output,
                "steps": self.steps, "tokens": self.tokens, "error": self.error}

    def interject(self, text: str) -> SimpleNamespace:
        self.interjections.append(text)
        return SimpleNamespace(seq=len(self.interjections), text=text)


class FakeParent:
    def __init__(self, child: FakeChild | None = None, *, factory: Any = None) -> None:
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


# ═══════════════════════════════════════════════════════════
# 测试基础设施
# ═══════════════════════════════════════════════════════════


@pytest.fixture
async def make_manager():
    made: list[subagents.SubagentManager] = []

    def _make(**kwargs: Any) -> subagents.SubagentManager:
        manager = subagents.SubagentManager(**kwargs)
        made.append(manager)
        return manager

    yield _make
    for manager in made:
        await _settle(manager)


async def _settle(manager: subagents.SubagentManager) -> None:
    """收尾：取消残留任务并清空状态；对顽固任务也给有界等待，避免测试挂死。"""
    tasks = [record.task_obj for record in list(manager._index.values())
             if record.task_obj is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        pending = {task for task in tasks if not task.done()}
        if pending:
            await asyncio.wait(pending, timeout=2.0)
    manager.reset_for_tests()


async def _tick(times: int = 3) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _await_task(manager: subagents.SubagentManager, agent_id: str,
                      *, session_id: str = "s-test", timeout: float = 2.0) -> None:
    """直接等记录背后的 asyncio.Task 结束（不轮询，压力循环里更快更稳）。"""
    record = manager.get_record(agent_id, session_id=session_id)
    assert record is not None, f"记录不存在：{agent_id}"
    assert record.task_obj is not None, f"记录没有后台任务：{agent_id}"
    done, pending = await asyncio.wait({record.task_obj}, timeout=timeout)
    assert not pending, f"子任务 {agent_id} 在 {timeout}s 内没有结束（挂住了）"
    del done


async def _wait_terminal(manager: subagents.SubagentManager, agent_id: str, *,
                         session_id: str = "s-test",
                         timeout: float = 2.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snapshot = manager.get(agent_id, session_id=session_id)
        if snapshot is not None and snapshot["status"] != "running":
            return snapshot
        await asyncio.sleep(0.002)
    raise AssertionError(f"子智能体 {agent_id} 在 {timeout:.1f}s 内没有进入终态")


def _of_type(parent: FakeParent, kind: str) -> list[dict[str, Any]]:
    return [event for event in parent.events if event.get("type") == kind]


def _bind_context(monkeypatch: pytest.MonkeyPatch, *, parent: Any,
                  session: str = "s-test", depth: int = 0) -> None:
    monkeypatch.setattr(subagents, "current_agent", lambda: parent)
    monkeypatch.setattr(subagents, "current_depth", lambda: depth)
    monkeypatch.setattr(subagents, "current_session_id", lambda: session)
    monkeypatch.setattr(subagents, "current_event_sink", lambda: None)


async def _assert_settled(manager: subagents.SubagentManager, *,
                          session_id: str = "s-test") -> None:
    """断言不存在 running 泄漏 / 悬挂后台任务。

    终态记录是先置状态、再发 done 事件、最后任务才结束 —— 因此这里给一个很短的
    收尾窗口（最多 1 秒）再判，避免把"事件正在发送中"误判成泄漏；超过窗口仍不
    收敛才是真的漏。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while loop.time() < deadline:
        records = manager._by_session.get(session_id) or {}
        leaked = [r.agent_id for r in records.values() if r.running]
        pending = [r.agent_id for r in manager._index.values()
                   if r.task_obj is not None and not r.task_obj.done()]
        if not leaked and not pending:
            return
        await asyncio.sleep(0.002)
    records = manager._by_session.get(session_id) or {}
    leaked = [r.agent_id for r in records.values() if r.running]
    assert not leaked, f"仍有记录停在 running（泄漏）：{leaked}"
    pending = [r.agent_id for r in manager._index.values()
               if r.task_obj is not None and not r.task_obj.done()]
    assert not pending, f"仍有后台任务没结束：{pending}"


def _running_count(manager: subagents.SubagentManager,
                    session_id: str = "s-test") -> int:
    return sum(1 for r in (manager._by_session.get(session_id) or {}).values()
               if r.running)


# ═══════════════════════════════════════════════════════════
# 1. 并发上限：4 个并发 / 第 5 个拒绝 / 完成释放
# ═══════════════════════════════════════════════════════════


class TestConcurrencyPressure:
    async def test_five_dispatches_are_capped_at_four_then_released(
            self, make_manager):
        started = time.perf_counter()
        gate = asyncio.Event()
        parent = FakeParent(factory=lambda: FakeChild(gate=gate))
        manager = make_manager()

        ids = [await manager.start(parent, f"并行任务 {i}", session_id="s-test")
               for i in range(subagents.MAX_CONCURRENT)]
        assert len(ids) == 4
        assert _running_count(manager) == 4

        with pytest.raises(subagents.SubagentLimitError) as excinfo:
            await manager.start(parent, "第五个", session_id="s-test")
        message = str(excinfo.value)
        assert "上限" in message and str(subagents.MAX_CONCURRENT) in message
        assert "subagent_stop" in message, "拒绝时要给模型可执行的腾位方法"
        assert _running_count(manager) == 4, "拒绝不能悄悄把第 5 个挂起"
        assert len(parent.clones) == 4, "被拒绝的派发不该克隆子 Agent（白花预算）"

        gate.set()
        for agent_id in ids:
            await _await_task(manager, agent_id)
        assert _running_count(manager) == 0, "完成之后槽位没有释放"
        await _assert_settled(manager)

        # 释放后必须能继续派发（否则长会话跑几次就永久锁死）
        fifth = await manager.start(parent, "释放后的第五个", session_id="s-test")
        await _await_task(manager, fifth)
        assert manager.get(fifth, session_id="s-test")["status"] == "done"
        await _assert_settled(manager)
        assert time.perf_counter() - started < 5, "脚本化并发场景不该超过 5 秒"

    async def test_concurrent_starts_never_exceed_the_limit(self, make_manager):
        """并发调用 start() 也不能突破上限（记录必须先落表再做异步事件）。"""
        gate = asyncio.Event()
        parent = FakeParent(factory=lambda: FakeChild(gate=gate))
        manager = make_manager()

        results = await asyncio.gather(
            *[manager.start(parent, f"并发派发 {i}", session_id="s-test")
              for i in range(8)],
            return_exceptions=True)
        ok = [r for r in results if isinstance(r, str)]
        rejected = [r for r in results if isinstance(r, subagents.SubagentLimitError)]
        assert len(ok) == subagents.MAX_CONCURRENT, f"实际放行 {len(ok)} 个"
        assert len(rejected) == 8 - subagents.MAX_CONCURRENT, "超限的必须拒绝而不是排队"
        assert _running_count(manager) == subagents.MAX_CONCURRENT
        assert len(parent.clones) == subagents.MAX_CONCURRENT

        gate.set()
        for agent_id in ok:
            await _await_task(manager, agent_id)
        await _assert_settled(manager)

    async def test_tool_layer_rejects_fifth_with_actionable_error(
            self, make_manager, monkeypatch):
        gate = asyncio.Event()
        parent = FakeParent(factory=lambda: FakeChild(gate=gate))
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        tool = SubagentTool(manager)

        for i in range(subagents.MAX_CONCURRENT):
            result = await tool.execute(task=f"任务 {i}")
            assert result.success is True

        fifth = await tool.execute(task="第五个")
        assert fifth.success is False, "第 5 个在工具层必须返回失败而不是抛异常"
        assert "上限" in fifth.error and "subagent_stop" in fifth.error
        assert len(parent.clones) == subagents.MAX_CONCURRENT

        gate.set()
        for record in list(manager._index.values()):
            await _await_task(manager, record.agent_id)
        await _assert_settled(manager)


# ═══════════════════════════════════════════════════════════
# 2. 嵌套深度：1 层拒绝
# ═══════════════════════════════════════════════════════════


class TestDepthGuard:
    async def test_manager_rejects_depth_one_before_cloning(self, make_manager):
        parent = FakeParent(FakeChild())
        manager = make_manager(depth_getter=lambda: 1)

        with pytest.raises(subagents.SubagentLimitError) as excinfo:
            await manager.start(parent, "子智能体内部的再次派发", session_id="s-test")
        assert "深度" in str(excinfo.value) and "上限" in str(excinfo.value)
        assert parent.clones == []
        assert manager._index == {}, "被深度拒绝的派发不能留下任何记录"

    async def test_tool_layer_rejects_depth_one(self, make_manager, monkeypatch):
        _bind_context(monkeypatch, parent=FakeParent(FakeChild()), depth=1)
        manager = make_manager()
        result = await SubagentTool(manager).execute(task="子智能体内部再派发")
        assert result.success is False
        assert "深度" in result.error and "上限" in result.error
        assert manager._index == {}

    async def test_depth_zero_is_allowed(self, make_manager):
        parent = FakeParent(FakeChild())
        manager = make_manager(depth_getter=lambda: 0)
        agent_id = await manager.start(parent, "顶层派发", session_id="s-test")
        await _await_task(manager, agent_id)
        assert manager.get(agent_id, session_id="s-test")["status"] == "done"
        await _assert_settled(manager)


# ═══════════════════════════════════════════════════════════
# 3. 规模压力：50 次派发 / 保留 20 / 淘汰旧结果 / 无无限增长
# ═══════════════════════════════════════════════════════════


class TestRetentionStress:
    async def test_fifty_sequential_dispatches_keep_only_twenty(
            self, make_manager):
        started = time.perf_counter()
        parent = FakeParent(factory=lambda: FakeChild(output="结果"))
        manager = make_manager()
        ids: list[str] = []

        for index in range(50):
            agent_id = await manager.start(parent, f"任务 {index}", session_id="s-test")
            await _await_task(manager, agent_id)
            ids.append(agent_id)

        items = manager.list(session_id="s-test")
        assert len(items) == subagents.KEEP_RESULTS == 20, (
            f"结果保留应为 20 条，实际 {len(items)}")
        kept = [item["agent_id"] for item in items]
        assert kept == list(reversed(ids[-20:])), "保留的必须是最新 20 条（且最新在前）"
        assert all(item["status"] == "done" for item in items)
        await _assert_settled(manager)

        # 被淘汰的旧结果必须"查不到"，而且 get 的缺失是可解释的（不是异常）
        for old_id in ids[:30]:
            assert manager.get(old_id, session_id="s-test") is None
        assert manager.get(ids[-1], session_id="s-test") is not None

        # 无无限增长：每会话表与全局索引都封顶在 20
        assert len(manager._by_session["s-test"]) == 20
        assert len(manager._index) == 20, f"全局索引增长到 {len(manager._index)} 条"
        assert time.perf_counter() - started < 5, (
            f"50 次顺序派发耗时 {time.perf_counter() - started:.2f}s，超出脚本化上限")

    async def test_evicted_ids_fail_with_actionable_errors(
            self, make_manager, monkeypatch):
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager(max_results=3)
        _bind_context(monkeypatch, parent=parent)
        ids: list[str] = []
        for index in range(5):
            agent_id = await manager.start(parent, f"任务 {index}", session_id="s-test")
            await _await_task(manager, agent_id)
            ids.append(agent_id)

        old = ids[0]
        assert manager.get(old, session_id="s-test") is None
        assert (await manager.send(old, "补充", session_id="s-test"))["ok"] is False
        assert (await manager.stop(old, session_id="s-test"))["ok"] is False

        listing = await SubagentListTool(manager).execute(agent_id=old)
        assert listing.success is False and old in listing.error
        assert "保留上限" in listing.error or "没有找到" in listing.error
        await _assert_settled(manager)

    async def test_fifty_dispatches_in_parallel_batches(self, make_manager):
        """贴近真实用法的规模压测：成批派发（每批 ≤4 并发）共 50 个。"""
        started = time.perf_counter()
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager()
        all_ids: list[str] = []
        remaining = 50
        while remaining:
            n = min(subagents.MAX_CONCURRENT, remaining)
            batch = [await manager.start(parent, f"批量任务 {remaining}-{i}",
                                         session_id="s-test")
                     for i in range(n)]
            for agent_id in batch:
                await _await_task(manager, agent_id)
            all_ids += batch
            remaining -= n

        assert len(all_ids) == 50
        assert len(set(all_ids)) == 50, "agent_id 必须唯一"
        assert len(manager.list(session_id="s-test")) == subagents.KEEP_RESULTS == 20
        assert len(manager._index) == subagents.KEEP_RESULTS
        assert _running_count(manager) == 0, "批量跑完后仍有 running 泄漏"
        await _assert_settled(manager)
        elapsed = time.perf_counter() - started
        assert elapsed < 5, f"50 个批量派发耗时 {elapsed:.2f}s，超出脚本化上限"

    async def test_performance_is_linear_and_bounded(self, make_manager):
        """再跑 20 次只应叠加线性成本 —— 每次派发不随历史规模变慢（粗略上界）。"""
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager()
        for index in range(20):
            agent_id = await manager.start(parent, f"预热 {index}", session_id="s-test")
            await _await_task(manager, agent_id)

        started = time.perf_counter()
        for index in range(subagents.KEEP_RESULTS):
            agent_id = await manager.start(parent, f"追加 {index}", session_id="s-test")
            await _await_task(manager, agent_id)
        elapsed = time.perf_counter() - started
        assert elapsed < 3, f"20 次派发耗时 {elapsed:.2f}s，疑似随历史规模退化"
        assert len(manager.list(session_id="s-test")) == subagents.KEEP_RESULTS


# ═══════════════════════════════════════════════════════════
# 4. list / get / send / stop 生命周期（工具层全链路）
# ═══════════════════════════════════════════════════════════


class TestToolLifecycle:
    async def test_spawn_list_send_detail_stop_roundtrip(
            self, make_manager, monkeypatch):
        gate = asyncio.Event()
        child = FakeChild(gate=gate, events=({"output": "已扫描 3/10", "steps": 2,
                                             "tokens": 88},))
        parent = FakeParent(child)
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        spawn_tool = SubagentTool(manager)
        list_tool = SubagentListTool(manager)
        send_tool = SubagentSendTool(manager)
        stop_tool = SubagentStopTool(manager)

        spawned = await spawn_tool.execute(task="扫描 src", max_steps=5)
        assert spawned.success is True
        agent_id = spawned.output["agent_id"]

        await _tick()
        listed = await list_tool.execute()
        assert listed.output["count"] == 1 and listed.output["running"] == 1
        item = listed.output["agents"][0]
        assert item["agent_id"] == agent_id and item["status"] == "running"
        assert item["output_tail"] == "已扫描 3/10" and item["steps"] == 2

        sent = await send_tool.execute(agent_id=agent_id, message="只看 src")
        assert sent.success is True and sent.output["queued"] is True
        assert child.interjections == ["只看 src"]

        detail = await list_tool.execute(agent_id=agent_id)
        assert detail.success is True
        assert detail.output["status"] == "running"
        assert detail.output["output_chars"] >= len("已扫描 3/10")
        assert "仍在运行" in detail.output["note"]

        stopped = await stop_tool.execute(agent_id=agent_id)
        assert stopped.success is True
        assert stopped.output["status"] == "cancelled"
        gate.set()

        await _tick()
        after = await list_tool.execute(agent_id=agent_id)
        assert after.success is True and after.output["status"] == "cancelled"
        assert "已结束" in after.output["note"]
        # 已结束不能再追加
        late = await send_tool.execute(agent_id=agent_id, message="再来一句")
        assert late.success is False and "已经结束" in late.error
        # stop 幂等
        again = await stop_tool.execute(agent_id=agent_id)
        assert again.success is True and again.output.get("already_finished") is True
        await _assert_settled(manager)

    async def test_list_results_are_bounded_for_parent_context(
            self, make_manager, monkeypatch):
        """20 条结果不能一次性原样塞进父上下文（总量/单条都有上限）。"""
        parent = FakeParent(factory=lambda: FakeChild(output="结论" * 3000))
        _bind_context(monkeypatch, parent=parent)
        manager = make_manager()
        for index in range(subagents.KEEP_RESULTS):
            agent_id = await manager.start(parent, f"任务 {index}", session_id="s-test")
            await _await_task(manager, agent_id)

        listed = await SubagentListTool(manager).execute()
        payload = listed.output
        assert payload["count"] == subagents.KEEP_RESULTS
        assert payload["running"] == 0
        assert len(payload.get("results", {})) <= subagents.KEEP_RESULTS
        total = sum(len(text) for text in payload.get("results", {}).values())
        assert total <= RESULT_TOTAL_CHARS * 2, f"结果总长度 {total} 超出上下文预算"
        assert payload.get("results_note"), "被截断时必须显式说明，不能静默剪掉"

    async def test_cross_session_isolation_of_lifecycle(
            self, make_manager, monkeypatch):
        parent = FakeParent(factory=lambda: FakeChild())
        manager = make_manager()
        first = await manager.start(parent, "会话一", session_id="s1")
        await _await_task(manager, first, session_id="s1")

        _bind_context(monkeypatch, parent=parent, session="s2")
        listing = await SubagentListTool(manager).execute(agent_id=first)
        assert listing.success is False, "跨会话点名查询必须失败"
        assert (await SubagentSendTool(manager).execute(
            agent_id=first, message="x")).success is False
        assert (await SubagentStopTool(manager).execute(agent_id=first)).success is False
        await _assert_settled(manager, session_id="s2")


# ═══════════════════════════════════════════════════════════
# 5. stop 的诚实性：未完成就说未完成、终态不残留 running
# ═══════════════════════════════════════════════════════════


class TestStopHonesty:
    async def test_stop_before_completion_marks_cancelled_honestly(
            self, make_manager):
        gate = asyncio.Event()
        parent = FakeParent(FakeChild(gate=gate))
        manager = make_manager()
        agent_id = await manager.start(parent, "停掉我", session_id="s-test")
        await _tick()

        result = await manager.stop(agent_id, session_id="s-test")
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        assert result["stop_requested"] is True
        done = _of_type(parent, "subagent_done")[-1]
        assert done["status"] == "cancelled"
        assert done["stop_reason"] == "cancelled", (
            "停止的终态要说明原因（stop_reason），不能留空让上层猜")
        gate.set()
        await _tick()
        await _assert_settled(manager)

    async def test_stop_before_task_starts_settles_with_reason(
            self, make_manager):
        """取消落在子任务协程体开始之前：终态必须补结算，且原因如实。"""
        gate = asyncio.Event()
        parent = FakeParent(FakeChild(gate=gate))
        manager = make_manager()
        agent_id = await manager.start(parent, "刚启动就停", session_id="s-test")

        result = await manager.stop(agent_id, session_id="s-test")
        assert result["ok"] is True and result["status"] == "cancelled"
        snapshot = manager.get(agent_id, session_id="s-test")
        assert snapshot["status"] == "cancelled"
        assert snapshot["stop_reason"] == "cancelled", "补结算也要给出停止原因"
        done = _of_type(parent, "subagent_done")
        assert done and done[-1]["status"] == "cancelled"
        assert done[-1]["stop_reason"] == "cancelled"
        await _assert_settled(manager)

    async def test_stop_timeout_reports_unfinished_not_stopped(
            self, make_manager, monkeypatch):
        """子任务吞掉取消（收尾不可打断）时，stop 必须如实说"还没结束"。"""
        monkeypatch.setattr(subagents, "STOP_WAIT_S", 0.05)
        gate = asyncio.Event()
        release = asyncio.Event()
        child = FakeChild(gate=gate, stubborn=True, release=release)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "顽固任务", session_id="s-test")
        await child.started.wait()

        result = await manager.stop(agent_id, session_id="s-test")
        assert result["ok"] is True
        assert result["stop_requested"] is True
        assert result["status"] == "running", "没结束就不能报成已停止"
        assert "没有在" in result["note"] and "可能仍在收尾" in result["note"], (
            f"超时后的说明必须诚实且可操作：{result['note']}")
        assert manager.get(agent_id, session_id="s-test")["status"] == "running"

        # 放行后子任务正常收尾；因为用户已请求停止，终态必须是 cancelled
        release.set()
        snapshot = await _wait_terminal(manager, agent_id)
        assert snapshot["status"] == "cancelled"
        done = _of_type(parent, "subagent_done")[-1]
        assert done["status"] == "cancelled"
        assert done["stop_reason"] == "cancelled"
        await _assert_settled(manager)

    async def test_concurrent_stops_are_idempotent_and_do_not_leak(
            self, make_manager):
        gate = asyncio.Event()
        parent = FakeParent(FakeChild(gate=gate))
        manager = make_manager()
        agent_id = await manager.start(parent, "两个停止同时到", session_id="s-test")
        await _tick()

        first, second = await asyncio.gather(
            manager.stop(agent_id, session_id="s-test"),
            manager.stop(agent_id, session_id="s-test"))
        assert first["ok"] is True and second["ok"] is True
        assert manager.get(agent_id, session_id="s-test")["status"] == "cancelled"
        gate.set()
        await _tick()
        await _assert_settled(manager)


# ═══════════════════════════════════════════════════════════
# 6. 会话收尾：cancel_session 的终态事件恰好一次、无泄漏
# ═══════════════════════════════════════════════════════════


class TestSessionCancel:
    async def test_cancel_session_with_stubborn_child_emits_one_done(
            self, make_manager, monkeypatch):
        """父任务结束时取消子任务：即使子任务要过一会才真正停下，终态事件也只发一次。

        收尾路径会"补结算"（先把记录标记为 cancelled 并发 done），而被取消的
        子任务随后真正返回时又会走一次正常收尾 —— 两条路径都发 done 的话，
        界面会出现同一结果的重复卡片。
        """
        monkeypatch.setattr(subagents, "STOP_WAIT_S", 0.05)
        gate = asyncio.Event()
        release = asyncio.Event()
        child = FakeChild(gate=gate, stubborn=True, release=release)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "会话收尾时的顽固任务",
                                       session_id="s-test")
        await child.started.wait()

        assert await manager.cancel_session(session_id="s-test") == 1
        snapshot = manager.get(agent_id, session_id="s-test")
        assert snapshot["status"] == "cancelled", "补结算必须如实标记为已停止"

        release.set()
        record = manager.get_record(agent_id, session_id="s-test")
        assert record is not None and record.task_obj is not None
        await asyncio.wait({record.task_obj}, timeout=2.0)

        dones = [event for event in _of_type(parent, "subagent_done")
                 if event.get("agent_id") == agent_id]
        assert len(dones) == 1, f"终态事件重复推送了 {len(dones)} 次"
        assert dones[0]["status"] == "cancelled"
        await _assert_settled(manager)

    async def test_cancel_session_is_bounded_and_idempotent(self, make_manager):
        gate = asyncio.Event()
        parent = FakeParent(factory=lambda: FakeChild(gate=gate))
        manager = make_manager()
        ids = [await manager.start(parent, f"并行 {i}", session_id="s-test")
               for i in range(subagents.MAX_CONCURRENT)]
        await _tick()

        started = time.perf_counter()
        assert await manager.cancel_session(session_id="s-test") == len(ids)
        assert time.perf_counter() - started < 2, "会话收尾不该卡住父任务"
        for agent_id in ids:
            assert manager.get(agent_id, session_id="s-test")["status"] == "cancelled"
        assert await manager.cancel_session(session_id="s-test") == 0, "二次收尾应当幂等"
        gate.set()
        await _assert_settled(manager)


# ═══════════════════════════════════════════════════════════
# 7. 派发失败 / 取消不得留下 running 孤儿记录
# ═══════════════════════════════════════════════════════════

class TestStartFailureNoLeak:
    async def test_cancelled_start_emit_leaves_no_running_record(
            self, make_manager):
        """派发事件推送中途被取消（父任务停止 / 服务关闭）时不能留下孤儿记录。

        此时后台 task 还没创建；若记录留在表里，界面会永远显示一个"运行中"
        但根本没有执行体的子智能体，也会占掉一个并发名额。
        """
        parent = FakeParent(FakeChild())
        manager = make_manager()

        async def _cancel_sink(event: dict[str, Any]) -> None:
            raise asyncio.CancelledError()

        parent.event_sink = _cancel_sink
        with pytest.raises(asyncio.CancelledError):
            await manager.start(parent, "派发到一半被取消", session_id="s-test")

        assert manager.list(session_id="s-test") == []
        assert manager._index == {}, "被取消的派发留下了全局索引"
        assert manager._by_session.get("s-test", {}) == {}
        await _assert_settled(manager)

    async def test_clone_failure_leaves_no_record(self, make_manager):
        parent = FakeParent()

        def _boom():
            raise RuntimeError("克隆失败")

        parent.clone_for_session = _boom  # type: ignore[assignment]
        manager = make_manager()
        with pytest.raises(subagents.SubagentError):
            await manager.start(parent, "克隆会失败", session_id="s-test")
        assert manager._index == {}
        assert manager.list(session_id="s-test") == []

    async def test_stop_after_external_task_cancel_settles_record(
            self, make_manager):
        """后台任务被外部取消（非经 stop）后，记录也必须补结算，不能停在 running。"""
        gate = asyncio.Event()
        parent = FakeParent(FakeChild(gate=gate))
        manager = make_manager()
        agent_id = await manager.start(parent, "外部取消", session_id="s-test")
        record = manager.get_record(agent_id, session_id="s-test")
        assert record is not None and record.task_obj is not None
        record.task_obj.cancel()              # 模拟运行时/事件循环直接取消任务
        await _tick()

        result = await manager.stop(agent_id, session_id="s-test")
        assert result["ok"] is True
        assert result["status"] == "cancelled", (
            "任务已被取消但记录仍报 running：stop 必须补结算")
        await _assert_settled(manager)


# ═══════════════════════════════════════════════════════════
# 8. 子任务预算 / 输出截断 / 事件 sink
# ═══════════════════════════════════════════════════════════


class TestChildBudgetAndOutput:
    async def test_limits_are_clamped_to_module_ceilings(self, make_manager):
        child = FakeChild()
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(
            parent, "越界限额", session_id="s-test",
            max_steps=10_000, max_tokens=10_000_000,
            allowed_tools=["file_read"])
        await _await_task(manager, agent_id)

        call = child.calls[0]
        assert call["max_steps"] == subagents.MAX_MAX_STEPS, "max_steps 未被模块上限夹住"
        assert call["max_tokens"] == subagents.MAX_MAX_TOKENS, "max_tokens 未被模块上限夹住"
        assert call["allowed_tools"] == ["file_read"]

    async def test_unspecified_limits_use_bounded_defaults(self, make_manager):
        child = FakeChild()
        manager = make_manager()
        agent_id = await manager.start(FakeParent(child), "默认限额", session_id="s-test")
        await _await_task(manager, agent_id)
        call = child.calls[0]
        assert call["max_steps"] == subagents.DEFAULT_MAX_STEPS
        assert call["max_tokens"] == 0, "0 = 继承父任务预算，不额外设限"

    async def test_long_output_is_truncated_in_record_detail_and_event(
            self, make_manager):
        huge = "x" * 60_000
        parent = FakeParent(FakeChild(output=huge))
        manager = make_manager()
        agent_id = await manager.start(parent, "超长输出", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        record = manager.get_record(agent_id, session_id="s-test")
        assert record is not None
        assert len(record.output) <= subagents.OUTPUT_KEEP_CHARS + 80, (
            f"记录里的输出没有截断：{len(record.output)} 字符")
        assert "已截断" in record.output

        assert len(snapshot["output"]) <= subagents.OUTPUT_KEEP_CHARS + 80
        assert "已截断" in snapshot["output"], "详情里的截断要标注原长"

        done = _of_type(parent, "subagent_done")[-1]
        assert len(done["output"]) <= subagents.EVENT_OUTPUT_CHARS + 80, (
            "事件里的输出没有截断，会把 WebSocket 帧撑爆")
        assert "已截断" in done["output"]

        started = _of_type(parent, "subagent_started")
        assert len(started[0]["task"]) <= subagents.EVENT_TASK_CHARS + 64

    async def test_budget_exhausted_is_reported_as_resumable(
            self, make_manager):
        child = FakeChild(success=False, output="已完成一半", steps=9, tokens=50_000,
                          error="token 预算耗尽", result={
                              "success": False, "output": "已完成一半",
                              "steps": 9, "tokens": 50_000,
                              "stop_reason": "budget_exhausted", "error": ""})
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "预算不够的任务", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)

        assert snapshot["status"] == "budget_exhausted", snapshot
        assert snapshot["resumable"] is True
        assert snapshot["stop_reason"] == "budget_exhausted"
        assert "已完成一半" in snapshot["output"], "部分产出必须保留"
        done = _of_type(parent, "subagent_done")[-1]
        assert done["status"] == "budget_exhausted"
        assert done["resumable"] is True
        await _assert_settled(manager)


class TestEventSink:
    async def test_progress_events_reach_the_parent_sink(
            self, make_manager, monkeypatch):
        # 进度事件默认按 0.5s 节流（记录照更新，事件不刷屏）。审计要验的是
        # "两条进度都送达"，所以这里把节流关掉。
        monkeypatch.setattr(subagents, "MIN_UPDATE_INTERVAL_S", 0.0)
        child = FakeChild(events=({"output": "进度一", "steps": 1},
                                  {"output": "进度二", "tokens": 42}))
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "推两条进度", session_id="s-test")
        await _await_task(manager, agent_id)

        kinds = [event["type"] for event in parent.events]
        assert kinds == ["subagent_started", "subagent_update", "subagent_update",
                         "subagent_done"], kinds
        updates = _of_type(parent, "subagent_update")
        assert updates[-1]["output_tail"] == "进度二"
        assert updates[-1]["tokens"] == 42
        assert all(event["session_id"] == "s-test" for event in parent.events)

    async def test_broken_sink_does_not_break_the_subagent(self, make_manager):
        parent = FakeParent(FakeChild(output="照常完成"))

        async def _broken(event: dict[str, Any]) -> None:
            raise RuntimeError("WebSocket 已断开")

        parent.event_sink = _broken
        manager = make_manager()
        agent_id = await manager.start(parent, "事件发送失败", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)
        assert snapshot["status"] == "done" and snapshot["output"] == "照常完成"
        await _assert_settled(manager)

    async def test_no_sink_still_completes_without_leak(self, make_manager):
        parent = FakeParent(FakeChild(output="安静完成"))
        parent.event_sink = None
        manager = make_manager()
        agent_id = await manager.start(parent, "没有事件出口", session_id="s-test")
        snapshot = await _wait_terminal(manager, agent_id)
        assert snapshot["status"] == "done"
        assert parent.events == []
        await _assert_settled(manager)

    async def test_sync_sink_call_updates_record_immediately(self, make_manager):
        gate = asyncio.Event()
        child = FakeChild(gate=gate)
        parent = FakeParent(child)
        manager = make_manager()
        agent_id = await manager.start(parent, "同步回调", session_id="s-test")
        await _tick()

        sink = child.calls[0]["event_sink"]
        returned = sink({"output": "同步进度", "steps": 1})   # 故意不 await
        listed = manager.list(session_id="s-test")[0]
        assert listed["output_tail"] == "同步进度" and listed["steps"] == 1
        if asyncio.iscoroutine(returned) or hasattr(returned, "__await__"):
            await returned
        gate.set()
        await _await_task(manager, agent_id)
        await _assert_settled(manager)
