# ruff: noqa: ARG005
"""v1.11.14 IM 渠道发送 payload / 分片标题 / 长连接适配（全部离线：monkeypatch 网络层）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from automind.bots import channels as ch_mod
from automind.bots import longconn as lc
from automind.bots.channels import CHANNELS, Incoming
from automind.bots.manager import BotManager, ProgressRelay


def run(coro):
    return asyncio.run(coro)


def _msg(**reply: Any) -> Incoming:
    return Incoming(chat_id="c1", user_id="u1", text="x", reply=reply)


@pytest.fixture(autouse=True)
def _fresh_tokens():
    """模块级 token 缓存会在用例间残留：每个用例前后都清掉，保证每次真的走假网络层。"""
    ch_mod._tokens.clear()
    yield
    ch_mod._tokens.clear()


MD = ("# 报告\n\n| 项目 | 状态 |\n| --- | --- |\n| 构建 | 完成 |\n\n"
      "```python\nprint(1)\n```\n\n**加粗** 与 [文档](https://example.com/a)")

RAW = "# 标题\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```py\nx\n```"


class _FakeWS:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


# ── 回调渠道：send 的 payload 断言 ──────────────────────────────


def test_feishu_send_payload_is_adapted(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(url, payload, headers=None, timeout=15.0):
        calls.append((url, payload))
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "tok", "expire": 7200}
        return {"code": 0}

    monkeypatch.setattr(ch_mod, "_post_json", fake_post)
    bot = {"id": "b1", "channel": "feishu",
           "credentials": {"app_id": "cli_x", "app_secret": "s"}}
    run(CHANNELS["feishu"].send(bot, _msg(message_id="om_1"), MD, "**周报**"))

    url, payload = calls[-1]
    assert url.endswith("/im/v1/messages/om_1/reply")
    assert payload["msg_type"] == "interactive"
    card = json.loads(payload["content"])
    body = card["elements"][0]["content"]
    assert "| ---" not in body and "**项目**：构建" in body
    assert "```python" in body and "print(1)" in body
    assert card["header"]["title"] == {"tag": "plain_text", "content": "周报"}


def test_dingtalk_send_payload_is_adapted(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(url, payload, headers=None, timeout=15.0):
        calls.append((url, payload))
        return {"errcode": 0}

    monkeypatch.setattr(ch_mod, "_post_json", fake_post)
    bot = {"id": "b1", "channel": "dingtalk", "credentials": {}}
    msg = _msg(webhook="https://oapi.dingtalk.com/robot/sendBySession?x")
    run(CHANNELS["dingtalk"].send(bot, msg, MD + "\n\n@13800000000 看一下", "**很长" * 30))

    url, payload = calls[-1]
    assert "oapi.dingtalk.com" in url and payload["msgtype"] == "markdown"
    title = payload["markdown"]["title"]
    assert len(title) <= 30 and "**" not in title
    body = payload["markdown"]["text"]
    assert "| ---" not in body and "```python" in body
    assert "@13800000000" in body                       # @ 人保持不变


def test_wecom_send_payload_is_adapted(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_get(url, params, timeout=15.0):
        return {"access_token": "tok", "expires_in": 7200}

    async def fake_post(url, payload, headers=None, timeout=15.0):
        calls.append(payload)
        return {"errcode": 0}

    monkeypatch.setattr(ch_mod, "_get_json", fake_get)
    monkeypatch.setattr(ch_mod, "_post_json", fake_post)
    bot = {"id": "b1", "channel": "wecom",
           "credentials": {"corp_id": "c", "agent_id": "1000002", "secret": "s"}}
    run(CHANNELS["wecom"].send(bot, _msg(touser="zhangsan"), MD, "周报"))

    payload = calls[-1]
    assert payload["msgtype"] == "markdown" and payload["touser"] == "zhangsan"
    body = payload["markdown"]["content"]
    assert body.startswith("## 周报\n")                 # 标题在正文首行（不再是 **标题**）
    assert "```" not in body and "> 代码块（python）：" in body
    assert "| ---" not in body and "**项目**：构建" in body
    assert len(body.encode("utf-8")) <= CHANNELS["wecom"].limit_bytes


def test_wechat_mp_send_payload_is_plain_text(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_get(url, params, timeout=15.0):
        return {"access_token": "tok", "expires_in": 7200}

    async def fake_post(url, payload, headers=None, timeout=15.0):
        calls.append(payload)
        return {"errcode": 0}

    monkeypatch.setattr(ch_mod, "_get_json", fake_get)
    monkeypatch.setattr(ch_mod, "_post_json", fake_post)
    bot = {"id": "b1", "channel": "wechat_mp",
           "credentials": {"app_id": "wx1", "app_secret": "s"}}
    run(CHANNELS["wechat_mp"].send(bot, _msg(openid="openid1"), MD, "周报"))

    payload = calls[-1]
    assert payload["msgtype"] == "text" and payload["touser"] == "openid1"
    content = payload["text"]["content"]
    assert content.startswith("【周报】\n")
    assert "**加粗**" not in content and "加粗" in content
    assert "| ---" not in content and "• 项目：构建 · 状态：完成" in content
    assert "```" not in content and "print(1)" in content
    assert "文档（https://example.com/a）" in content


def test_webhook_send_keeps_raw_markdown(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_post(url, payload, headers=None, timeout=15.0):
        calls.append(payload)
        return {}

    monkeypatch.setattr(ch_mod, "_post_json", fake_post)
    monkeypatch.setattr("automind.tools._toolkit.check_url",
                        lambda url, allow_private=False: None)
    bot = {"id": "b1", "channel": "webhook",
           "credentials": {"callback_url": "https://example.com/hook"}}
    run(CHANNELS["webhook"].send(bot, _msg(), RAW, "**原始**"))

    payload = calls[-1]
    assert payload["text"] == RAW                       # 下游自己渲染：原样保留
    assert payload["title"] == "**原始**"


# ── 长连接（企业微信智能机器人）────────────────────────────────


def test_wecom_bot_send_adapts_stream_and_followup_push(monkeypatch):
    async def go():
        ws = _FakeWS()
        monkeypatch.setitem(lc._WECOM_LIVE, "w1", {"ws": ws, "lock": asyncio.Lock()})
        bot = {"id": "w1", "channel": "wecom_bot", "credentials": {}}
        msg = Incoming(chat_id="g1", user_id="u1", text="x",
                       reply={"req_id": "R1", "chatid": "g1"})
        await lc.wecom_bot_send(bot, msg, MD, title="周报")

        first = ws.frames[0]
        assert first["cmd"] == "aibot_respond_msg"
        assert first["headers"]["req_id"] == "R1"
        assert first["body"]["stream"]["finish"] is True       # 首条流式回复 finish=true（保持现状）
        content = first["body"]["stream"]["content"]
        assert content.startswith("## 周报")
        assert "| ---" not in content and "**项目**：构建" in content
        assert "```python" in content                          # 智能机器人保留围栏代码块

        await lc.wecom_bot_send(bot, msg, MD)
        second = ws.frames[1]
        assert second["cmd"] == "aibot_send_msg"
        assert second["body"]["chatid"] == "g1"
        assert "| ---" not in second["body"]["markdown"]["content"]

    run(go())


def test_wecom_bot_channel_send_goes_through_adapter(monkeypatch):
    async def go():
        ws = _FakeWS()
        monkeypatch.setitem(lc._WECOM_LIVE, "w1", {"ws": ws, "lock": asyncio.Lock()})
        bot = {"id": "w1", "channel": "wecom_bot", "credentials": {}}
        msg = Incoming(chat_id="g", user_id="u", text="x",
                       reply={"req_id": "R", "chatid": "g"})
        await CHANNELS["wecom_bot"].send(bot, msg, MD, title="周报")
        content = ws.frames[0]["body"]["stream"]["content"]
        assert content.startswith("## 周报")
        assert "| ---" not in content

    run(go())


# ── 管理器：分片标题 / 进度与结果排版 ──────────────────────────


def _mgr(run_task=None) -> BotManager:
    cfg: dict[str, Any] = {}

    def read() -> dict[str, Any]:
        return json.loads(json.dumps(cfg))

    def write(value: dict[str, Any]) -> None:
        cfg.clear()
        cfg.update(value)

    return BotManager(read_cfg=read, write_cfg=write, run_task=run_task, run_hooks={})


def test_channel_sender_splits_safely_and_titles_continuations(monkeypatch):
    async def go():
        mgr = _mgr()
        channel = CHANNELS["wecom"]
        sent: list[tuple[str, str]] = []

        async def fake_send(bot, msg, text, title=""):
            sent.append((title, text))

        monkeypatch.setattr(channel, "send", fake_send)
        bot = {"id": "b1", "channel": "wecom"}
        sender = mgr._channel_sender(bot)
        text = "\n\n".join("第" + str(i) + "段：" + "甲" * 400 for i in range(3))

        await sender(_msg(), text, "周报")
        assert len(sent) == 3
        assert [t for t, _ in sent] == ["周报", "周报（续 2/3）", "周报（续 3/3）"]
        assert all(len(part.encode("utf-8")) <= channel.limit_bytes for _, part in sent)
        assert all("续" not in part for _, part in sent)     # 正文不重复续片标记

        sent.clear()
        await sender(_msg(), text, "")
        assert [t for t, _ in sent] == ["", "（续 2/3）", "（续 3/3）"]

    run(go())


def test_progress_and_final_result_use_markdown_lists():
    async def go():
        sent: list[tuple[str, str]] = []

        async def send(text: str, title: str) -> None:
            sent.append((title, text))

        relay = ProgressRelay(send, "brief", 10)
        await relay.on_event({"type": "step_action", "tool": "file_read",
                              "success": True, "args": {}})
        await relay.on_event({"type": "tool_error", "tool": "terminal", "error": "boom"})
        await relay.flush()
        body = sent[0][1]
        assert body.startswith("- ")
        assert all(line.startswith("- ") for line in body.splitlines())
        assert "已调用工具" in body and "工具失败" in body

        async def run_task(data):
            return {"success": True, "output": "报告已生成", "steps": 2, "tokens": 9}

        mgr = _mgr(run_task)
        bot = mgr.get(mgr.create({"channel": "webhook"})["id"])
        out: list[tuple[str, str]] = []

        async def capture(msg, text, title=""):
            out.append((title, text))

        task = await mgr.process(bot, Incoming(chat_id="c1", user_id="u1", text="统计"), capture)
        await task
        assert out[-1][0] == "✅ 任务完成"
        final = out[-1][1]
        assert "报告已生成" in final
        assert "- ⏱ 用时" in final
        assert "- 🧭 步数 2" in final
        assert "- 🔤 9 tokens" in final

    run(go())
