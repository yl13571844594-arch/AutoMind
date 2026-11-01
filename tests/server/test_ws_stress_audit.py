"""v1.11.23 审计：WS / 团队在线 / 重连 / 并发压力。

## 审计范围

这份文件不是"再测一遍功能"，而是把 v1.11.20 断线保活与 v1.11.15 团队在线放在
**连接风暴 + 并发 + 反复断开**下压：进程级注册表（``_ws_clients`` /
``_ws_sessions`` / ``_ws_identity`` / ``_ws_tasks`` / ``_live_runs`` /
``_live_run_ctxs`` / 在线成员表）任何一个漏删，都会在长跑里变成"幽灵任务、
输入框永远锁着、并发上限越跑越小"这类难复现问题。

覆盖场景：

1. 多连接注册 / 绑定 / 断开（20 轮）后所有全局表归零；
2. 同一成员多标签页在 presence 里聚合为一个人（tabs=2/3），逐个断开后 tabs
   递减、全部断开后在线表清空；
3. 同一成员换工作区（member_id 变化）时在线表先拆后合；
4. 同一会话重复 run 被明确拒绝，且第一个任务不受影响；
5. 两个会话并发跑任务时事件严格不串台（25 条流式事件各自归属）；
6. run→断线→重连接管：run_state 回放 running，后续事件送到新连接，终态在
   新连接落地，计数与注册表归零；
7. run 在断线期间跑完：终态 + 完整 output 在重连 bind 时回放（TTL 内），
   之后资源被 TTL 回收；
8. 旧客户端（不带 reattach）断线 → 任务取消、并发槽归还、无僵尸 ctx；
9. stop（本连接）与 stop（重连接管后的另一条连接）都能真正取消运行；
10. grace 到点无人接管 → 批量任务被取消 → TTL 回收，注册表与计数归零；
11. 8 个会话并发执行，全部完成后并发计数与 ctx 表归零；
12. **审计发现的真实 bug**：``_ws_run`` 把 ``task_start`` 的发送放在主 try
    之外，而它可能是本任务的第一个 await —— 若恰在这里被取消（用户立刻点
    停止 / 旧客户端断线取消），``finally`` 不执行，``_running_tasks`` 计数
    永久 +1，并发上限越跑越小。回归用例：
    ``test_cancel_at_task_start_send_releases_concurrency_slot``。

全部用内存假 WebSocket 驱动真实 ``ws_endpoint`` / ``_ws_run``，不启动 uvicorn、
不联网、不落盘；异步等待用 0/1ms 轮询 + 明确超时，避免真实 sleep 拖慢 CI。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

import automind.server as srv
from automind.core import team_presence as presence
from automind.core.types import InteractionMode

# ═══════════════════════════════════════════════════════════
# 假连接 / 工具
# ═══════════════════════════════════════════════════════════

_CLOSE = object()


class _RecordingWS:
    """内存假 WebSocket：出站记账，入站从队列取；``close_from_client`` 断开。

    与 ``tests/server/test_ws_run_reattach.py`` 的 ``_EndpointWS`` 同思路，
    但多了"连接可以一直开着、随时喂消息"的队列能力 —— 压力测试要并发地
    喂 bind/run/stop，而不是开局就把消息列表消费光。
    """

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.sent: list[dict] = []
        self.accepted = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.accepted = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(dict(data))

    async def receive_json(self) -> dict:
        item = await self._queue.get()
        if item is _CLOSE:
            raise srv.WebSocketDisconnect()
        if isinstance(item, BaseException):
            raise item
        return item

    # ── 测试驱动接口 ──
    def push(self, data: dict) -> None:
        self._queue.put_nowait(data)

    def close_from_client(self) -> None:
        self._queue.put_nowait(_CLOSE)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]

    def of_type(self, type_name: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == type_name]


async def _wait_for(predicate, *, timeout: float = 3.0, what: str = "条件"):
    """轮询等待（0/1ms 让步），超时抛 AssertionError 并带上下文。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
        if time.monotonic() < deadline:
            await asyncio.sleep(0.001)
    raise AssertionError(f"等待超时（{timeout:g}s）：{what}")


