"""v1.11.11 服务端：待办端点 + ``user_answer`` / ``compact`` 两个 WS 动作。

这一版新增的交互都有"必须给出回执"的性质：

* 待办清单是右栏常驻区块 —— 端点只读/清空，不能顺带触发任何任务逻辑；
* 提问弹窗的答案可能来自**另一个标签页**：作答后要通知同会话的其它窗口
  关闭弹窗，否则它们会一直挂着一个已经失效的问题；未知/已结束的问题必须回
  ``user_question_stale``，静默丢弃会让用户以为"我明明答了"；
* 手动压缩只作用于该会话**已有**的 Agent（正在跑 / 复用中的克隆）。
  没有就明确报错 —— 临时重建一个空 Agent 假装压缩成功，比不压更糟。

data 层（todo_store / user_questions）由核心层提供，这里用替身验证服务端的
装配与回执语义，不重复测核心层自己的校验逻辑。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

srv = pytest.importorskip("automind.server")

_ROUTES_SNAPSHOT = Path(__file__).with_name("routes_snapshot.txt")


class _WS:
    """只记账的假连接（与 test_ws_interject.py 同款）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """这几个表是进程级全局状态，测试之间必须互不污染。"""
    monkeypatch.setattr(srv, "_live_runs", {})
    monkeypatch.setattr(srv, "_session_clones", OrderedDict())
    monkeypatch.setattr(srv, "_ws_sessions", {})
    monkeypatch.setattr(srv, "_ws_clients", {"all": []})


@pytest.fixture
def client(monkeypatch):
    """真实 app（验证路由/action 分发），但不重建真实 Agent、不要求鉴权。"""
    monkeypatch.setattr(srv, "get_agent", lambda: SimpleNamespace())
    monkeypatch.setattr(srv, "_auth_token", lambda: "")
    return TestClient(srv.app, client=("127.0.0.1", 50000))


# ═══════════════════════════════════════════════════════════
# 1. GET / DELETE /api/todo
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def todo_state(monkeypatch):
    """todo_store 的内存替身：按会话隔离，行为与核心层契约一致。"""
    from automind.core import todo_store

    state: dict[str, list[dict]] = {}

    def _load(session_id):
        return [dict(t) for t in state.get(session_id, [])]

    def _clear(session_id):
        state.pop(session_id, None)

    def _counts(todos):
        out = {"pending": 0, "in_progress": 0, "completed": 0}
        for t in todos:
            status = t.get("status")
            if status in out:
                out[status] += 1
        return out

    monkeypatch.setattr(todo_store, "load", _load)
    monkeypatch.setattr(todo_store, "clear", _clear)
    monkeypatch.setattr(todo_store, "counts", _counts)
    return state


def test_todo_get_returns_todos_and_counts(client, todo_state):
    todo_state["s1"] = [
        {"id": "t1", "content": "写测试", "status": "pending"},
        {"id": "t2", "content": "跑测试", "status": "in_progress"},
    ]

    r = client.get("/api/todo", params={"session_id": "s1"})

    assert r.status_code == 200
    body = r.json()
    assert [t["id"] for t in body["todos"]] == ["t1", "t2"]
    assert body["counts"] == {"pending": 1, "in_progress": 1, "completed": 0}


def test_todo_get_defaults_to_default_session(client, todo_state):
    todo_state["default"] = [
        {"id": "t1", "content": "收尾", "status": "completed"}]

    body = client.get("/api/todo").json()

    assert body["todos"][0]["id"] == "t1"
    assert body["counts"]["completed"] == 1


def test_todo_get_empty_store_is_not_an_error(client, todo_state):
    r = client.get("/api/todo", params={"session_id": "never-used"})

    assert r.status_code == 200
    assert r.json() == {
        "todos": [],
        "counts": {"pending": 0, "in_progress": 0, "completed": 0},
    }


def test_todo_delete_clears_only_the_requested_session(client, todo_state):
    todo_state["s1"] = [{"id": "t1", "content": "x", "status": "pending"}]
    todo_state["s2"] = [{"id": "t2", "content": "y", "status": "pending"}]

    r = client.delete("/api/todo", params={"session_id": "s1"})

    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert "s1" not in todo_state, "没有清空请求的会话"
    assert "s2" in todo_state, "把别的会话的待办一起清掉了"
    assert client.get("/api/todo", params={"session_id": "s1"}).json()["todos"] == []


