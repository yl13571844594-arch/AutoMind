"""WebSocket 中途插话：**不打断任务**，且三种去向都有回执（v1.7.2）。

## 修的是什么

界面上此前唯一的"说话"方式是 ``action=run``，而它的处理逻辑第一句就是
"有旧任务就先 cancel"。于是用户在 AI 写到一半时补一句，实际效果是
**把刚才那半截回答掐了重来** —— 这正是用户最不想要的那种"响应"。

现在多了一条 ``action=interject``：收下、排进正在跑的那轮，什么都不取消。

## 这个文件盯住的四件事

1. 有任务在跑 → 入队 + 回执，**且绝不 cancel**；
2. 没有任务在跑 → 直接当成新任务开跑（用户的话不能掉在地上）；
3. 收不下（空/超长/超量）→ 明确回执，不假装收到；
4. ``ref`` 原样回显 —— 前端据此把回执对到**自己发的那一条**上
   （连发两句相同内容时，靠文本匹配是分不清的）。
"""

from __future__ import annotations

import asyncio

import pytest

import automind.server as srv
from automind.core.interject import MAX_PENDING, InterjectionQueue
from automind.core.types import InteractionMode


class _WS:
    """只记账的假连接。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def types(self) -> list[str]:
        return [m.get("type", "") for m in self.sent]


def _agent() -> object:
    """真 AutoMindAgent 实例，只补插话相关的那几个属性。"""
    from automind.agent import AutoMindAgent

    agent = object.__new__(AutoMindAgent)
    agent._interjections = InterjectionQueue()
    agent._interaction = InteractionMode.CHAT
    agent._run_session_id = "run-abc"
    return agent


@pytest.fixture(autouse=True)
def _clean_tables():
    """这两个表是进程级全局状态，测试之间必须互不污染。"""
    srv._live_runs.clear()
    srv._interject_refs.clear()
    yield
    srv._live_runs.clear()
    srv._interject_refs.clear()


# ═══════════════════════════════════════════════════════════
# 1. 有任务在跑：收下、回执、不打断
# ═══════════════════════════════════════════════════════════


async def test_interject_is_queued_and_acknowledged():
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent

    await srv._handle_interject(ws, "c1", {
        "session_id": "s1", "text": "顺便把日志级别改成 DEBUG", "ref": "bubble-7"})

    assert ws.types() == ["interjection_received"]
    ack = ws.sent[0]
    assert ack["seq"] == 1 and ack["text"].startswith("顺便")
    assert ack["ref"] == "bubble-7"
    assert ack["run_session_id"] == "run-abc"
    assert ack["pending"] == 1
    assert agent.pending_interjections() == 1


async def test_interject_does_not_cancel_the_running_task():
    """这是整件事的底线：插话的语义是"接着说"，不是"重来"。"""
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent
    running = asyncio.create_task(asyncio.sleep(30))
    srv._ws_tasks["c1"] = running
    try:
        await srv._handle_interject(ws, "c1", {"session_id": "s1", "text": "再补一句"})

        assert not running.cancelled() and not running.done(), \
            "插话把正在跑的任务取消了 —— 那就成了「先停止再重发」的旧行为"
    finally:
        running.cancel()
        srv._ws_tasks.pop("c1", None)


async def test_ref_is_remembered_for_the_applied_event_to_echo_back():
    """回执的 ref 要从"收到"一直跟到"生效" —— 中间丢了，前端就只能猜。"""
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent

    await srv._handle_interject(ws, "c1", {
        "session_id": "s1", "text": "改一下", "ref": "bubble-9"})

    assert srv._interject_refs[("s1", 1)] == "bubble-9"


async def test_client_ref_alias_is_accepted():
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent

    await srv._handle_interject(ws, "c1", {
        "session_id": "s1", "text": "x", "client_ref": "bubble-alias"})

    assert ws.sent[0]["ref"] == "bubble-alias"


async def test_missing_ref_is_an_empty_string_not_a_key_error():
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent

    await srv._handle_interject(ws, "c1", {"session_id": "s1", "text": "没有 ref"})

    assert ws.sent[0]["ref"] == ""


# ═══════════════════════════════════════════════════════════
# 2. 收不下：如实回执
# ═══════════════════════════════════════════════════════════


async def test_empty_interjection_is_rejected_not_silently_ignored():
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent

    await srv._handle_interject(ws, "c1", {"session_id": "s1", "text": "   "})

    assert ws.types() == ["interjection_rejected"]
    assert "空" in ws.sent[0]["reason"]


async def test_overlong_interjection_is_rejected_with_the_numbers():
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent

    await srv._handle_interject(ws, "c1", {"session_id": "s1", "text": "x" * 99999})

    assert ws.types() == ["interjection_rejected"]
    assert "太长" in ws.sent[0]["reason"]


async def test_pending_cap_is_enforced_through_the_ws_path():
    ws, agent = _WS(), _agent()
    srv._live_runs["s1"] = agent
    for i in range(MAX_PENDING):
        agent.interject(f"第 {i} 条")

    await srv._handle_interject(ws, "c1", {"session_id": "s1", "text": "再来一条"})

    assert ws.types() == ["interjection_rejected"]
    assert "上限" in ws.sent[0]["reason"]


# ═══════════════════════════════════════════════════════════
# 3. 没有任务在跑：当成新任务开跑
# ═══════════════════════════════════════════════════════════


async def test_interject_without_a_running_task_starts_one(monkeypatch):
    ws = _WS()
    started: list[dict] = []

    async def _fake_run(sock, client_id, data):
        started.append(data)

    monkeypatch.setattr(srv, "_ws_run", _fake_run)
    try:
        await srv._handle_interject(ws, "c1", {
            "session_id": "s1", "text": "帮我查一下这个报错", "interaction": "work",
            "ref": "b1"})

        assert ws.types() == ["interjection_promoted"]
        assert ws.sent[0]["ref"] == "b1"
        task = srv._ws_tasks.get("c1")
        assert task is not None
        await task
        assert started and started[0]["task"] == "帮我查一下这个报错"
        assert started[0]["interaction"] == "work"
    finally:
        srv._ws_tasks.pop("c1", None)


async def test_empty_text_without_a_running_task_is_rejected():
    ws = _WS()

    await srv._handle_interject(ws, "c1", {"session_id": "s1", "text": ""})

    assert ws.types() == ["interjection_rejected"]


# ═══════════════════════════════════════════════════════════
# 4. 接线：action 名与事件名都不能漂
# ═══════════════════════════════════════════════════════════


def test_ws_action_names_are_all_accepted():
    """三个别名都要收：前端写法不同不该让功能失效。"""
    import inspect

    src = inspect.getsource(srv.ws_endpoint)
    for alias in ("interject", "inject", "supplement"):
        assert f'"{alias}"' in src, f"WS 分发里没有 {alias}"


def test_registration_is_inside_the_finally_guarded_block():
    """登记必须被 finally 兜住 —— 否则中途抛错会留下一个"假装在跑"的 Agent。"""
    import inspect

    src = inspect.getsource(srv._ws_run)
    try_at = src.index("\n    try:\n")
    reg_at = src.index("_live_runs[chat_sid] = agent")
    fin_at = src.index("\n    finally:\n")

    assert try_at < reg_at, "登记早于 try：提前返回的路径会让它永远留在表里"
    assert reg_at < fin_at
    assert "_live_runs.pop(chat_sid" in src[fin_at:], "注销必须挂在 finally 里"


def test_dropped_is_reported_at_the_end_of_a_run():
    """没来得及并入本轮的插话必须回执，不能停在"已收到"。"""
    import inspect

    src = inspect.getsource(srv._ws_run)
    assert "interjection_dropped" in src
    assert "drain_interjections" in src
