"""WS 断线保活（v1.11.20）：LiveRun + run_state 回放 + 旧行为兼容。

## 修的是什么

此前一次 WS 任务与发起它的那条 socket 生死绑定：连接一断（切网络、刷新页面、
笔记本合盖），``ws_endpoint`` 的 finally 立刻 cancel，前端也只能落一张"执行
失败"卡片。可任务明明还在服务器上跑 —— "页面在不在看"与"任务该不该继续"
本是两件事。

现在 ``LiveRun`` 把两者拆开：ws 只是当前事件出口（attach/detach 可更换）；
断开后给一段重连宽限，到点仍没人接管才取消；任务结束时把终态与完整 record
缓存在 ctx 上，重连/刷新后由 ``bind`` 回放的 ``run_state`` 接管。

## 这个文件盯住的几件事

1. ``LiveRun.send``：正常捕获；ws 抛异常/超时 → 返回 False 且标记 detached，
   **绝不向上抛**（发送失败不能反过来把任务弄挂）。
2. ``attach`` / ``state`` / ``finish``：能回放"正在跑"与"已完成（含 record）"。
3. grace 到点仍无人接管 → 取消任务；重新 attach → 取消 grace、任务继续。
4. 断线期间结束的运行留 TTL；TTL 到点从注册表清掉（不留内存）。
5. 旧语义：``reattach_supported=False`` 的 ctx 断开即取消；新页面也不谎报它。
6. ``_ws_run`` 直接调用（run_ctx=None）仍能跑完并收到 task_start/chat_done。
7. 接线契约（源码级）：ws_endpoint 的 run/stop/bind/finally 与 ws.ts 的关键字。

测试全部用假连接 + 直接调 helper/endpoint（endpoint 用内存假 WebSocket，
不启动真实 uvicorn / Starlette TestClient websocket —— 后者在本仓库里 flaky）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

import automind.server as srv
from automind.core.types import InteractionMode

# ═══════════════════════════════════════════════════════════
# 替身与夹具
# ═══════════════════════════════════════════════════════════


class _FakeWS:
    """只记账的假连接（与 tests/server/test_ws_interject.py 的 _WS 同款）。"""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.sent: list[dict] = []
        self.fail = fail
        self.delay = delay

    async def send_json(self, data: dict) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(dict(data))

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]


@pytest.fixture(autouse=True)
def _clean_tables():
    """这些表是进程级全局状态，测试之间必须互不污染。"""
    srv._live_run_ctxs.clear()
    srv._live_runs.clear()
    srv._ws_tasks.clear()
    srv._interject_refs.clear()
    yield
    # 别把 grace/TTL 的长睡眠任务留给下一个用例（也避免 loop 关闭时告警）
    for ctx in list(srv._live_run_ctxs.values()):
        for task in (ctx.expiry_task, ctx.retention_task, ctx.task):
            if task is not None and not task.done():
                task.cancel()
    srv._live_run_ctxs.clear()
    srv._live_runs.clear()
    srv._ws_tasks.clear()
    srv._interject_refs.clear()


def _ctx(ws=None, *, sid: str = "s1", task: str = "长任务",
         interaction: str = "work", reattach: bool = True) -> srv.LiveRun:
    return srv.LiveRun(ws if ws is not None else _FakeWS(), "c1", sid,
                       {"task": task, "interaction": interaction},
                       reattach_supported=reattach)


async def _sleep_task() -> asyncio.Task:
    """一个不会自己结束的任务（用来模拟"还在跑"）——调用方负责取消。"""
    return asyncio.create_task(asyncio.sleep(30))


# ═══════════════════════════════════════════════════════════
# 1. send：正常收发；送不到只解绑，绝不抛
# ═══════════════════════════════════════════════════════════


async def test_send_records_payloads_while_connected():
    ws = _FakeWS()
    ctx = _ctx(ws)

    assert await ctx.send({"type": "task_start"}) is True
    assert ws.types() == ["task_start"]
    assert ctx.ws is ws


async def test_send_failure_detaches_instead_of_raising(monkeypatch):
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 30.0)
    ctx = _ctx(_FakeWS(fail=True))

    assert await ctx.send({"type": "chat_chunk"}) is False
    assert ctx.ws is None, "发送失败后仍把死连接当出口"
    assert ctx.expiry_task is not None, "断线后没有进入重连宽限"
    ctx._cancel_expiry()


async def test_send_timeout_detaches_instead_of_hanging(monkeypatch):
    """对端不读的 socket 上 send 可能一直挂着 —— 任务不能卡在一条事件上。"""
    monkeypatch.setattr(srv, "_WS_SEND_TIMEOUT_S", 0.05)
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 30.0)
    ctx = _ctx(_FakeWS(delay=5.0))

    assert await ctx.send({"type": "step_thought"}) is False
    assert ctx.ws is None
    ctx._cancel_expiry()


# ═══════════════════════════════════════════════════════════
# 2. attach / state / finish：重连后能接管，也能回放结果
# ═══════════════════════════════════════════════════════════


async def test_attach_reflects_running_state_and_finish_caches_terminal():
    ctx = _ctx(_FakeWS(), task="分析日志", interaction="coding")
    ctx.task = await _sleep_task()
    srv._register_live_run(ctx)
    try:
        ctx.detach()
        assert ctx.ws is None

        new_ws = _FakeWS()
        assert ctx.attach(new_ws) is True, "attach 没有报告此前处于 detached"
        state = ctx.state()
        assert state["type"] == "run_state"
        assert state["running"] is True
        assert state["session_id"] == "s1"
        assert state["run"]["interaction"] == "coding"
        assert "分析日志" in state["run"]["task"]
        assert state["terminal"] is None

        record = {"session_id": "run-1", "output": "完整结果", "success": True}
        ctx.finish({"type": "task_complete", "session_id": "run-1"}, record=record)
        done = ctx.state()
        assert done["running"] is False
        assert done["terminal"]["type"] == "task_complete"
        assert done["terminal_record"]["output"] == "完整结果"
        assert srv._live_run_for_sid("s1") is None, "终态已送到界面却还占着注册表"
    finally:
        ctx.task.cancel()
        with suppress(asyncio.CancelledError):
            await ctx.task


# ═══════════════════════════════════════════════════════════
# 3. 重连宽限：到点取消；及时接管则不取消
# ═══════════════════════════════════════════════════════════


async def test_grace_expiry_cancels_the_orphaned_run(monkeypatch):
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 0.05)
    ctx = _ctx()
    ctx.task = await _sleep_task()
    srv._register_live_run(ctx)

    ctx.detach()
    await asyncio.sleep(0.2)

    assert ctx.task.cancelled(), "grace 到点仍没人接管，任务却没有被取消"


async def test_reconnect_within_grace_keeps_the_run_alive(monkeypatch):
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 0.05)
    ctx = _ctx()
    ctx.task = await _sleep_task()
    srv._register_live_run(ctx)
    try:
        ctx.detach()
        assert ctx.attach(_FakeWS()) is True
        await asyncio.sleep(0.15)
        assert not ctx.task.done(), "重连接管之后任务仍被 grace 取消了"
    finally:
        ctx._cancel_expiry()
        ctx.task.cancel()
        with suppress(asyncio.CancelledError):
            await ctx.task


# ═══════════════════════════════════════════════════════════
# 4. 断线期间完成的运行：留 TTL 回放，到点清理
# ═══════════════════════════════════════════════════════════


async def test_finished_detached_run_survives_until_ttl(monkeypatch):
    monkeypatch.setattr(srv, "_RUN_RESULT_TTL_S", 0.05)
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 30.0)
    ctx = _ctx()
    srv._register_live_run(ctx)
    ctx.detach()

    ctx.finish({"type": "task_error", "session_id": "run-1",
                "error": "断线期间失败了"})
    assert srv._live_run_for_sid("s1") is ctx, "TTL 内应当可供刷新页面回放"
    assert ctx.expiry_task is None, "终态已缓存却还挂着 grace 计时"

    await asyncio.sleep(0.2)
    assert srv._live_run_for_sid("s1") is None, "TTL 到点后结果没有被清理"


async def test_ttl_cleanup_does_not_remove_a_newer_run(monkeypatch):
    """旧 ctx 的 TTL 到点时，不能把同会话的新一次运行从注册表里顶掉。"""
    monkeypatch.setattr(srv, "_RUN_RESULT_TTL_S", 0.05)
    old = _ctx(reattach=True)
    srv._register_live_run(old)
    old.detach()
    old.finish({"type": "task_cancelled", "session_id": "run-old"})

    newer = _ctx(reattach=True)
    newer.task = await _sleep_task()
    try:
        srv._register_live_run(newer)         # 同会话的新任务顶替旧 ctx
        await asyncio.sleep(0.2)
        assert srv._live_run_for_sid("s1") is newer
    finally:
        newer.task.cancel()
        with suppress(asyncio.CancelledError):
            await newer.task


# ═══════════════════════════════════════════════════════════
# 5. 旧客户端：断开即取消，且新页面不被谎报"正在跑"
# ═══════════════════════════════════════════════════════════


async def test_non_reattach_ctx_cancels_immediately_on_detach():
    ctx = _ctx(reattach=False)
    ctx.task = await _sleep_task()
    srv._register_live_run(ctx)

    ctx.detach()
    await asyncio.sleep(0.01)

    assert ctx.task.cancelled(), "旧客户端断开后任务没有按旧语义取消"
    assert ctx.expiry_task is None, "旧客户端不该有重连宽限"


async def test_old_client_run_is_not_advertised_to_new_pages():
    ctx = _ctx(reattach=False)
    ctx.task = await _sleep_task()
    srv._register_live_run(ctx)
    try:
        ws = _FakeWS()
        await srv._send_run_state(ws, "s1")

        state = ws.sent[-1]
        assert state["type"] == "run_state"
        assert state["running"] is False, "不可接管的运行被谎报成正在跑"
        assert state["run"] is None
        assert ctx.ws is not ws, "不可接管的运行被新连接抢走了"
    finally:
        ctx.task.cancel()
        with suppress(asyncio.CancelledError):
            await ctx.task


# ═══════════════════════════════════════════════════════════
# 6. bind 回放 run_state：接管运行中 / 回放已完成
# ═══════════════════════════════════════════════════════════


async def test_bind_replays_running_state_and_takes_over_event_stream():
    old_ws = _FakeWS()
    ctx = _ctx(old_ws, task="跑个长任务", interaction="multi")
    ctx.task = await _sleep_task()
    srv._register_live_run(ctx)
    try:
        new_ws = _FakeWS()
        await srv._send_run_state(new_ws, "s1")

        state = new_ws.sent[-1]
        assert state["type"] == "run_state" and state["running"] is True
        assert state["run"]["interaction"] == "multi"
        assert ctx.ws is new_ws, "bind 之后事件出口没有切到新连接"
        # 接管之后事件应该送到新连接
        await ctx.send({"type": "ma_plan", "plan": []})
        assert "ma_plan" in new_ws.types()
        assert old_ws.types() == []
    finally:
        ctx._cancel_expiry()
        ctx.task.cancel()
        with suppress(asyncio.CancelledError):
            await ctx.task


async def test_bind_replays_finished_result_after_disconnect(monkeypatch):
    monkeypatch.setattr(srv, "_RUN_RESULT_TTL_S", 30.0)
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 30.0)
    ctx = _ctx(task="写周报", interaction="chat")
    srv._register_live_run(ctx)
    ctx.detach()
    ctx.finish({"type": "chat_done", "session_id": "run-1", "tokens": 9},
               record={"session_id": "run-1", "output": "周报正文"})

    ws = _FakeWS()
    await srv._send_run_state(ws, "s1")

    state = ws.sent[-1]
    assert state["running"] is False
    assert state["terminal"]["type"] == "chat_done"
    assert state["terminal_record"]["output"] == "周报正文", \
        "刷新页面后拿不到断线期间写完的完整回答"
    ctx._cancel_expiry()
    if ctx.retention_task is not None:
        ctx.retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await ctx.retention_task


async def test_bind_without_a_run_sends_idle_state():
    ws = _FakeWS()

    await srv._send_run_state(ws, "never-ran")

    assert ws.sent[-1] == {
        "type": "run_state", "session_id": "never-ran", "running": False,
        "run": None, "terminal": None, "terminal_record": None,
    }


# ═══════════════════════════════════════════════════════════
# 7. _ws_run(run_ctx=None)：自动创建 ctx，任务仍正常跑完
# ═══════════════════════════════════════════════════════════


class _ChatAgent:
    """只实现 chat 流式路径需要的表面（不联网、不落盘）。"""

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

    async def chat_stream(self, task, images=None, history=None):
        for piece in ("你好", "，世界"):
            yield piece

    def interjection_report(self) -> dict:
        return {}

    def drain_interjections(self) -> list:
        return []


@pytest.fixture
def chat_run_env(monkeypatch):
    """把 _ws_run 的外部依赖替换成替身，只保留执行主流程。"""
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
    monkeypatch.setattr(srv, "_running_tasks", {"count": 0})
    monkeypatch.setattr(srv, "_approval_waiting", {"count": 0})
    monkeypatch.setattr(srv, "_ws_sessions", {})

    async def _save_ok(sid, ws=None, send=None):
        return True

    monkeypatch.setattr(srv, "_save_history_notify", _save_ok)
    return agent


async def test_ws_run_direct_call_auto_creates_ctx_and_streams(chat_run_env):
    """直接调用（不传 run_ctx）也必须正常工作 —— 测试与内部触发走的正是这条路。"""
    ws = _FakeWS()

    await srv._ws_run(ws, "cid", {
        "task": "打个招呼", "session_id": "s1", "interaction": "chat"})

    types = ws.types()
    assert "task_start" in types, f"没有收到 task_start：{types}"
    assert "chat_done" in types, f"没有收到 chat_done：{types}"
    assert types[-1] == "chat_done"
    assert [m["delta"] for m in ws.sent if m["type"] == "chat_chunk"] == ["你好", "，世界"]
    # 自动创建的 ctx 不可接管，结束后必须从注册表注销（否则同会话后续任务被误拒）
    assert srv._live_run_for_sid("s1") is None
    assert srv._running_tasks["count"] == 0


# ═══════════════════════════════════════════════════════════
# 8. ws_endpoint 接线：同会话拒绝 / 断线只解绑 / 旧客户端仍取消
# ═══════════════════════════════════════════════════════════


class _EndpointWS:
    """喂给 ws_endpoint 的内存假 WebSocket（不启动 uvicorn / TestClient）。"""

    def __init__(self, messages: list[dict]) -> None:
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.sent: list[dict] = []
        self.accepted = False
        self._messages = list(messages)

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.accepted = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(dict(data))

    async def receive_json(self) -> dict:
        if not self._messages:
            raise srv.WebSocketDisconnect()
        return self._messages.pop(0)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]


@pytest.fixture
def endpoint_env(monkeypatch):
    """隔离 ws_endpoint 的进程级表 + 旁路广播（真实 endpoint 实现不变）。"""
    monkeypatch.setattr(srv, "_auth_token", lambda: "")
    monkeypatch.setattr(srv, "_ws_clients", {"all": []})
    monkeypatch.setattr(srv, "_ws_sessions", {})
    monkeypatch.setattr(srv, "_ws_identity", {})
    monkeypatch.setattr(srv, "_ws_tasks", {})
    monkeypatch.setattr(srv, "_live_runs", {})
    monkeypatch.setattr(srv, "_pending_approvals", {})
    monkeypatch.setattr(srv, "_ws_approvals", {})
    monkeypatch.setattr(srv, "_RUN_DETACH_GRACE_S", 30.0)
    monkeypatch.setattr(srv, "_presence", SimpleNamespace(
        register=lambda *_a, **_k: None,
        bind=lambda *_a, **_k: False,
        unregister=lambda *_a, **_k: None,
    ))

    async def _no_presence(**kw):
        return None

    monkeypatch.setattr(srv, "_broadcast_presence", _no_presence)


def _capture_ctxs(monkeypatch) -> list:
    """包一层 srv.LiveRun，收集 ws_endpoint 内部创建的 ctx（事后断言用）。

    任务正常结束/被取消后 ctx 可能已被清理出注册表；测试要检查的正是"它到底
    有没有被取消/解绑"，所以直接持有实例更可靠。
    """
    created: list = []
    real = srv.LiveRun

    def _factory(*args, **kwargs):
        ctx = real(*args, **kwargs)
        created.append(ctx)
        return ctx

    monkeypatch.setattr(srv, "LiveRun", _factory)
    return created


async def test_endpoint_disconnect_keeps_reattach_run(endpoint_env, monkeypatch):
    """新前端（reattach:true）断开：只解绑，任务继续跑。"""
    gate = asyncio.Event()
    started: list[dict] = []

    async def fake_run(sock, client_id, data, run_ctx=None):
        started.append(data)
        try:
            await gate.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(srv, "_ws_run", fake_run)
    created = _capture_ctxs(monkeypatch)
    ws = _EndpointWS([{"action": "run", "task": "长任务",
                       "session_id": "s1", "reattach": True}])

    await srv.ws_endpoint(ws)

    ctx = created[0]
    assert ctx.reattach_supported is True
    assert ctx.ws is None, "断开后 ctx 仍指向死连接"
    assert not ctx.task.done(), "新前端断线后任务被取消了（应当保活）"
    # 收尾：结束假任务，别留下 grace 计时
    ctx._cancel_expiry()
    gate.set()
    await asyncio.wait_for(ctx.task, timeout=2)


async def test_endpoint_disconnect_cancels_old_client_run(endpoint_env, monkeypatch):
    """旧客户端（不带 reattach）断开：保持旧语义，立即取消。"""
    gate = asyncio.Event()

    async def fake_run(sock, client_id, data, run_ctx=None):
        try:
            await gate.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(srv, "_ws_run", fake_run)
    created = _capture_ctxs(monkeypatch)
    ws = _EndpointWS([{"action": "run", "task": "x", "session_id": "s1"}])

    await srv.ws_endpoint(ws)
    await asyncio.sleep(0.01)

    ctx = created[0]
    assert ctx.reattach_supported is False
    assert ctx.task.cancelled(), "旧客户端断线后任务没有取消（破坏了旧行为）"
    # 被取消的运行没有终态可回放，必须从注册表清掉（否则同会话后续任务被误拒）
    assert srv._live_run_for_sid("s1") is None
    gate.set()


async def test_endpoint_rejects_a_second_run_in_the_same_session(
        endpoint_env, monkeypatch):
    """同会话已有任务在跑 → 明确拒绝，而不是静默顶掉前一个任务。"""
    gate = asyncio.Event()
    started: list[dict] = []

    async def fake_run(sock, client_id, data, run_ctx=None):
        started.append(data)
        try:
            await gate.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(srv, "_ws_run", fake_run)
    created = _capture_ctxs(monkeypatch)
    ws = _EndpointWS([
        {"action": "run", "task": "第一个", "session_id": "s1", "reattach": True},
        {"action": "run", "task": "第二个", "session_id": "s1", "reattach": True},
    ])

    await srv.ws_endpoint(ws)

    errs = [m for m in ws.sent if m.get("type") == "task_error"]
    assert errs and "已有任务正在执行" in errs[-1]["error"]
    assert len(created) == 1, "同会话第二个 run 也创建了执行上下文"
    ctx = created[0]
    assert ctx.task is not None and not ctx.task.done()
    # 收尾：放行第一个任务，确认只真正执行了它
    ctx._cancel_expiry()
    gate.set()
    await asyncio.wait_for(ctx.task, timeout=2)
    assert len(started) == 1 and started[0]["task"] == "第一个"


# ═══════════════════════════════════════════════════════════
# 9. 审批：重连接管后弹窗必须发往当前连接（仍 fail-closed）
# ═══════════════════════════════════════════════════════════


def _approval_env(monkeypatch):
    monkeypatch.setattr(srv, "_pending_approvals", {})
    monkeypatch.setattr(srv, "_ws_approvals", {})
    monkeypatch.setattr(srv, "_ws_clients", {"all": []})
    monkeypatch.setattr(srv, "_ws_sessions", {})
    monkeypatch.setattr(srv, "_record_and_notify", lambda _sid, _ev: None)
    monkeypatch.setattr(srv, "_external_approval_available", lambda: False)
    return SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(
        approval_timeout_seconds=5.0,
        approval_timeout_action="reject",
        release_slot_on_approval_wait=False,
    )))


async def test_approval_after_reattach_goes_to_the_new_connection(monkeypatch):
    """断线被新连接接管之后才触发的审批：弹窗要发到新连接（否则用户明明在线
    也批不了），待决记录要及时更新归属。"""
    agent = _approval_env(monkeypatch)
    ctx = _ctx(reattach=True)
    srv._register_live_run(ctx)
    old_ws = ctx.ws
    new_ws = _FakeWS()
    ctx.detach()
    assert ctx.attach(new_ws, "cid-new") is True

    cb = srv.make_approval_callback(
        old_ws, "cid-old", "run-1", "s1", agent,
        ws_getter=lambda: ctx.ws,
        client_id_getter=lambda: ctx.client_id,
    )
    task = asyncio.create_task(
        cb("terminal", {"command": "rm -rf x"}, "dangerous", "高危"))
    try:
        await asyncio.sleep(0.01)
        assert "approval_request" in new_ws.types(), \
            "接管后的审批弹窗仍发往了已断开的旧连接"
        assert old_ws.types() == []
        req = new_ws.sent[-1]
        assert srv._pending_approvals[req["approval_id"]]["client_id"] == "cid-new"
        srv._ws_approvals[req["approval_id"]].set_result(
            {"approved": True, "arguments": None, "comment": ""})
        out = await asyncio.wait_for(task, timeout=2)
        assert out["approved"] is True, "新连接上的批准没有生效"
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_approval_while_run_is_detached_still_fails_closed(monkeypatch):
    """没有连接能收弹窗 → 当场按拒绝收尾（审批 fail-closed 不变，不允许"等等看"）。"""
    agent = _approval_env(monkeypatch)
    ctx = _ctx(reattach=True)
    srv._register_live_run(ctx)
    ctx.detach()                       # 当前没有任何连接承载它

    cb = srv.make_approval_callback(
        ctx.ws, "cid-old", "run-1", "s1", agent,
        ws_getter=lambda: ctx.ws,
        client_id_getter=lambda: ctx.client_id,
    )
    out = await asyncio.wait_for(
        cb("terminal", {"command": "x"}, "dangerous", "高危"), timeout=2)

    assert out["approved"] is False, f"送不到弹窗却没有按拒绝收尾：{out!r}"
    assert "没有可送达" in out["comment"]
    assert srv._pending_approvals == {} and srv._ws_approvals == {}


# ═══════════════════════════════════════════════════════════
# 10. 接线契约（源码级，防字段/调用漂移）
# ═══════════════════════════════════════════════════════════


async def test_start_ws_run_falls_back_for_three_arg_test_doubles(monkeypatch):
    """测试里把 _ws_run 换成三参数替身时不能炸，且不能留下无人接管的 ctx。"""
    started: list[dict] = []

    async def fake_run(ws, client_id, data):
        started.append(data)

    monkeypatch.setattr(srv, "_ws_run", fake_run)
    ctx = srv.LiveRun(_FakeWS(), "c1", "s1", {"task": "x"},
                      reattach_supported=True)
    srv._register_live_run(ctx)

    task = srv._start_ws_run(_FakeWS(), "c1", {"task": "x"}, ctx)
    await task

    assert srv._live_run_for_sid("s1") is None, \
        "替身接管不了 ctx，却没有把它从注册表里清掉（会误拒后续任务）"
    assert started and started[0]["task"] == "x"


async def test_ctx_is_cleaned_up_when_run_task_never_reaches_finish(monkeypatch):
    """主流程在 try 之前就异常/被取消时，ctx 不能永远留在注册表里。

    这类 ctx 的 ``running`` 一算就是 False，但留在表里会让重连页面收到一条
    "既没在跑、也没结果"的状态；done 回调必须负责清掉它。
    """
    async def broken_run(ws, client_id, data, run_ctx=None):
        raise RuntimeError("在进入主流程之前就炸了")

    monkeypatch.setattr(srv, "_ws_run", broken_run)
    ctx = srv.LiveRun(_FakeWS(), "c1", "s1", {"task": "x"},
                      reattach_supported=True)
    srv._register_live_run(ctx)
    task = srv._start_ws_run(_FakeWS(), "c1", {"task": "x"}, ctx)
    with pytest.raises(RuntimeError):
        await task
    await asyncio.sleep(0)             # done 回调是 call_soon 调度的
    assert srv._live_run_for_sid("s1") is None


def test_ws_endpoint_wiring_covers_run_stop_bind_and_finally():
    import inspect

    src = inspect.getsource(srv.ws_endpoint)
    # run：同会话活跃运行拒绝 + 创建 LiveRun + 把 ctx 传给 _ws_run
    assert "_live_run_for_sid" in src and "已有任务正在执行" in src
    assert "LiveRun(" in src and "_start_ws_run(" in src
    assert "reattach_supported=bool(data.get(\"reattach\"))" in src
    # stop：找不到本连接的任务时按当前会话找 ctx
    assert "_live_run_for_sid(_ws_session_of(ws))" in src
    # bind：回放 run_state
    assert "_send_run_state" in src
    # finally：先解绑（不取消），旧客户端/无 ctx 才取消
    assert "_live_run_for_ws" in src and "current.detach()" in src
    assert "_live_run_for_task" in src


def test_ws_run_sends_everything_through_the_ctx():
    import inspect

    src = inspect.getsource(srv._ws_run)
    assert "run_ctx: LiveRun | None = None" in src
    assert "await run_ctx.send(" in src
    assert "ws.send_json(" not in src, "仍有事件绕过 ctx 直发，断线时会卡住/丢失"
    assert "run_ctx.finish(" in src
    # 审批回调按"当前连接"取发送目标：接管后新触发的审批不能发往旧 socket
    assert "ws_getter=lambda: run_ctx.ws" in src
    assert "client_id_getter=lambda: run_ctx.client_id" in src
    # 既有约束（tests/server/test_ws_interject.py 也盯着）：登记在 try 内、注销在 finally
    try_at = src.index("\n    try:\n")
    assert src.index("_live_runs[chat_sid] = agent") > try_at
    finally_at = src.index("\n    finally:\n")
    assert finally_at > try_at


def test_frontend_ws_ts_declares_reattach_and_run_state_replay():
    root = Path(__file__).resolve().parents[2]
    src = (root / "web" / "src" / "ws.ts").read_text(encoding="utf-8")
    assert "reattach: true" in src, "sendRun 没有声明 reattach —— 服务端不会保活"
    assert "RUN_RECONNECT_GRACE_MS" in src and "150000" in src
    assert "reconnectFailTimer" in src
    assert "case 'run_state'" in src
    assert "terminal_record" in src
    assert "连接中断，任务仍在服务器继续" in src


def test_reattach_opt_in_defaults_off_for_old_clients():
    """只有 run 里带 reattach:true 才保活；缺省必须是旧行为。"""
    ctx_old = srv.LiveRun(_FakeWS(), "c1", "s1", {"task": "x"})
    ctx_new = srv.LiveRun(_FakeWS(), "c1", "s1", {"task": "x"},
                          reattach_supported=True)
    assert ctx_old.reattach_supported is False
    assert ctx_new.reattach_supported is True