async def _open_ws() -> tuple[_RecordingWS, asyncio.Task]:
    ws = _RecordingWS()
    task = asyncio.create_task(srv.ws_endpoint(ws))
    await _wait_for(lambda: ws.accepted, what="连接建立（accept）")
    return ws, task


async def _close_ws(ws: _RecordingWS, task: asyncio.Task) -> None:
    ws.close_from_client()
    with suppress(Exception):
        await asyncio.wait_for(task, timeout=3)


def _assert_all_tables_empty(*, connections: bool = True, sessions: bool = True):
    """全局注册表必须回到初始状态（任何一项非空都是泄漏）。"""
    assert srv._live_run_ctxs == {}, f"僵尸 LiveRun：{srv._live_run_ctxs}"
    assert srv._live_runs == {}, f"残留 _live_runs：{srv._live_runs}"
    assert srv._ws_tasks == {}, f"残留 _ws_tasks：{srv._ws_tasks}"
    if sessions:
        assert srv._ws_sessions == {}, f"残留 _ws_sessions：{srv._ws_sessions}"
    if connections:
        assert srv._ws_clients.get("all") == [], \
            f"残留连接：{len(srv._ws_clients.get('all') or [])} 条"
    assert srv._ws_identity == {}, f"残留连接身份：{srv._ws_identity}"
    assert srv._running_tasks["count"] == 0, \
        f"并发计数未归零：{srv._running_tasks['count']}"
    assert srv._approval_waiting["count"] == 0, \
        f"审批等待计数未归零：{srv._approval_waiting['count']}"
    assert presence.client_count() == 0, \
        f"在线表残留 {presence.client_count()} 条连接"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """进程级表哪怕漏一点都会串到别的用例，每个用例前后都清零。"""
    monkeypatch.setattr(srv, "_auth_token", lambda: "")
    # 在线成员 payload 会顺带读团队任务负载（SQLite）——审计不需要它，返回空表
    monkeypatch.setattr(srv, "_team_load", lambda: [])

    def _reset() -> None:
        srv._live_run_ctxs.clear()
        srv._live_runs.clear()
        srv._ws_tasks.clear()
        srv._ws_sessions.clear()
        srv._ws_identity.clear()
        srv._ws_clients["all"] = []
        srv._pending_approvals.clear()
        srv._ws_approvals.clear()
        srv._interject_refs.clear()
        srv._running_tasks["count"] = 0
        srv._approval_waiting["count"] = 0
        presence.reset()

    _reset()
    yield
    # 先取消本用例可能残留的后台任务，再清表（顺序反了会留下 pending task 告警）
    for ctx in list(srv._live_run_ctxs.values()):
        for task in (ctx.expiry_task, ctx.retention_task, ctx.task):
            if task is not None and not task.done():
                task.cancel()
    for task in list(srv._ws_tasks.values()):
        if task is not None and not task.done():
            task.cancel()
    _reset()


# ═══════════════════════════════════════════════════════════
# 真实 _ws_run 所需的 chat 替身（不联网、不落盘）
# ═══════════════════════════════════════════════════════════


class _ChatAgent:
    """只实现 chat 流式路径需要的表面。

    ``gate``/``parts`` 用来把"跑到一半"变成可控状态：产出第 ``gate_after``
    个片段后等待 gate，方便测试在流式过程中断开/接管/取消。
    """

    def __init__(self) -> None:
        self._interaction = InteractionMode.CHAT
        self._mode = None
        self.llm = object()                  # 非 None，越过就绪检查
        self.session_id = ""
        self._run_session_id = ""
        self.approval_callback = None
        self.plan_review_cb = None
        self.prefer_tools: list[str] = []
        self.event_sink = None
        self._last_stream_usage = None
        self._interjections = SimpleNamespace(dropped=0)
        self.parts: list[str] = ["part1", "part2"]
        self.gate: asyncio.Event | None = None
        self.gate_after = 1
        self.on_stream_start: asyncio.Event | None = None

    async def chat_stream(self, task, images=None, history=None):
        if self.on_stream_start is not None:
            self.on_stream_start.set()
        for i, piece in enumerate(self.parts):
            yield piece
            if self.gate is not None and i + 1 >= self.gate_after:
                await self.gate.wait()

    def interjection_report(self) -> dict:
        return {}

    def drain_interjections(self) -> list:
        return []