def test_todo_routes_are_declared_in_the_snapshot():
    """新增端点必须显式登记进对外契约快照（与 route_inventory 测试同源）。"""
    snapshot = {
        ln.strip()
        for ln in _ROUTES_SNAPSHOT.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }
    assert {"GET /api/todo", "DELETE /api/todo"} <= snapshot

    registered: set[str] = set()
    for route in srv.app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or []:
            if method not in ("HEAD", "OPTIONS"):
                registered.add(f"{method} {path}")
    assert {"GET /api/todo", "DELETE /api/todo"} <= registered


# ═══════════════════════════════════════════════════════════
# 2. WS action=user_answer
# ═══════════════════════════════════════════════════════════


def _resolved_record(question_id: str = "q1",
                     session_id: str = "s1",
                     answers: list | None = None) -> dict:
    return {
        "question_id": question_id,
        "session_id": session_id,
        "questions": [{"id": "q1", "question": "选哪个？",
                       "options": [{"label": "A"}, {"label": "B"}]}],
        "answers": answers or [{"id": "q1", "selected": ["A"]}],
        "created": 0.0,
        "timeout_s": 300.0,
    }


async def test_user_answer_success_notifies_other_tabs(monkeypatch):
    """成功：回给发起连接，并让同会话的其它标签页关掉弹窗。"""
    from automind.core import user_questions

    ws, other_tab = _WS(), _WS()
    srv._ws_sessions["s1"] = [ws, other_tab]
    srv._ws_clients["all"] = [ws, other_tab]
    seen: dict = {}

    def _resolve(qid, answers):
        seen["qid"], seen["answers"] = qid, answers
        return _resolved_record("q1", "s1")

    monkeypatch.setattr(user_questions, "resolve", _resolve)

    await srv._handle_user_answer(ws, {
        "question_id": "q1",
        "answers": [{"id": "q1", "selected": ["A"]}],
    })

    assert seen["qid"] == "q1" and seen["answers"] == [
        {"id": "q1", "selected": ["A"]}]
    msg = ws.sent[0]
    assert msg["type"] == "user_question_resolved"
    assert msg["question_id"] == "q1"
    assert msg["answers"] == [{"id": "q1", "selected": ["A"]}]
    assert msg["session_id"] == "s1"
    assert other_tab.sent == [msg], "其它标签页没收到关闭弹窗的通知"
    assert len(ws.sent) == 1, "发起连接自己不该再收到一份广播"


async def test_user_answer_stale_when_question_already_finished(monkeypatch):
    """未知 / 已结束：必须回 stale 回执，不能静默。"""
    from automind.core import user_questions

    monkeypatch.setattr(user_questions, "resolve", lambda _qid, _answers: None)
    ws = _WS()

    await srv._handle_user_answer(ws, {
        "question_id": "gone",
        "answers": [{"id": "q1", "selected": ["A"]}],
    })

    assert ws.types() == ["user_question_stale"]
    assert ws.sent[0]["question_id"] == "gone"
    assert "已结束" in ws.sent[0]["message"]


@pytest.mark.parametrize("bad_answers", [
    None,
    [],
    "not-a-list",
    [{"selected": ["A"]}],                      # 缺 id
    [{"id": 1, "selected": ["A"]}],              # id 不是字符串
    [{"id": "q1"}],                              # 缺 selected
    [{"id": "q1", "selected": "A"}],             # selected 不是数组
    [{"id": "q1", "selected": [1]}],             # selected 元素不是字符串
    [{"id": "q1", "selected": [], "custom": 5}],  # custom 不是字符串
])
async def test_user_answer_invalid_structure_is_rejected(monkeypatch, bad_answers):
    """结构不合法 → 直接 stale（说明格式），绝不能拿去 resolve。"""
    from automind.core import user_questions

    calls: list = []
    monkeypatch.setattr(user_questions, "resolve",
                        lambda *_a, **_k: calls.append(_a) or None)
    ws = _WS()

    await srv._handle_user_answer(ws, {
        "question_id": "q1", "answers": bad_answers})

    assert calls == [], "非法回答被送进 resolve 了"
    assert ws.types() == ["user_question_stale"]
    assert "格式" in ws.sent[0]["message"]


