"""审批模式的**端到端**链路 —— 弹窗必达、等待必结、断开必收尾。

## 为什么单独测这一条链

审批是唯一"前端不参与就必然失败"的功能：后端在这里**阻塞等待**，
任何一环断掉的表现都是"任务卡住不动"，而用户完全看不出是自己在被等待。

此前这条链路上有三处会静默断掉：

1. **弹窗送不到就没救了**：``ws.send_json`` 抛异常时（连接刚断、代理掐流），
   回调既不返回也不上报，任务一路挂到 300 秒超时。
2. **连接断开后待决审批无人终结**：回调要等满整个超时才返回，
   用户换个窗口看到的就是"它卡住了"；等待计数与并发槽也跟着虚占。
3. **审批模式只在建克隆那一刻生效**：顶栏改成「询问」后，已经在用的
   会话克隆仍按老模式跑 —— 界面上写着"询问"，实际一直自动放行。

测试走的是**真实实现**（``make_approval_callback`` + 真实 WebSocket），
不是把逻辑抄一份到测试里 —— 抄一份只能证明"我抄对了"。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from automind import server as srv  # noqa: E402
from automind.core.types import (  # noqa: E402
    LLMResponse,
    PermissionDecision,
    PermissionTier,
    ToolCall,
    ToolResult,
)
from automind.planning.react_executor import ReActExecutor  # noqa: E402

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _AskPermissions:
    """恒返回 ask_user 的权限引擎 —— 直击审批分支。"""

    approval_mode = "ask"

    def check(self, tool_name, tier, params=None):
        return PermissionDecision.ASK_USER, f"{tool_name} 需要人工批准"


class _Tool:
    name = "terminal"
    description = "fake terminal"
    permission_tier = PermissionTier.DANGEROUS
    parameters = {"properties": {"command": {"type": "string"}}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object",
                               "properties": self.parameters["properties"],
                               "required": []}}


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_names(self):
        return ["terminal"]

    def list_all(self):
        return [_Tool()]

    def get(self, name):
        if name != "terminal":
            raise KeyError(name)
        return _Tool()

    async def dispatch(self, name, **kw):
        self.calls.append((name, kw))
        return ToolResult(tool_name=name, success=True, output={"ran": True})


class _OneShotLLM:
    """只请求一次 terminal 调用，然后收尾。"""

    def __init__(self) -> None:
        self._n = 0

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self._n += 1
        if self._n == 1:
            return LLMResponse(
                text="要执行 rm -rf /tmp/x",
                tool_calls=[ToolCall(id="1", name="terminal",
                                     arguments={"command": "rm -rf /tmp/x"})])
        return LLMResponse(text="完成")


class _ExecCfg:
    approval_timeout_seconds = 2.0
    approval_timeout_action = "reject"
    release_slot_on_approval_wait = True


class _StubAgent:
    """只提供审批回调需要的那几个属性。"""

    def __init__(self) -> None:
        self.config = type("C", (), {"execution": _ExecCfg()})()
        self.permissions = type("P", (), {"approval_mode": "ask"})()
        self.approval_callback = None
        self.event_sink = None
        self.session_id = ""
        self.llm = object()          # 非 None，越过 _ws_run 的就绪检查
        self._interaction = type("I", (), {"value": "chat"})()
        self._mode = None
        self._is_session_clone = True


def _client() -> TestClient:
    # 回环来源：ws_endpoint 会做 Origin 校验，TestClient 默认 host 是 "testclient"
    return TestClient(srv.app, client=("127.0.0.1", 51000))


def _stub_agent() -> Any:
    """一个只有审批相关属性的 agent 替身（构造真 Agent 要拉整个环境探测）。"""
    return _StubAgent()


@pytest.fixture
def env(monkeypatch):
    """干净的审批全局状态 + 一个只做"注入真实回调"的 agent。"""
    from collections import OrderedDict
    monkeypatch.setattr(srv, "_running_tasks", {"count": 0})
    monkeypatch.setattr(srv, "_approval_waiting", {"count": 0})
    monkeypatch.setattr(srv, "_ws_approvals", {})
    monkeypatch.setattr(srv, "_pending_approvals", {})
    monkeypatch.setattr(srv, "_ws_sessions", {})
    monkeypatch.setattr(srv, "_MAX_CONCURRENT", 8)
    monkeypatch.setattr(srv, "_session_clones", OrderedDict())
    monkeypatch.setattr(srv, "_pool_enabled", _returns_false)
    monkeypatch.setattr(srv, "_close_clone_later", _noop1)
    monkeypatch.setattr(srv, "_save_active", _noop_kw)
    monkeypatch.setattr(srv, "_quota", type("Q", (), {
        "try_consume_task": staticmethod(lambda: (True, ""))})())
    stub = _StubAgent()
    monkeypatch.setattr(srv, "get_agent", _constant(stub))
    return stub


def _returns_false() -> bool:
    return False


def _noop1(_a: Any) -> None:
    return None


def _noop_kw(**_k: Any) -> None:
    return None


def _constant(value: Any):
    def _get(*_a: Any, **_k: Any) -> Any:
        return value
    return _get


def _patch_ws_run(monkeypatch, registry: _Registry):
    """把 _ws_run 换成"最小但真实"的链路：门控 → 真实审批回调 → ReAct 执行。

    这里**不复制**审批逻辑：回调由 ``make_approval_callback``（生产实现）
    构造，测试只负责把整条任务跑起来。
    """

    async def fake_ws_run(ws, client_id, data):
        agent = srv.get_agent()
        slots: list[str] = []
        # 真实实现：注册 future / 发 approval_request / 等回答 / 超时与断连收尾
        agent.approval_callback = srv.make_approval_callback(
            ws, client_id, "sess-" + client_id, data.get("session_id") or "default",
            agent,
            release_slot=lambda: slots.append("release"),
            reclaim_slot=lambda: slots.append("reclaim"),
        )
        try:
            ex = ReActExecutor(
                _OneShotLLM(), registry, max_iterations=3,
                permissions=_AskPermissions(),
                approval_cb=agent.approval_callback)
            text = await ex.run(data.get("task") or "任务")
            fake_ws_run.last_executor = ex
            await ws.send_json({"type": "task_complete", "output": text})
        finally:
            fake_ws_run.slots = slots
            agent.approval_callback = None

    fake_ws_run.last_executor = None
    fake_ws_run.slots = []
    monkeypatch.setattr(srv, "_ws_run", fake_ws_run)
    return fake_ws_run


def _drain(ws, want: str, tries: int = 30) -> dict:
    """读到指定类型的消息（跳过 task_start 等噪声）。"""
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("type") == want:
            return msg
    raise AssertionError(f"没有收到 {want} 消息")


# ═══════════════════════════════════════════════════════════
# 1. 弹窗必达 + 回答生效
# ═══════════════════════════════════════════════════════════


class TestApprovalRoundTrip:
    def test_approval_request_reaches_the_client(self, monkeypatch, env):
        reg = _Registry()
        _patch_ws_run(monkeypatch, reg)
        with _client() as c, c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"action": "run", "task": "删个临时目录",
                          "session_id": "default"})
            req = _drain(ws, "approval_request")
            assert req["tool"] == "terminal"
            assert req["tier"] == "dangerous"
            assert req["approval_id"]
            assert req["timeout_s"] > 0, "没给等待上限，前端无法倒计时"
            # 弹窗里要能看到**要执行什么** —— 只给工具名等于让用户盲批
            assert "command" in req["editable"]
            assert "rm -rf /tmp/x" in req["editable"]["command"]
            ws.send_json({"action": "approval_response",
                          "approval_id": req["approval_id"],
                          "approved": False})
            _drain(ws, "task_complete")

    def test_approved_action_actually_runs(self, monkeypatch, env):
        reg = _Registry()
        _patch_ws_run(monkeypatch, reg)
        with _client() as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"action": "run", "task": "跑", "session_id": "s1"})
            req = _drain(ws, "approval_request")
            ws.send_json({"action": "approval_response",
                          "approval_id": req["approval_id"], "approved": True})
            _drain(ws, "task_complete")
        assert reg.calls == [("terminal", {"command": "rm -rf /tmp/x"})], \
            "批准了却没有真的执行"

    def test_denied_action_is_not_executed(self, monkeypatch, env):
        reg = _Registry()
        _patch_ws_run(monkeypatch, reg)
        with _client() as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"action": "run", "task": "跑", "session_id": "s2"})
            req = _drain(ws, "approval_request")
            ws.send_json({"action": "approval_response",
                          "approval_id": req["approval_id"], "approved": False,
                          "comment": "不许删"})
            _drain(ws, "task_complete")
        assert reg.calls == [], "用户拒绝了，动作却仍然执行了"

    def test_modified_arguments_are_used(self, monkeypatch, env):
        """「修改后批准」必须真的改用用户给的参数。"""
        reg = _Registry()
        _patch_ws_run(monkeypatch, reg)
        with _client() as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"action": "run", "task": "跑", "session_id": "s3"})
            req = _drain(ws, "approval_request")
            ws.send_json({"action": "approval_response",
                          "approval_id": req["approval_id"], "approved": True,
                          "arguments": {"command": "rm -rf /tmp/x-safe"},
                          "comment": "改了范围"})
            _drain(ws, "task_complete")
        assert reg.calls == [("terminal", {"command": "rm -rf /tmp/x-safe"})], \
            "批准时改的参数没有生效"

    def test_late_answer_gets_a_stale_receipt(self, monkeypatch, env):
        """审批已经结束/超时后用户才点 —— 必须回执，不能静默丢弃。"""
        _patch_ws_run(monkeypatch, _Registry())
        with _client() as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"action": "approval_response",
                          "approval_id": "nonexistent", "approved": True})
            msg = _drain(ws, "approval_stale")
            assert "已经结束" in msg["message"]

    def test_approval_timeout_reaches_the_client(self, monkeypatch, env):
        """超时必须明确告知 —— 静默超时=弹窗还挂着，用户以为还在等他。"""
        env.config.execution.approval_timeout_seconds = 0.4
        _patch_ws_run(monkeypatch, _Registry())
        with _client() as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"action": "run", "task": "跑", "session_id": "s5"})
            _drain(ws, "approval_request")
            msg = _drain(ws, "approval_timeout", tries=60)
            assert msg["approved"] is False
            assert "未响应" in msg["message"]
            _drain(ws, "task_complete")


# ═══════════════════════════════════════════════════════════
# 2. 连接断开 / 弹窗送不到 —— 不许挂到超时
# ═══════════════════════════════════════════════════════════


class TestNoDanglingApproval:
    def test_pending_approval_is_settled_when_the_socket_closes(
            self, monkeypatch, env):
        """连接断开后待决审批**立刻**按拒绝收尾，而不是等满超时。"""
        done: dict = {}

        async def scenario():
            fut = asyncio.get_event_loop().create_future()
            srv._pending_approvals["abc123"] = {
                "future": fut, "client_id": "cid", "session_id": "sess",
                "tool": "terminal", "tier": "dangerous", "asked_at": time.time()}
            srv._ws_approvals["abc123"] = fut
            t0 = time.perf_counter()
            srv._fail_pending_approvals("cid", "连接中断，按拒绝处理")
            done["elapsed"] = time.perf_counter() - t0
            done["result"] = fut.result() if fut.done() else None
            done["pending"] = dict(srv._pending_approvals)
            done["approvals"] = dict(srv._ws_approvals)

        asyncio.run(scenario())
        assert done["result"] is not None, "断链后 future 没有被终结（会挂到超时）"
        assert done["result"]["approved"] is False, "断链必须 fail-closed"
        assert done["elapsed"] < 0.5, f"收尾用了 {done['elapsed']:.2f}s（应当立即）"
        assert done["pending"] == {} and done["approvals"] == {}, \
            "待决审批表没有清干净（等待计数会永久偏差）"

    def test_other_clients_approvals_are_left_alone(self, monkeypatch, env):
        """只终结自己连接上的审批 —— 别的会话正在等的不能被误杀。"""
        kept = asyncio.new_event_loop().create_future()
        srv._pending_approvals["other"] = {
            "future": kept, "client_id": "someone-else", "session_id": "s",
            "tool": "t", "tier": "safe", "asked_at": time.time()}
        srv._ws_approvals["other"] = kept
        srv._fail_pending_approvals("cid", "reason")
        assert not kept.done(), "误杀了别的连接的待决审批"
        srv._pending_approvals.clear()
        srv._ws_approvals.clear()

    def test_undeliverable_prompt_denies_immediately(self, monkeypatch, env):
        """弹窗发不出去（连接已断）→ 当场拒绝，不许挂到超时。"""
        async def scenario():
            class _Dead:
                async def send_json(self, payload):
                    raise RuntimeError("socket closed")

            agent = _StubAgent()
            agent.config.execution.approval_timeout_seconds = 30.0
            cb = srv.make_approval_callback(
                _Dead(), "cid", "sess", "default", agent)
            t0 = time.perf_counter()
            out = await cb("terminal", {"command": "x"}, "dangerous", "高危")
            return out, time.perf_counter() - t0

        out, elapsed = asyncio.run(scenario())
        assert out["approved"] is False
        assert "无法送达" in out["comment"]
        assert elapsed < 1.0, f"送不出去还等了 {elapsed:.1f}s"
        assert srv._pending_approvals == {}, "送不出去仍留在待决表里"

    def test_slot_is_returned_after_a_denied_prompt(self, monkeypatch, env):
        """审批结束必须把并发槽收回来，否则并发计数会一路漏下去。"""
        calls: list[str] = []

        async def scenario():
            class _WS:
                async def send_json(self, payload):
                    pass

            agent = _StubAgent()
            agent.config.execution.approval_timeout_seconds = 0.3
            cb = srv.make_approval_callback(
                _WS(), "cid", "sess", "default", agent,
                release_slot=lambda: calls.append("release"),
                reclaim_slot=lambda: calls.append("reclaim"))
            return await cb("t", {}, "safe", "r")

        out = asyncio.run(scenario())
        # 与"送不到"分支同形状的结构化结果（都经 ApprovalOutcome.normalize 归一化）
        assert out["approved"] is False, f"超时应当按拒绝收尾，实际 {out!r}"
        assert calls == ["release", "reclaim"], calls


# ═══════════════════════════════════════════════════════════
# 3. 多窗口：审批弹窗补发到同会话的其它标签页
# ═══════════════════════════════════════════════════════════


class TestMultiTabDelivery:
    def test_other_tab_of_same_session_also_gets_the_prompt(
            self, monkeypatch, env):
        _patch_ws_run(monkeypatch, _Registry())
        with _client() as c, c.websocket_connect("/ws") as ws1:
            ws1.receive_json()
            with c.websocket_connect("/ws") as ws2:
                ws2.receive_json()
                ws1.send_json({"action": "run", "task": "跑",
                               "session_id": "default"})
                req1 = _drain(ws1, "approval_request")
                req2 = _drain(ws2, "approval_request")
                assert req2["approval_id"] == req1["approval_id"], \
                        "同会话的另一个标签页没有收到审批弹窗"
                # 在**第二个**窗口回答，也应当生效
                ws2.send_json({"action": "approval_response",
                               "approval_id": req2["approval_id"],
                               "approved": True})
                _drain(ws1, "task_complete")

    def test_session_binding_moves_with_the_request(self, monkeypatch, env):
        with _client() as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            sock = srv._ws_clients["all"][-1]
            assert srv._ws_session_of(sock) == "default"


# ═══════════════════════════════════════════════════════════
# 4. 审批模式改动必须落到已存在的会话上
# ═══════════════════════════════════════════════════════════


class TestApprovalModePropagation:
    def test_mode_change_syncs_existing_session_clones(self, monkeypatch, env):
        """界面上改成「询问」，已经在用的会话也必须跟着变。

        此前只改了全局 agent，会话克隆在创建时就固化了模式 ——
        用户看到的是"我明明设成询问了，它怎么没问我"。
        """
        class _Perms:
            def __init__(self, mode):
                self.approval_mode = mode

        class _Cfg:
            class execution:      # noqa: N801
                approval_mode = "auto"

        class _Agent:
            def __init__(self, mode):
                self.permissions = _Perms(mode)
                self.config = _Cfg()

        base, clone = _Agent("auto"), _Agent("auto")
        monkeypatch.setattr(srv, "get_agent", _constant(base))
        monkeypatch.setattr(srv, "_session_clones", {"sess": clone})

        with _client() as c:
            r = c.post("/api/config/approval", json={"approval_mode": "ask"})
        assert r.status_code == 200
        assert r.json()["approval_mode"] == "ask"
        assert base.permissions.approval_mode == "ask"
        assert clone.permissions.approval_mode == "ask", \
            "已存在的会话克隆没跟上审批模式"
        assert clone.config.execution.approval_mode == "ask"

    def test_acquire_run_agent_syncs_mode(self, monkeypatch, env):
        class _Perms:
            def __init__(self, mode):
                self.approval_mode = mode

        class _Agent:
            def __init__(self, mode):
                self.permissions = _Perms(mode)
                self._interaction = None
                self._mode = None
                self.approval_callback = None
                self.event_sink = None

            def clone_for_session(self):
                return _Agent(self.permissions.approval_mode)

        base = _Agent("ask")
        got = srv._acquire_run_agent(base, "brand-new-session")
        assert got.permissions.approval_mode == "ask", \
            "新会话克隆没有继承当前的审批模式"

    def test_mode_change_is_reported_in_status(self, env):
        """改完之后状态接口必须回显新模式 —— 否则界面会显示成旧值。"""
        with _client() as c:
            body = c.post("/api/config/approval",
                          json={"approval_mode": "ask"}).json()
            assert body["approval_mode"] == "ask"
            assert env.permissions.approval_mode == "ask"

    def test_health_exposes_pending_approval_count(self):
        with _client() as c:
            body = c.get("/api/health").json()
        assert "approval_pending" in body
        assert "approval_waiting" in body


# ═══════════════════════════════════════════════════════════
# 5. 前后端契约（防止字段漂移导致"弹窗打不开"）
# ═══════════════════════════════════════════════════════════


class TestFrontendContract:
    @property
    def ws_ts(self) -> str:
        from pathlib import Path
        return (Path(__file__).resolve().parents[2] / "web" / "src" / "ws.ts"
                ).read_text(encoding="utf-8")

    def test_frontend_handles_every_approval_event(self):
        src = self.ws_ts
        for ev in ("approval_request", "approval_timeout", "approval_stale",
                   "approval_failed", "react_no_progress"):
            assert f"case '{ev}'" in src, f"前端没有处理 {ev} 事件"

    def test_frontend_sends_arguments_for_modify(self):
        src = self.ws_ts
        assert "action: 'approval_response'" in src
        assert "arguments" in src, "「修改后批准」的参数没有回传通道"

    def test_modal_reads_the_fields_the_backend_sends(self):
        from pathlib import Path
        modal = (Path(__file__).resolve().parents[2] / "web" / "src"
                 / "components" / "modals" / "ApprovalModal.tsx").read_text(encoding="utf-8")
        for field in ("editable", "timeoutS", "askedAt", "params", "reason", "tier"):
            assert field in modal, f"弹窗没有使用 {field}"

    def test_approval_modal_is_mounted(self):
        from pathlib import Path
        app = (Path(__file__).resolve().parents[2] / "web" / "src"
               / "App.tsx").read_text(encoding="utf-8")
        assert "ApprovalModal" in app and "<ApprovalModal />" in app, \
            "弹窗组件没有被挂载（永远弹不出来）"


def test_json_payload_stays_serializable():
    """审批请求里的参数可能含 Path / 枚举 —— 必须能 JSON 化，否则弹窗发不出去。"""
    from pathlib import Path as _P

    from automind.core.types import PermissionTier as _PT
    payload = {"path": _P("/tmp/x"), "tier": _PT.SAFE,
               "nested": {"a": [1, {"b": _P("y")}]}}
    out = srv._jsonable(payload)
    json.dumps(out)          # 不抛异常即合格
    assert isinstance(out["path"], str)