@pytest.fixture
def chat_run_env(monkeypatch):
    """把 _ws_run 的外部依赖替换成替身，只保留执行主流程（真实 _ws_run）。"""
    agent = _ChatAgent()
    monkeypatch.setattr(srv, "get_agent", lambda: agent)
    monkeypatch.setattr(srv, "_acquire_run_agent", lambda base, _sid: base)
    monkeypatch.setattr(srv, "_apply_mode_model", lambda a, _interaction: a)
    monkeypatch.setattr(srv, "_resolve_model_override",
                        lambda a, _data, _raw, _images, _receipt: (a, True))
    monkeypatch.setattr(srv, "_apply_expert_override",
                        lambda task, _data, _rec: task)
    monkeypatch.setattr(srv, "_apply_references",
                        lambda task, _data, _rec: (task, []))
    monkeypatch.setattr(srv, "_apply_goal", lambda task, _data, _rec: (task, ""))
    monkeypatch.setattr(srv, "_apply_attachments",
                        lambda task, _data, _rec: (task, ""))
    monkeypatch.setattr(srv, "_apply_mentions",
                        lambda task, _data, _rec: (task, ""))
    monkeypatch.setattr(srv, "_apply_conversation_context",
                        lambda task, _sid: task)
    monkeypatch.setattr(srv, "_get_session_history", lambda _sid: [])
    monkeypatch.setattr(srv, "_cache_lookup", lambda _task, _images: None)
    monkeypatch.setattr(srv, "_cache_store", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_apply_kb", lambda task, _hist=None: task)
    monkeypatch.setattr(srv, "_push_history", lambda record: record)
    monkeypatch.setattr(srv, "_accumulate_tokens", lambda _record: None)
    monkeypatch.setattr(srv, "_record_and_notify", lambda _sid, _ev: None)
    monkeypatch.setattr(srv, "_goal_finish", lambda _ctx, _sid, _task: None)
    monkeypatch.setattr(srv, "_save_active", lambda **_kw: None)
    monkeypatch.setattr(srv, "_quota",
                        SimpleNamespace(try_consume_task=lambda: (True, "")))

    async def _save_ok(sid, ws=None, send=None):
        return True

    monkeypatch.setattr(srv, "_save_history_notify", _save_ok)
    return agent


def _chat_run_payload(sid: str, *, reattach: bool = True, task: str = "任务") -> dict:
    return {"action": "run", "task": task, "session_id": sid,
            "interaction": "chat", "reattach": reattach}


# ═══════════════════════════════════════════════════════════
# 1. 多连接注册 / 绑定 / 断开 + presence 聚合
# ═══════════════════════════════════════════════════════════


async def test_twenty_connect_bind_disconnect_rounds_leave_no_state():
    """20 轮"连接→bind→断开"：每轮结束所有全局表都要回零。"""
    started = time.perf_counter()
    for i in range(20):
        ws, task = await _open_ws()
        ws.push({"action": "bind", "session_id": f"s{i}", "member_id": f"m{i}"})
        await _wait_for(
            lambda i=i, ws=ws: any(m.get("type") == "run_state"
                                   and m.get("session_id") == f"s{i}"
                                   for m in ws.sent),
            what=f"第 {i} 轮 bind 的 run_state 回执")
        assert presence.client_count() == 1
        await _close_ws(ws, task)
        assert srv._ws_clients.get("all") == [], f"第 {i} 轮断开后连接表未清空"
        assert srv._ws_sessions == {}, f"第 {i} 轮断开后会话表未清空"
        assert presence.client_count() == 0, f"第 {i} 轮断开后在线成员未清空"

    _assert_all_tables_empty()
    elapsed = time.perf_counter() - started
    # 20 轮全走内存/事件循环，正常应在亚秒级；超过 10s 说明引入了真实等待
    assert elapsed < 10.0, f"20 轮连接风暴耗时 {elapsed:.2f}s（疑似真实等待）"


async def test_multi_tab_presence_aggregates_then_cleans_up():
    """同一成员开 3 个标签页 → presence 聚合为一个人 tabs=3，逐个断开递减。"""
    pairs = [await _open_ws() for _ in range(3)]
    all_ws = [ws for ws, _ in pairs]
    for i, ws in enumerate(all_ws):
        ws.push({"action": "bind", "session_id": "s-me",
                 "member_id": "m-1", "tab": i})
    await _wait_for(lambda: presence.count() == 1
                    and presence.list_members()[0]["sid"] == "m-1"
                    and presence.list_members()[0]["tabs"] == 3,
                    what="3 个标签页聚合为 1 人（m-1）")
    member = presence.list_members()[0]
    assert member["sid"] == "m-1" and member["tabs"] == 3
    assert len(srv._ws_sessions.get("s-me") or []) == 3, "3 条连接应绑定同一会话"

    # 断开一个标签页：其余标签页收到 presence 更新，tabs 递减
    ws0, task0 = pairs.pop(0)
    await _close_ws(ws0, task0)
    await _wait_for(lambda: presence.count() == 1
                    and presence.list_members()[0]["tabs"] == 2,
                    what="断开一个标签页后 tabs=2")
    assert all(ws.of_type("team_presence") for ws in all_ws), \
        "标签页从未收到 team_presence 广播"

    # 全部断开：在线表 / 连接表 / 会话表全部清空
    for ws, task in pairs:
        await _close_ws(ws, task)
    await _wait_for(lambda: presence.client_count() == 0, what="全部断开后在线表清空")
    _assert_all_tables_empty()


async def test_repeated_bind_same_session_is_idempotent_and_cleans_up():
    """审计修复回归：重复 bind 到同一会话不能把同一连接登记两份。

    真实前端"连接时预绑 default → 页面打开再 bind default"必然重复一次；
    旧实现会追加第二份，断开时只摘一份，``_ws_sessions`` 里留下已断开的
    WebSocket 引用（长跑服务器持续累积）。修复后 bind 幂等、unbind 清干净。
    """
    ws, task = await _open_ws()
    try:
        # 与 ws_endpoint 建立连接时的预绑定重复（default → default）
        ws.push({"action": "bind", "session_id": "default", "member_id": "m-dup"})
        await _wait_for(lambda: ws.of_type("run_state"), what="首次 bind 回执")
        ws.push({"action": "bind", "session_id": "default", "member_id": "m-dup"})
        # 同一会话再 bind 两次；中途切到另一个会话再切回来
        ws.push({"action": "bind", "session_id": "s-dup", "member_id": "m-dup"})
        ws.push({"action": "bind", "session_id": "s-dup", "member_id": "m-dup"})
        await _wait_for(lambda: srv._ws_sessions.get("s-dup") == [ws],
                        what="同一会话只登记一份")
        assert "default" not in srv._ws_sessions, "旧会话绑定没有摘干净"
        assert all(sockets.count(ws) == 1 for sockets in srv._ws_sessions.values())
    finally:
        await _close_ws(ws, task)
    absent = [sid for sid, sockets in srv._ws_sessions.items() if ws in sockets]
    assert absent == [], f"断开后 _ws_sessions 残留幽灵连接：{absent}"
    _assert_all_tables_empty()


async def test_member_rebind_splits_and_merges_presence():
    """同一连接换工作区（member_id 变化）：在线表先拆成两人、再合回一人。"""
    ws1, t1 = await _open_ws()
    ws2, t2 = await _open_ws()
    try:
        ws1.push({"action": "bind", "session_id": "sa", "member_id": "m-1"})
        ws2.push({"action": "bind", "session_id": "sb", "member_id": "m-1"})
        await _wait_for(lambda: presence.count() == 1
                        and presence.list_members()[0]["sid"] == "m-1"
                        and presence.list_members()[0]["tabs"] == 2,
                        what="两标签页聚合为一人（m-1）")

        ws2.push({"action": "bind", "session_id": "sb", "member_id": "m-2"})
        await _wait_for(lambda: {m["sid"] for m in presence.list_members()}
                        == {"m-1", "m-2"},
                        what="换工作区后拆成两人")
        assert all(m["tabs"] == 1 for m in presence.list_members())

        ws2.push({"action": "bind", "session_id": "sb", "member_id": "m-1"})
        await _wait_for(lambda: presence.count() == 1
                        and presence.list_members()[0]["tabs"] == 2,
                        what="换回同一工作区后合回一人")
    finally:
        await _close_ws(ws2, t2)
        await _close_ws(ws1, t1)
    _assert_all_tables_empty()


# ═══════════════════════════════════════════════════════════
# 2. 同会话重复 run：拒绝且不伤害第一个任务
# ═══════════════════════════════════════════════════════════


async def test_duplicate_run_same_session_rejected_without_killing_first(
        monkeypatch):
    gate = asyncio.Event()
    started: list[dict] = []

    async def fake_run(sock, client_id, data, run_ctx=None):
        started.append(data)
        try:
            await gate.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(srv, "_ws_run", fake_run)
    ws, task = await _open_ws()
    try:
        ws.push({"action": "run", "task": "第一个", "session_id": "dup",
                 "interaction": "chat", "reattach": True})
        await _wait_for(lambda: len(started) == 1, what="第一个任务启动")
        ws.push({"action": "run", "task": "第二个", "session_id": "dup",
                 "interaction": "chat", "reattach": True})
        await _wait_for(lambda: ws.of_type("task_error"), what="重复 run 被拒绝")

        err = ws.of_type("task_error")[-1]
        assert "已有任务正在执行" in err["error"]
        assert len(started) == 1, f"同会话第二个 run 也启动了：{started}"
        ctx = srv._live_run_for_sid("dup")
        assert ctx is not None and not ctx.task.done(), "第一个任务被误杀"
    finally:
        await _close_ws(ws, task)
        ctx = srv._live_run_for_sid("dup")
        if ctx is not None:
            ctx._cancel_expiry()
            if ctx.task is not None and not ctx.task.done():
                ctx.task.cancel()
            with suppress(asyncio.CancelledError):
                await ctx.task
            await _wait_for(lambda: srv._live_run_ctxs == {},
                            what="重复 run 用例清理 ctx")
    _assert_all_tables_empty()


# ═══════════════════════════════════════════════════════════
# 3. 两个会话并发：事件严格不串台
# ═══════════════════════════════════════════════════════════


async def test_concurrent_runs_do_not_cross_events(monkeypatch):
    """两个会话各发 25 条流式事件：每条事件的归属必须严格是各自会话。"""
    events_per_run = 25

    async def fake_run(sock, client_id, data, run_ctx=None):
        rid = str(data.get("ref") or data.get("task") or "")
        for i in range(events_per_run):
            await run_ctx.send({"type": "chat_chunk", "session_id": rid,
                                "delta": f"{rid}:{i}"})
        done = {"type": "chat_done", "session_id": rid, "tokens": i}
        await run_ctx.send(done)
        run_ctx.finish(done, record={"output": rid})

    monkeypatch.setattr(srv, "_ws_run", fake_run)
    ws1, t1 = await _open_ws()
    ws2, t2 = await _open_ws()
    try:
        ws1.push({"action": "run", "task": "A", "session_id": "iso1",
                  "interaction": "chat", "reattach": True, "ref": "run-A"})
        ws2.push({"action": "run", "task": "B", "session_id": "iso2",
                  "interaction": "chat", "reattach": True, "ref": "run-B"})
        await _wait_for(lambda: ws1.of_type("chat_done")
                        and ws2.of_type("chat_done"), what="两个并发任务完成")

        chunks1 = ws1.of_type("chat_chunk")
        chunks2 = ws2.of_type("chat_chunk")
        assert len(chunks1) == events_per_run and len(chunks2) == events_per_run
        assert {m["session_id"] for m in chunks1} == {"run-A"}
        assert {m["session_id"] for m in chunks2} == {"run-B"}
        assert all(m["delta"].startswith("run-A:") for m in chunks1)
        assert all(m["delta"].startswith("run-B:") for m in chunks2)
        # 终态也各归各的
        assert ws1.of_type("chat_done")[0]["session_id"] == "run-A"
        assert ws2.of_type("chat_done")[0]["session_id"] == "run-B"
        await _wait_for(lambda: srv._live_run_ctxs == {}
                        and srv._live_runs == {}, what="并发任务 ctx 清理")
        assert srv._running_tasks["count"] == 0
    finally:
        await _close_ws(ws2, t2)
        await _close_ws(ws1, t1)
    _assert_all_tables_empty()


# ═══════════════════════════════════════════════════════════
# 4. run→断线→重连接管 / 终态回放（真实 _ws_run）
# ═══════════════════════════════════════════════════════════


async def test_reconnect_takeover_mid_run_delivers_rest_and_terminal(chat_run_env):
    agent = chat_run_env
    agent.gate = asyncio.Event()

    ws1, t1 = await _open_ws()
    ws1.push(_chat_run_payload("takeover"))
    await _wait_for(lambda: ws1.of_type("chat_chunk"), what="首个流式片段")
    ctx = srv._live_run_for_sid("takeover")
    assert ctx is not None and ctx.ws is ws1 and not ctx.task.done()

    # 断线：任务保留、ctx 只解绑
    await _close_ws(ws1, t1)
    assert ctx.ws is None, "断线后 ctx 仍持有已断开的连接"
    assert not ctx.task.done(), "断线后任务被取消了"

    # 新连接（刷新/重连）bind：回放 running + 接管事件出口
    ws2, t2 = await _open_ws()
    try:
        ws2.push({"action": "bind", "session_id": "takeover",
                  "member_id": "m-takeover"})
        await _wait_for(lambda: any(m.get("type") == "run_state"
                                    and m.get("running") for m in ws2.sent),
                        what="接管 run_state(running)")
        assert ctx.ws is ws2, "bind 之后事件出口没有切到新连接"
        state = [m for m in ws2.sent if m["type"] == "run_state"][-1]
        assert state["session_id"] == "takeover"
        assert state["run"]["interaction"] == "chat"

        agent.gate.set()
        await _wait_for(lambda: ws2.of_type("chat_done"),
                        what="chat_done 送达接管后的新连接")
        assert ctx.terminal is not None and ctx.terminal["type"] == "chat_done"
        assert ctx.terminal_record["output"] == "part1part2"
        await _wait_for(lambda: srv._live_run_ctxs == {}
                        and srv._live_runs == {}, what="终态后注册表清理")
        assert srv._running_tasks["count"] == 0
    finally:
        await _close_ws(ws2, t2)
    _assert_all_tables_empty()


async def test_run_finished_while_disconnected_replays_full_output(
        chat_run_env, monkeypatch):
    monkeypatch.setattr(srv, "_RUN_RESULT_TTL_S", 30.0)
    agent = chat_run_env
    agent.gate = asyncio.Event()

    ws1, t1 = await _open_ws()
    ws1.push(_chat_run_payload("offline-done", task="写周报"))
    await _wait_for(lambda: ws1.of_type("chat_chunk"), what="首个流式片段")
    ctx = srv._live_run_for_sid("offline-done")
    await _close_ws(ws1, t1)
    assert ctx is not None and ctx.ws is None

    # 断线期间回答写完：终态 + 完整 record 缓存在 ctx 里等回放
    agent.gate.set()
    await _wait_for(lambda: ctx.terminal is not None, what="断线期间任务完成")
    assert ctx.terminal["type"] == "chat_done"
    assert ctx.terminal_record["output"] == "part1part2"
    assert srv._live_run_for_sid("offline-done") is ctx, "TTL 内结果不可回放"
    assert srv._running_tasks["count"] == 0

    # 刷新页面 → bind → 回放完整回答
    ws2, t2 = await _open_ws()
    try:
        ws2.push({"action": "bind", "session_id": "offline-done",
                  "member_id": "m-offline"})
        await _wait_for(lambda: any(m.get("type") == "run_state"
                                    and m.get("terminal_record") for m in ws2.sent),
                        what="绑定后回放终态")
        state = [m for m in ws2.sent if m["type"] == "run_state"][-1]
        assert state["running"] is False
        assert state["terminal"]["type"] == "chat_done"
        assert state["terminal_record"]["output"] == "part1part2"
    finally:
        await _close_ws(ws2, t2)
        if ctx.retention_task is not None:
            ctx.retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await ctx.retention_task
        srv._unregister_live_run(ctx)
    _assert_all_tables_empty()


async def test_old_client_disconnect_cancels_run_and_releases_slot(chat_run_env):
    agent = chat_run_env
    agent.gate = asyncio.Event()

    ws, task = await _open_ws()
    ws.push(_chat_run_payload("old-client", reattach=False))
    await _wait_for(lambda: ws.of_type("chat_chunk"), what="旧客户端任务开始")
    ctx = srv._live_run_for_sid("old-client")
    assert ctx is not None and not ctx.reattach_supported

    await _close_ws(ws, task)

    await _wait_for(lambda: ctx.task is not None and ctx.task.done(),
                    what="旧客户端断线后任务被取消")
    assert ctx.task.cancelled(), "旧客户端断线没有取消任务（破坏旧语义）"
    await _wait_for(lambda: srv._live_run_ctxs == {}
                    and srv._live_runs == {}, what="取消后注册表清理")
    assert srv._running_tasks["count"] == 0, "取消路径并发槽没有归还"
    _assert_all_tables_empty()


async def test_stop_action_cancels_run_and_reports_terminal(chat_run_env):
    agent = chat_run_env
    agent.gate = asyncio.Event()

    ws, task = await _open_ws()
    try:
        ws.push(_chat_run_payload("stop-mode"))
        await _wait_for(lambda: ws.of_type("chat_chunk"), what="任务开始流式输出")
        ws.push({"action": "stop"})
        await _wait_for(lambda: ws.of_type("task_cancelled"),
                        what="停止后收到 task_cancelled 回执")
        await _wait_for(lambda: srv._live_run_ctxs == {}
                        and srv._running_tasks["count"] == 0,
                        what="停止后资源清理")
        # 连接本身仍然可用（stop 只中断任务，不关连接）
        ws.push({"action": "ping"})
        await _wait_for(lambda: ws.of_type("pong"), what="停止后连接仍可用")
    finally:
        await _close_ws(ws, task)
    _assert_all_tables_empty()


async def test_stop_from_takeover_connection_cancels_current_run(chat_run_env):
    """重连接管后的连接没有自己的 _ws_tasks 条目，stop 必须按会话找到 ctx。"""
    agent = chat_run_env
    agent.gate = asyncio.Event()

    ws1, t1 = await _open_ws()
    ws1.push(_chat_run_payload("stop-takeover"))
    await _wait_for(lambda: ws1.of_type("chat_chunk"), what="首个流式片段")
    await _close_ws(ws1, t1)

    ws2, t2 = await _open_ws()
    try:
        ws2.push({"action": "bind", "session_id": "stop-takeover",
                  "member_id": "m-stop"})
        await _wait_for(lambda: any(m.get("type") == "run_state"
                                    and m.get("running") for m in ws2.sent),
                        what="接管运行")
        ctx = srv._live_run_for_sid("stop-takeover")
        assert ctx is not None and ctx.ws is ws2
        assert t2 is not None

        ws2.push({"action": "stop"})
        await _wait_for(lambda: ws2.of_type("task_cancelled"),
                        what="接管连接 stop 后收到 task_cancelled")
        await _wait_for(lambda: srv._live_run_ctxs == {}
                        and srv._running_tasks["count"] == 0,
                        what="接管连接 stop 后资源清理")
    finally:
        await _close_ws(ws2, t2)
    _assert_all_tables_empty()


# ═══════════════════════════════════════════════════════════
# 5. grace 到点 + TTL 回收（10 个孤儿运行）
# ═══════════════════════════════════════════════════════════


async def test_orphaned_reattach_runs_cancelled_by_grace_and_reclaimed_by_ttl(
        chat_run_env, monkeypatch):
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 0.02)
    monkeypatch.setattr(srv, "_RUN_RESULT_TTL_S", 0.05)
    agent = chat_run_env

    for i in range(10):
        agent.gate = asyncio.Event()
        ws, task = await _open_ws()
        ws.push(_chat_run_payload(f"orphan{i}", task=f"孤儿任务{i}"))
        await _wait_for(lambda ws=ws: ws.of_type("chat_chunk"),
                        what=f"第 {i} 个任务开始")
        await _close_ws(ws, task)     # 断线且永不重连 → grace 到点取消

    # grace 取消 → 终态缓存 → TTL 回收
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and (
            srv._live_run_ctxs or srv._running_tasks["count"]):
        await asyncio.sleep(0.01)
    assert srv._running_tasks["count"] == 0, \
        f"10 个孤儿运行后并发计数泄漏：{srv._running_tasks['count']}"
    _assert_all_tables_empty(sessions=False, connections=False)
    assert srv._ws_sessions == {}, "断线会话绑定未清理"


