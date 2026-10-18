"""v1.11.7 IM 长连接：用本地 WebSocket 服务模拟飞书 / 钉钉 Stream / 企业微信智能机器人。

验证的是"本机主动连平台"这条路的协议细节：注册 → 建连 → 收消息 → 逐条 ACK →
交给机器人处理 → 回复（企业微信走同一条连接）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from automind.bots import longconn as lc
from automind.bots.channels import CHANNELS, Incoming

websockets = pytest.importorskip("websockets")


def test_frame_codec_roundtrip():
    f = {"seq_id": 3, "log_id": 2 ** 40, "service": 77, "method": 1,
         "headers": {"type": "event", "message_id": "m1", "sum": "2", "seq": "1"},
         "payload": "中文".encode(), "payload_type": "json", "log_id_new": "x"}
    back = lc.decode_frame(lc.encode_frame(f))
    assert back["seq_id"] == 3 and back["log_id"] == 2 ** 40 and back["service"] == 77
    assert back["headers"] == f["headers"] and back["payload"].decode() == "中文"
    assert back["payload_type"] == "json" and back["log_id_new"] == "x"


def test_transports_and_missing():
    ding = CHANNELS["dingtalk"]
    assert ding.transports[0] == "stream"
    assert "AppKey（Client ID）" in ding.missing({"app_secret": "s"}, "stream")
    assert ding.missing({"app_secret": "s"}, "webhook") == []
    assert CHANNELS["wecom_bot"].transports == ("stream",)
    assert lc.LongConnRunner.wants_stream({"channel": "feishu", "enabled": True})
    assert not lc.LongConnRunner.wants_stream({"channel": "feishu", "transport": "webhook"})
    assert not lc.LongConnRunner.wants_stream({"channel": "webhook"})


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _runner():
    got: list[Incoming] = []

    async def on_message(bot, msg):
        got.append(msg)
    return lc.LongConnRunner(on_message), got


def test_dingtalk_stream(monkeypatch):
    async def go():
        acks: list[dict[str, Any]] = []

        async def handler(ws):
            await ws.send(json.dumps({"type": "SYSTEM", "headers": {"topic": "ping", "messageId": "p1"},
                                      "data": json.dumps({"opaque": "x"})}))
            acks.append(json.loads(await ws.recv()))
            body = {"text": {"content": "你好"}, "conversationId": "cid1", "senderStaffId": "u1",
                    "senderNick": "张三", "sessionWebhook": "https://x/y", "conversationType": "1"}
            await ws.send(json.dumps({"type": "CALLBACK",
                                      "headers": {"topic": "/v1.0/im/bot/messages/get", "messageId": "m1"},
                                      "data": json.dumps(body)}))
            acks.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "SYSTEM", "headers": {"topic": "disconnect", "messageId": "d"},
                                      "data": ""}))
            acks.append(json.loads(await ws.recv()))
            await asyncio.sleep(0.2)

        server, port = await _serve(handler)
        seen: dict[str, Any] = {}

        async def fake_post(url, payload, headers=None, timeout=15.0):
            seen["url"], seen["payload"] = url, payload
            return {"endpoint": f"ws://127.0.0.1:{port}", "ticket": "t k"}
        monkeypatch.setattr("automind.bots.channels._post_json", fake_post)
        runner, got = _runner()
        bot = {"id": "d1", "channel": "dingtalk", "credentials": {"app_key": "ak", "app_secret": "sk"}}
        await asyncio.wait_for(runner._dingtalk(bot), timeout=5)    # disconnect → 正常返回
        await asyncio.sleep(0.05)
        server.close()
        assert seen["payload"]["clientId"] == "ak" and seen["payload"]["subscriptions"][0]["topic"] \
            == "/v1.0/im/bot/messages/get"
        assert acks[0]["code"] == 200 and acks[0]["headers"]["messageId"] == "p1"
        assert acks[0]["data"] == json.dumps({"opaque": "x"})             # ping 原样回显
        assert acks[1]["headers"]["messageId"] == "m1"
        assert got and got[0].text == "你好" and got[0].reply["webhook"] == "https://x/y"
        assert lc.status("d1")["state"] == "connected"
    asyncio.run(go())


def test_wecom_bot_subscribe_receive_and_reply():
    async def go():
        frames: list[dict[str, Any]] = []
        done = asyncio.Event()

        async def handler(ws):
            sub = json.loads(await ws.recv())
            frames.append(sub)
            await ws.send(json.dumps({"headers": {"req_id": sub["headers"]["req_id"]}, "errcode": 0,
                                      "errmsg": "ok"}))
            await ws.send(json.dumps({"cmd": "aibot_msg_callback", "headers": {"req_id": "R1"},
                                      "body": {"msgid": "m", "chattype": "group", "chatid": "g1",
                                               "from": {"userid": "zhang"}, "msgtype": "text",
                                               "text": {"content": "@小助手 生成周报"}}}))
            frames.append(json.loads(await ws.recv()))
            frames.append(json.loads(await ws.recv()))
            done.set()
            await asyncio.sleep(0.5)

        server, port = await _serve(handler)
        runner, got = _runner()
        bot = {"id": "w1", "channel": "wecom_bot",
               "credentials": {"bot_id": "b", "secret": "s", "ws_url": f"ws://127.0.0.1:{port}"}}
        task = asyncio.create_task(runner._wecom_bot(bot))
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        msg = got[0]
        assert msg.text == "生成周报" and msg.chat_id == "g1" and msg.user_id == "zhang" and msg.is_group
        await lc.wecom_bot_send(bot, msg, "收到")
        await lc.wecom_bot_send(bot, msg, "结果")
        await asyncio.wait_for(done.wait(), 5)
        task.cancel()
        server.close()
        assert frames[0]["cmd"] == "aibot_subscribe" and frames[0]["body"] == {"bot_id": "b", "secret": "s"}
        assert frames[1]["cmd"] == "aibot_respond_msg" and frames[1]["headers"]["req_id"] == "R1"
        assert frames[1]["body"]["stream"]["finish"] is True
        assert frames[2]["cmd"] == "aibot_send_msg" and frames[2]["body"]["chatid"] == "g1"
    asyncio.run(go())


def test_wecom_bot_auth_failure_is_reported():
    async def go():
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"errcode": 40001, "errmsg": "invalid secret"}))
            await asyncio.sleep(0.2)
        server, port = await _serve(handler)
        runner, _ = _runner()
        bot = {"id": "w2", "channel": "wecom_bot",
               "credentials": {"bot_id": "b", "secret": "bad", "ws_url": f"ws://127.0.0.1:{port}"}}
        with pytest.raises(Exception, match="鉴权失败"):
            await runner._wecom_bot(bot)
        server.close()
    asyncio.run(go())


def test_feishu_long_connection(monkeypatch):
    async def go():
        acks: list[dict[str, Any]] = []
        event = {"schema": "2.0", "header": {"event_id": "ev-lc-1", "event_type": "im.message.receive_v1"},
                 "event": {"sender": {"sender_id": {"open_id": "ou_9"}},
                           "message": {"message_id": "om_9", "chat_id": "oc_9", "chat_type": "p2p",
                                       "message_type": "text", "content": json.dumps({"text": "查日志"})}}}
        raw = json.dumps(event).encode()
        half = len(raw) // 2

        async def handler(ws):
            for seq, chunk in enumerate((raw[:half], raw[half:])):
                await ws.send(lc.encode_frame({"seq_id": 1, "service": 7, "method": 1, "payload": chunk,
                                               "headers": {"type": "event", "message_id": "mm",
                                                           "sum": "2", "seq": str(seq)}}))
            acks.append(lc.decode_frame(await ws.recv()))
            await asyncio.sleep(0.3)

        server, port = await _serve(handler)

        async def fake_post(url, payload, headers=None, timeout=15.0):
            assert url.endswith("/callback/ws/endpoint") and payload["AppID"] == "cli"
            return {"code": 0, "data": {"URL": f"ws://127.0.0.1:{port}/?device_id=d&service_id=7",
                                        "ClientConfig": {"PingInterval": 60}}}
        monkeypatch.setattr("automind.bots.channels._post_json", fake_post)
        runner, got = _runner()
        bot = {"id": "f1", "channel": "feishu", "credentials": {"app_id": "cli", "app_secret": "s"}}
        task = asyncio.create_task(runner._feishu(bot))
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        server.close()
        assert got[0].text == "查日志" and got[0].reply["message_id"] == "om_9"
        ack = acks[0]
        assert ack["method"] == 1 and json.loads(ack["payload"])["code"] == 200
        assert "biz_rt" in ack["headers"]
    asyncio.run(go())


def test_runner_reconnects_with_backoff(monkeypatch):
    async def go():
        calls = {"n": 0}
        runner, _ = _runner()

        async def boom(bot):
            calls["n"] += 1
            raise RuntimeError("network down")
        monkeypatch.setattr(runner, "_dingtalk", boom)
        monkeypatch.setattr(lc, "_BACKOFF", (0.01,))
        bot = {"id": "r1", "channel": "dingtalk", "enabled": True,
               "credentials": {"app_key": "a", "app_secret": "s"}}
        runner.sync([bot])
        await asyncio.sleep(0.2)
        assert calls["n"] >= 3
        st = lc.status("r1")
        assert st["state"] == "retrying" and "network down" in st["error"]
        runner.sync([])                                   # 删除后停止
        await asyncio.sleep(0.05)
        assert "r1" not in runner._tasks
        await runner.stop()
    asyncio.run(go())
