# ruff: noqa: ARG005
"""v1.11.6 IM 机器人：渠道验签 / 解析、指令、过程回传、聊天内审批、回调入口的准入。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.bots import BotManager, HttpReq, Incoming
from automind.bots import crypto as bc
from automind.bots.channels import CHANNELS, ChannelError, split_utf8

AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"   # 43 位


def _req(method="POST", body: Any = b"", query=None, headers=None) -> HttpReq:
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    return HttpReq(method=method, query=query or {}, headers=headers or {}, body=body)


def run(coro):
    return asyncio.run(coro)


# ── 加解密 ──────────────────────────────────────────────────────


def test_wx_crypt_roundtrip():
    wx = bc.WXCrypt("tok", AES_KEY, "corp1")
    enc = wx.encrypt("<xml><Content>你好</Content></xml>")
    assert wx.decrypt(enc) == "<xml><Content>你好</Content></xml>"
    with pytest.raises(ValueError):
        bc.WXCrypt("tok", AES_KEY, "other").decrypt(enc)


def test_feishu_crypt_roundtrip():
    assert bc.feishu_decrypt("k", bc.feishu_encrypt("k", '{"a":1}')) == '{"a":1}'


# ── 渠道 ────────────────────────────────────────────────────────


def _bot(channel, **creds):
    return {"id": "b1", "channel": channel, "credentials": creds}


def test_feishu_url_verification_and_token():
    ch = CHANNELS["feishu"]
    bot = _bot("feishu", verification_token="vt")
    rep, msg = run(ch.handle(bot, _req(body={"type": "url_verification", "challenge": "c1", "token": "vt"})))
    assert rep.body == {"challenge": "c1"} and msg is None
    with pytest.raises(ChannelError):
        run(ch.handle(bot, _req(body={"type": "url_verification", "challenge": "c1", "token": "bad"})))


def test_feishu_encrypted_message_event():
    ch = CHANNELS["feishu"]
    bot = _bot("feishu", encrypt_key="ek")
    event = {"schema": "2.0", "header": {"event_id": f"e{time.time()}", "event_type": "im.message.receive_v1"},
             "event": {"sender": {"sender_id": {"open_id": "ou_1"}},
                       "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "group",
                                   "message_type": "text",
                                   "content": json.dumps({"text": "@_user_1 帮我统计日志"})}}}
    body = json.dumps({"encrypt": bc.feishu_encrypt("ek", json.dumps(event))}).encode()
    ts, nonce = "1700000000", "n1"
    sig = bc.feishu_signature(ts, nonce, "ek", body)
    rep, msg = run(ch.handle(bot, _req(body=body, headers={
        "x-lark-signature": sig, "x-lark-request-timestamp": ts, "x-lark-request-nonce": nonce})))
    assert msg.text == "帮我统计日志" and msg.chat_id == "oc_1" and msg.is_group
    assert msg.user_id == "ou_1" and msg.reply["message_id"] == "om_1"
    # 同一事件重推不重复处理
    _, again = run(ch.handle(bot, _req(body=body, headers={
        "x-lark-signature": sig, "x-lark-request-timestamp": ts, "x-lark-request-nonce": nonce})))
    assert again is None
    with pytest.raises(ChannelError):
        run(ch.handle(bot, _req(body=body, headers={
            "x-lark-signature": "bad", "x-lark-request-timestamp": ts, "x-lark-request-nonce": nonce})))


def test_dingtalk_signature():
    ch = CHANNELS["dingtalk"]
    bot = _bot("dingtalk", app_secret="sec")
    ts = str(int(time.time() * 1000))
    body = {"text": {"content": " 跑一下测试 "}, "conversationId": "cid", "senderStaffId": "u1",
            "senderNick": "张三", "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?x",
            "conversationType": "2"}
    _, msg = run(ch.handle(bot, _req(body=body, headers={"timestamp": ts, "sign": bc.dingtalk_sign("sec", ts)})))
    assert msg.text == "跑一下测试" and msg.user_name == "张三" and msg.reply["webhook"]
    with pytest.raises(ChannelError):
        run(ch.handle(bot, _req(body=body, headers={"timestamp": ts, "sign": "bad"})))


def test_wecom_url_verify_and_message():
    ch = CHANNELS["wecom"]
    bot = _bot("wecom", corp_id="corp1", agent_id="1000002", secret="s", token="tok",
               encoding_aes_key=AES_KEY)
    wx = bc.WXCrypt("tok", AES_KEY, "corp1")
    echo = wx.encrypt("echo-123")
    q = {"timestamp": "1", "nonce": "n", "echostr": echo,
         "msg_signature": bc.wx_signature("tok", "1", "n", echo)}
    rep, _ = run(ch.handle(bot, _req("GET", query=q)))
    assert rep.body == "echo-123"
    inner = ("<xml><ToUserName>corp1</ToUserName><FromUserName>zhangsan</FromUserName>"
             "<MsgType>text</MsgType><Content>生成周报</Content><MsgId>9</MsgId></xml>")
    enc = wx.encrypt(inner)
    q = {"timestamp": "2", "nonce": "m", "msg_signature": bc.wx_signature("tok", "2", "m", enc)}
    rep, msg = run(ch.handle(bot, _req(body=f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>", query=q)))
    assert rep.body == "success" and msg.text == "生成周报" and msg.reply["touser"] == "zhangsan"


def test_wechat_mp_plain_mode():
    ch = CHANNELS["wechat_mp"]
    bot = _bot("wechat_mp", app_id="wx1", app_secret="s", token="tok")
    q = {"timestamp": "1", "nonce": "n", "signature": bc.wx_signature("tok", "1", "n"), "echostr": "hi"}
    rep, _ = run(ch.handle(bot, _req("GET", query=q)))
    assert rep.body == "hi"
    xml = ("<xml><ToUserName>gh</ToUserName><FromUserName>openid1</FromUserName>"
           "<MsgType>text</MsgType><Content>查天气</Content><MsgId>1</MsgId></xml>")
    _, msg = run(ch.handle(bot, _req(body=xml, query=q)))
    assert msg.text == "查天气" and msg.user_id == "openid1"
    with pytest.raises(ChannelError):
        run(ch.handle(bot, _req("GET", query={**q, "signature": "x"})))


def test_split_utf8_respects_byte_limit():
    parts = split_utf8("中" * 1500, 2000)
    assert all(len(p.encode()) <= 2000 for p in parts) and "".join(parts) == "中" * 1500


# ── 管理器：指令 / 过程回传 / 审批 ─────────────────────────────


class FakeCfg:
    def __init__(self):
        self.cfg: dict[str, Any] = {}

    def read(self):
        return json.loads(json.dumps(self.cfg))

    def write(self, c):
        self.cfg = c


def _mgr(run_task, hooks=None):
    c = FakeCfg()
    hooks = {} if hooks is None else hooks
    return BotManager(read_cfg=c.read, write_cfg=c.write, run_task=run_task, run_hooks=hooks), hooks


def test_bot_session_ids_are_namespaced_and_chat_scoped():
    """IM 会话必须与 Web 会话分属不同命名空间（v1.11.13）。

    会话 id 同时决定：会话克隆键、历史/记忆键、**目录隔离副本名**。若机器人
    会话与浏览器会话共用一套 id，两个工作区就会互相覆盖/读串 —— 这是"工作区
    与 IM 机器人工作区混淆"的根源之一。这里钉住三条：bot- 前缀、按聊天隔离、
    /new 后换新 id。
    """
    from automind.bots.manager import _ChatState

    m, _ = _mgr(None)
    b1 = m.get(m.create({"channel": "webhook", "mode": "work"})["id"])
    b2 = m.get(m.create({"channel": "webhook", "mode": "work"})["id"])
    msg_a = Incoming(chat_id="chat-A", user_id="u1", text="x")
    msg_b = Incoming(chat_id="chat-B", user_id="u1", text="x")
    st_a, st_b = _ChatState(), _ChatState()

    sid_a = m._session(b1, msg_a, st_a)
    sid_b = m._session(b1, msg_b, st_b)
    sid_other_bot = m._session(b2, msg_a, st_b)

    assert sid_a.startswith("bot-") and sid_a != sid_b
    assert sid_a != sid_other_bot
    assert not sid_a.startswith(("s_", "web-", "admin-"))
    st_a.generation = 1
    assert m._session(b1, msg_a, st_a) != sid_a


def test_crud_masks_and_keeps_secrets():
    m, _ = _mgr(None)
    b = m.create({"channel": "feishu", "credentials": {"app_id": "cli_1", "app_secret": "supersecret"}})
    assert b["credentials"]["app_secret"] == "••••cret" and b["hook_token"]
    m.update(b["id"], {"name": "x", "credentials": dict(b["credentials"])})   # 原样回传打码值
    assert m.get(b["id"])["credentials"]["app_secret"] == "supersecret"
    with pytest.raises(ChannelError):
        m.create({"channel": "nope"})
    assert m.delete(b["id"]) and m.list() == []


def _collect():
    out: list[tuple[str, str]] = []

    async def sender(msg: Incoming, text: str, title: str = ""):
        out.append((title, text))
    return out, sender


def test_task_progress_and_result_flow():
    async def go():
        async def run_task(data):
            sink = hooks[data["session_id"]]["event_sink"]
            await sink({"type": "plan_created", "steps": [
                {"goal_id": "g1", "description": "读取日志"}, {"goal_id": "g2", "description": "写报告"}]})
            await sink({"type": "step_action", "tool": "file_read", "success": True, "args": {}})
            await sink({"type": "tool_error", "tool": "terminal", "error": "Could not resolve host",
                        "network": True})
            await sink({"type": "plan_step_end", "goal_id": "g1", "success": True})
            assert data["overrides"] == {"mode": "work"}
            return {"success": True, "output": "报告已写好：report.md", "steps": 2, "tokens": 900}
        m, hooks = _mgr(run_task)
        bot = m.get(m.create({"channel": "webhook"})["id"])
        out, sender = _collect()
        task = await m.process(bot, Incoming(chat_id="c1", user_id="u1", text="统计日志"), sender)
        await task
        titles = [t for t, _ in out]
        assert titles[0] == "🤖 收到，开始执行"
        assert "📋 已生成计划（2 步）" in titles
        assert any("网络中断" in x for _, x in out)
        assert titles[-1] == "✅ 任务完成" and "report.md" in out[-1][1]
        assert hooks == {}                       # 结束后回调已注销
    asyncio.run(go())


def test_approval_in_chat():
    async def go():
        decided: dict[str, Any] = {}

        async def run_task(data):
            appr = hooks[data["session_id"]]["approval"]
            decided["r"] = await appr("terminal", {"command": "rm -rf build"}, "dangerous", "删除目录")
            return {"success": True, "output": "ok"}
        m, hooks = _mgr(run_task)
        bot = m.get(m.create({"channel": "webhook"})["id"])
        out, sender = _collect()
        task = await m.process(bot, Incoming(chat_id="c1", user_id="u1", text="清理构建目录"), sender)
        for _ in range(50):
            if any(t == "⚠ 需要你批准" for t, _ in out):
                break
            await asyncio.sleep(0.01)
        # 任务进行中再来普通消息 → 提示等待；审批回复不受影响
        await m.process(bot, Incoming(chat_id="c1", user_id="u1", text="再来一个"), sender)
        assert out[-1][0] == "⏳ 请稍候"
        await m.process(bot, Incoming(chat_id="c1", user_id="u1", text="同意"), sender)
        await task
        assert decided["r"]["approved"] is True
    asyncio.run(go())


def test_commands_and_allowlist():
    async def go():
        m, _ = _mgr(None)
        bot = m.get(m.create({"channel": "webhook", "allow_users": "boss"})["id"])
        out, sender = _collect()
        await m.process(bot, Incoming(chat_id="c", user_id="intruder", text="删库"), sender)
        assert out[-1][0] == "🔒 未授权"
        await m.process(bot, Incoming(chat_id="c", user_id="boss", text="/help"), sender)
        assert "/stop" in out[-1][1]
        await m.process(bot, Incoming(chat_id="c", user_id="boss", text="/mode 编程"), sender)
        assert "编程" in out[-1][1]
        await m.process(bot, Incoming(chat_id="c", user_id="boss", text="/stop"), sender)
        assert "没有正在执行" in out[-1][1]
    asyncio.run(go())


def test_stop_cancels_running_task():
    async def go():
        started = asyncio.Event()

        async def run_task(data):
            started.set()
            await asyncio.sleep(30)
        m, _ = _mgr(run_task)
        bot = m.get(m.create({"channel": "webhook"})["id"])
        out, sender = _collect()
        task = await m.process(bot, Incoming(chat_id="c", user_id="u", text="长任务"), sender)
        await started.wait()
        await m.process(bot, Incoming(chat_id="c", user_id="u", text="停止"), sender)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(t == "⏹ 已停止" for t, _ in out)
    asyncio.run(go())


# ── 服务端入口 ──────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg: dict[str, Any] = {}
    monkeypatch.setattr(srv, "_read_config", lambda: json.loads(json.dumps(cfg)))
    monkeypatch.setattr(srv, "_write_config", lambda c: (cfg.clear(), cfg.update(c)))
    calls: list[dict] = []

    async def fake_run(data):
        calls.append(data)
        return {"success": True, "output": f"done: {data['task']}"}
    mgr = BotManager(read_cfg=srv._read_config, write_cfg=srv._write_config,
                     run_task=fake_run, run_hooks=srv._run_hooks)
    monkeypatch.setattr(srv, "_bots_mgr", mgr)
    srv._AUTH_TOKEN = ""
    with TestClient(srv.app, client=("127.0.0.1", 5555)) as c:
        yield c, calls


def test_hook_endpoint_webhook_wait(client):
    c, calls = client
    bot = c.post("/api/bots", json={"channel": "webhook", "credentials": {"secret": "s1"}}).json()["bot"]
    url = f"/api/bots/hook/{bot['id']}/{bot['hook_token']}"
    r = c.post(url, json={"text": "你好", "chat_id": "g1", "wait": True}, headers={"X-Bot-Secret": "s1"})
    assert r.status_code == 200, r.text
    titles = [x["title"] for x in r.json()["replies"]]
    assert titles[0] == "🤖 收到，开始执行" and titles[-1] == "✅ 任务完成"
    assert calls[0]["task"] == "你好" and calls[0]["session_id"].startswith("bot-")
    assert c.post(url, json={"text": "x"}, headers={"X-Bot-Secret": "bad"}).status_code == 401
    assert c.post(f"/api/bots/hook/{bot['id']}/wrong", json={"text": "x"}).status_code == 404
    log = c.get(f"/api/bots/{bot['id']}/log").json()["log"]
    assert log[0]["dir"] == "in" and log[0]["text"] == "你好"


def test_hook_passes_auth_token_and_public_host(client, monkeypatch):
    """平台回调经公网域名进来、也不带平台访问令牌：回调放行，其余接口照旧拦。"""
    c, _ = client
    bot = c.post("/api/bots", json={"channel": "webhook"}).json()["bot"]
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "platform-token")
    url = f"/api/bots/hook/{bot['id']}/{bot['hook_token']}"
    hdr = {"Host": "abc.ngrok-free.app"}
    assert c.post(url, json={"text": "hi"}, headers=hdr).status_code == 200
    assert c.get("/api/bots", headers=hdr).status_code in (401, 403)


def test_bot_admin_endpoints_need_admin_remotely(monkeypatch):
    with TestClient(srv.app, client=("192.168.1.9", 5555)) as c:
        assert c.post("/api/bots", json={"channel": "webhook"}).status_code == 403


def test_channels_spec(client):
    c, _ = client
    keys = {x["key"] for x in c.get("/api/bots/channels").json()["channels"]}
    assert keys == {"feishu", "dingtalk", "wecom_bot", "wecom", "wechat_mp", "webhook"}


def test_run_hooks_receive_events_inside_api_run(monkeypatch):
    """/api/run 必须把登记的 event_sink / approval 装到本次执行的 Agent 上，结束后摘掉，且不广播。"""
    from types import SimpleNamespace

    from automind.core.types import InteractionMode
    seen: dict[str, Any] = {}

    class FakeAgent:
        _interaction = InteractionMode.WORK
        event_sink = None
        approval_callback = None
        llm = object()
        config = SimpleNamespace(llm=SimpleNamespace(provider="p", model="m"))

        async def run(self, task):
            seen["sink"], seen["appr"] = self.event_sink, self.approval_callback
            return SimpleNamespace(output="ok", success=True, steps_executed=1, backtracks=0,
                                   errors_corrected=0, duration_ms=1.0, plan=None,
                                   token_usage=SimpleNamespace(total=0, prompt_tokens=0,
                                                               completion_tokens=0))
    fake = FakeAgent()
    monkeypatch.setattr(srv, "_acquire_run_agent", lambda a, sid: fake)
    monkeypatch.setattr(srv, "_apply_mode_model", lambda a, m: a)
    monkeypatch.setattr(srv, "_apply_router", lambda a, t, i: a)
    monkeypatch.setattr(srv._quota, "try_consume_task", lambda: (True, ""))
    broadcasts: list = []

    async def bc_(d):
        broadcasts.append(d)
    monkeypatch.setattr(srv, "_broadcast", bc_)
    sink, appr = object(), object()
    srv._run_hooks["s-hook"] = {"event_sink": sink, "approval": appr, "quiet": True}
    try:
        rec = asyncio.run(srv.api_run({"task": "x", "session_id": "s-hook"}))
    finally:
        srv._run_hooks.pop("s-hook", None)
    assert rec["output"] == "ok"
    assert seen == {"sink": sink, "appr": appr}
    assert fake.event_sink is None and fake.approval_callback is None
    assert broadcasts == []