async def test_user_answer_custom_text_is_kept(monkeypatch):
    """选了自定义输入：custom 要一并归一化后交给核心层。"""
    from automind.core import user_questions

    captured: dict = {}

    def _resolve(qid, answers):
        captured["answers"] = answers
        return _resolved_record(answers=answers)

    monkeypatch.setattr(user_questions, "resolve", _resolve)
    ws = _WS()

    await srv._handle_user_answer(ws, {
        "question_id": "q1",
        "answers": [{"id": "q1", "selected": ["B"], "custom": "我自己写"}],
    })

    assert captured["answers"] == [
        {"id": "q1", "selected": ["B"], "custom": "我自己写"}]


# ═══════════════════════════════════════════════════════════
# 3. WS action=compact
# ═══════════════════════════════════════════════════════════


class _CompactAgent:
    """只提供 force_compact 的 Agent 替身。"""

    def __init__(self, result: dict | None = None, raises: Exception | None = None):
        self.result = result if result is not None else {
            "ok": True, "scope": "react", "folded": 3, "chars_reclaimed": 1200,
            "est_tokens_saved": 400, "messages": 18, "error": "",
        }
        self.raises = raises
        self.calls = 0

    async def force_compact(self) -> dict:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


async def test_compact_running_agent_returns_full_result():
    agent = _CompactAgent()
    srv._live_runs["s1"] = agent
    ws = _WS()

    await srv._handle_compact(ws, {"session_id": "s1"})

    assert agent.calls == 1
    msg = ws.sent[0]
    assert msg["type"] == "compact_result"
    assert msg["session_id"] == "s1"
    assert msg["ok"] is True
    assert (msg["folded"], msg["chars_reclaimed"], msg["est_tokens_saved"],
            msg["messages"]) == (3, 1200, 400, 18)
    assert msg["scope"] == "react"
    assert "error" not in msg


async def test_compact_falls_back_to_reused_session_clone():
    """任务已结束：仍可用该会话复用中的克隆压缩上下文。"""
    agent = _CompactAgent()
    srv._session_clones["s2"] = agent
    ws = _WS()

    await srv._handle_compact(ws, {"session_id": "s2"})

    assert agent.calls == 1
    assert ws.sent[0]["ok"] is True


async def test_compact_without_any_agent_is_a_clear_error():
    """没有 Agent → 明确报错，不能假装压缩成功。"""
    ws = _WS()

    await srv._handle_compact(ws, {"session_id": "never-ran"})

    msg = ws.sent[0]
    assert msg["type"] == "compact_result"
    assert msg["ok"] is False
    assert "没有可压缩" in msg["error"]


async def test_compact_failure_is_reported_not_swallowed():
    agent = _CompactAgent(raises=RuntimeError("内部状态损坏"))
    srv._live_runs["s3"] = agent
    ws = _WS()

    await srv._handle_compact(ws, {"session_id": "s3"})

    msg = ws.sent[0]
    assert msg["type"] == "compact_result"
    assert msg["ok"] is False
    assert "内部状态损坏" in msg["error"]


def test_ws_compact_action_is_dispatched(client):
    """action 分发接线：前端发 {action:'compact'} 能收到 compact_result。"""
    srv._live_runs["s1"] = _CompactAgent()

    with client as c, c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"action": "compact", "session_id": "s1"})
        msg = ws.receive_json()

    assert msg["type"] == "compact_result"
    assert msg["ok"] is True and msg["session_id"] == "s1"


def test_ws_user_answer_action_is_dispatched(client, monkeypatch):
    """action 分发接线：前端发 {action:'user_answer'} 能收到 resolved。"""
    from automind.core import user_questions

    monkeypatch.setattr(
        user_questions, "resolve",
        lambda qid, answers: _resolved_record(qid, "default", answers))

    with client as c, c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({
            "action": "user_answer", "question_id": "q1",
            "answers": [{"id": "q1", "selected": ["A"]}],
        })
        msg = ws.receive_json()

    assert msg["type"] == "user_question_resolved"
    assert msg["question_id"] == "q1"
    assert msg["answers"] == [{"id": "q1", "selected": ["A"]}]