# ═══════════════════════════════════════════════════════════
# 6. 并发执行压力：8 个会话同时跑
# ═══════════════════════════════════════════════════════════


async def test_eight_concurrent_runs_finish_and_release_everything(
        chat_run_env, monkeypatch):
    monkeypatch.setattr(srv, "_MAX_CONCURRENT", 32)
    agent = chat_run_env
    agent.parts = ["x", "y"]
    agent.gate = None

    pairs: list[tuple[_RecordingWS, asyncio.Task]] = []
    try:
        for i in range(8):
            ws, task = await _open_ws()
            ws.push(_chat_run_payload(f"conc{i}", task=f"并发任务{i}"))
            pairs.append((ws, task))
        for i, (ws, _) in enumerate(pairs):
            await _wait_for(lambda ws=ws: ws.of_type("chat_done"),
                            what=f"并发任务 {i} 完成")
        assert srv._running_tasks["count"] == 0, \
            f"8 个并发任务完成后计数为 {srv._running_tasks['count']}"
        assert srv._live_runs == {} and srv._live_run_ctxs == {}
    finally:
        for ws, task in pairs:
            await _close_ws(ws, task)
    assert srv._ws_tasks == {}, "并发结束后 _ws_tasks 残留"
    assert srv._ws_clients.get("all") == [] and srv._ws_sessions == {}
    _assert_all_tables_empty()


# ═══════════════════════════════════════════════════════════
# 7. 审计发现的真实 bug 回归：task_start 发送处被取消 → 并发槽泄漏
# ═══════════════════════════════════════════════════════════


async def test_cancel_at_task_start_send_releases_concurrency_slot(chat_run_env):
    """审计发现：``_ws_run`` 在 ``_running_tasks["count"] += 1`` 之后、主 try
    之前发 ``task_start``。若任务恰在这**第一个 await** 上被取消（用户立刻点
    停止、旧客户端断线取消、ws_endpoint 的 finally cancel），CancelledError
    会绕过 finally，并发计数永久 +1 —— 表现为"并发上限越跑越小，最后什么都
    发不出去"。修复：把该发送移进 try，让 finally 兜住取消。"""
    ws = _RecordingWS()
    started = asyncio.Event()

    class _SlowTaskStartCtx(srv.LiveRun):
        async def send(self, payload):
            if payload.get("type") == "task_start":
                started.set()
                await asyncio.sleep(30)      # 模拟"发送卡住时被取消"
            return await super().send(payload)

    ctx = _SlowTaskStartCtx(ws, "c1", "slow", {"task": "x",
                                               "session_id": "slow",
                                               "interaction": "chat"},
                            reattach_supported=True)
    srv._register_live_run(ctx)
    task = asyncio.create_task(
        srv._ws_run(ws, "c1", {"task": "x", "session_id": "slow",
                               "interaction": "chat"}, run_ctx=ctx))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert srv._running_tasks["count"] == 0, \
            "在 task_start 发送处取消后并发槽泄漏（finally 没有执行）"
        assert srv._live_runs == {}, "取消后 _live_runs 残留"
        assert srv._live_run_for_sid("slow") is None, "取消后 ctx 残留"
        assert ctx.terminal is not None, "取消路径没有缓存终态"
        assert ctx.terminal["type"] == "task_cancelled"
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # 直接调用 _ws_run 没有 ws_endpoint 的 finally —— 会话绑定要由测试自己摘掉
        srv._ws_unbind(ws)
    _assert_all_tables_empty()
