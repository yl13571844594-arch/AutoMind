"""IM 长连接（v1.11.7）：本机**主动**连到平台，消息经这条连接推过来 —— 不需要公网地址。

为什么这是本地 / 云电脑部署的最优解：
回调（webhook）模式要求平台能从公网访问到本机，于是必须有公网 IP、域名、内网穿透，
还要处理证书、可信 IP、端口映射；任何一环断了机器人就"收不到消息"，而且排查困难。
长连接模式反过来由本机发起一条出站 WebSocket 连接，只要本机能上网（能打开网页就行）
就能收消息，NAT、防火墙、无公网 IP 的云电脑都不受影响。三家主流平台都官方支持：

=================  ===============================================  ==================
平台               协议                                              回复方式
=================  ===============================================  ==================
飞书 / Lark         POST /callback/ws/endpoint → WebSocket，帧为       开放平台 API
                   protobuf（pbbp2.Frame），应用层 ping
钉钉               POST /v1.0/gateway/connections/open → WebSocket    sessionWebhook
                   （Stream 模式），JSON 帧，逐条 ACK
企业微信智能机器人  wss://openws.work.weixin.qq.com，JSON 帧           同一条连接
                   （aibot_subscribe 鉴权，ping 心跳）
=================  ===============================================  ==================

每个机器人一个后台任务：断线后按 1s → 2s → … → 60s 指数退避无限重连；连接状态
（已连接 / 连接中 / 失败原因 / 最近一次消息）实时展示在「设置 → 机器人」里。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from automind.bots.channels import CHANNELS, ChannelError, Incoming
from automind.core.logging import get_logger

logger = get_logger("automind.bots.longconn")

#: 每个机器人的长连接状态（供设置页展示）
STATUS: dict[str, dict[str, Any]] = {}
#: 企业微信智能机器人的活连接（回复要走同一条连接）
_WECOM_LIVE: dict[str, Any] = {}

_BACKOFF = (1, 2, 4, 8, 15, 30, 60)


def _set(bot_id: str, **kw: Any) -> None:
    st = STATUS.setdefault(bot_id, {"state": "idle", "error": "", "since": 0.0,
                                    "reconnects": 0, "last_msg": 0.0})
    st.update(kw)


def status(bot_id: str) -> dict[str, Any]:
    return dict(STATUS.get(bot_id) or {"state": "idle", "error": "", "since": 0.0,
                                       "reconnects": 0, "last_msg": 0.0})


def _ws_connect(url: str, **kw: Any) -> Any:
    try:
        import websockets
    except ImportError as e:      # pragma: no cover - 依赖缺失
        raise ChannelError("长连接需要 websockets 库：pip install websockets") from e
    return websockets.connect(url, max_size=8 * 1024 * 1024, open_timeout=20, **kw)


# ═══════════════════════════════════════════════════════════════
# 飞书：pbbp2.Frame 的最小 protobuf 编解码
# ═══════════════════════════════════════════════════════════════
#   message Header { required string key = 1; required string value = 2; }
#   message Frame  { required uint64 SeqID = 1; required uint64 LogID = 2;
#                    required int32 service = 3; required int32 method = 4;
#                    repeated Header headers = 5; optional string payload_encoding = 6;
#                    optional string payload_type = 7; optional bytes payload = 8;
#                    optional string LogIDNew = 9; }

FRAME_CONTROL = 0
FRAME_DATA = 1


def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 截断")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, data: bytes) -> bytes:
    return _key(field, 2) + _varint(len(data)) + data


def encode_frame(f: dict[str, Any]) -> bytes:
    out = bytearray()
    out += _key(1, 0) + _varint(int(f.get("seq_id") or 0))
    out += _key(2, 0) + _varint(int(f.get("log_id") or 0))
    out += _key(3, 0) + _varint(int(f.get("service") or 0))
    out += _key(4, 0) + _varint(int(f.get("method") or 0))
    for k, v in (f.get("headers") or {}).items():
        out += _ld(5, _ld(1, str(k).encode()) + _ld(2, str(v).encode()))
    for field, name in ((6, "payload_encoding"), (7, "payload_type")):
        if f.get(name):
            out += _ld(field, str(f[name]).encode())
    if f.get("payload") is not None:
        out += _ld(8, bytes(f["payload"]))
    if f.get("log_id_new"):
        out += _ld(9, str(f["log_id_new"]).encode())
    return bytes(out)


def _decode_fields(buf: bytes) -> list[tuple[int, int, Any]]:
    i, out = 0, []
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"不支持的 wire type {wire}")
        out.append((field, wire, val))
    return out


def decode_frame(buf: bytes) -> dict[str, Any]:
    f: dict[str, Any] = {"seq_id": 0, "log_id": 0, "service": 0, "method": 0, "headers": {},
                         "payload": b""}
    names = {1: "seq_id", 2: "log_id", 3: "service", 4: "method", 6: "payload_encoding",
             7: "payload_type", 8: "payload", 9: "log_id_new"}
    for field, _wire, val in _decode_fields(buf):
        if field == 5:
            kv = {k: v for k, _w, v in _decode_fields(val)}
            f["headers"][bytes(kv.get(1, b"")).decode()] = bytes(kv.get(2, b"")).decode()
        elif field in (6, 7, 9):
            f[names[field]] = bytes(val).decode()
        elif field == 8:
            f["payload"] = bytes(val)
        elif field in names:
            f[names[field]] = val
    return f


# ═══════════════════════════════════════════════════════════════
# 运行器
# ═══════════════════════════════════════════════════════════════


class LongConnRunner:
    """管理所有长连接机器人的后台任务（随机器人增删改自动启停）。"""

    def __init__(self, on_message: Any) -> None:
        #: async (bot, Incoming) -> None：交给 BotManager 处理
        self._on_message = on_message
        self._tasks: dict[str, asyncio.Task] = {}
        self._sigs: dict[str, str] = {}

    @staticmethod
    def wants_stream(bot: dict[str, Any]) -> bool:
        ch = CHANNELS.get(str(bot.get("channel")))
        if ch is None or "stream" not in ch.transports or not bot.get("enabled", True):
            return False
        return (bot.get("transport") or ch.transports[0]) == "stream"

    def sync(self, bots: list[dict[str, Any]]) -> None:
        """按当前配置启停连接：新增 / 凭据变化的重连，删除 / 停用 / 改回调模式的断开。"""
        want: dict[str, dict[str, Any]] = {}
        for b in bots:
            ch = CHANNELS.get(str(b.get("channel")))
            if self.wants_stream(b) and ch and not ch.missing(b.get("credentials") or {}, "stream"):
                want[b["id"]] = b
            elif self.wants_stream(b):
                _set(b["id"], state="config", error="凭据未填写完整")
            else:
                STATUS.pop(b["id"], None)
        for bid in list(self._tasks):
            sig = json.dumps(want.get(bid, {}).get("credentials"), sort_keys=True) if bid in want else ""
            if bid not in want or sig != self._sigs.get(bid):
                self._tasks.pop(bid).cancel()
                self._sigs.pop(bid, None)
                if bid not in want:
                    _WECOM_LIVE.pop(bid, None)
        for bid, b in want.items():
            if bid not in self._tasks:
                self._sigs[bid] = json.dumps(b.get("credentials"), sort_keys=True)
                self._tasks[bid] = asyncio.create_task(self._loop(b), name=f"bot-ws-{bid}")

    def restart(self, bot: dict[str, Any]) -> None:
        t = self._tasks.pop(bot["id"], None)
        if t:
            t.cancel()
        self._sigs.pop(bot["id"], None)
        self.sync([bot])

    async def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        for t in list(self._tasks.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _loop(self, bot: dict[str, Any]) -> None:
        attempt = 0
        runner = {"feishu": self._feishu, "dingtalk": self._dingtalk,
                  "wecom_bot": self._wecom_bot}[bot["channel"]]
        while True:
            # 不清空上一次的失败原因：重连中界面仍要看得到"为什么连不上"，连上后再清
            _set(bot["id"], state="connecting")
            started = time.monotonic()
            try:
                await runner(bot)
                err = "连接被平台关闭"
            except asyncio.CancelledError:
                _set(bot["id"], state="stopped")
                raise
            except Exception as e:                  # 任何异常都只影响本机器人，退避后重连
                err = f"{type(e).__name__}: {e}"[:300]
                logger.warning("bot_longconn_failed", bot=bot["id"], channel=bot["channel"], error=err)
            # 连上过一阵子再断的，重新从 1 秒开始退避
            attempt = 0 if time.monotonic() - started > 120 else attempt + 1
            wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            st = status(bot["id"])
            _set(bot["id"], state="retrying", error=err, reconnects=st.get("reconnects", 0) + 1,
                 retry_in=wait)
            await asyncio.sleep(wait)

    async def _deliver(self, bot: dict[str, Any], msg: Incoming | None) -> None:
        if msg is None or not msg.text:
            return
        _set(bot["id"], last_msg=time.time())
        try:
            await self._on_message(bot, msg)
        except Exception as e:                      # 处理失败不能拖垮连接
            logger.warning("bot_longconn_dispatch_failed", bot=bot["id"], error=str(e))

    # ── 飞书 ───────────────────────────────────────────────────

    async def _feishu(self, bot: dict[str, Any]) -> None:
        from automind.bots.channels import _post_json
        cred = bot["credentials"]
        base = str(cred.get("api_base") or "https://open.feishu.cn").rstrip("/")
        d = await _post_json(f"{base}/callback/ws/endpoint",
                             {"AppID": cred.get("app_id"), "AppSecret": cred.get("app_secret")},
                             headers={"locale": "zh"})
        if d.get("code") not in (0, None) or not (d.get("data") or {}).get("URL"):
            raise ChannelError(f"获取飞书长连接地址失败：{d.get('msg') or d}（检查 App ID / Secret，"
                               "并确认应用已开通机器人能力）")
        url = d["data"]["URL"]
        conf = d["data"].get("ClientConfig") or {}
        ping_every = max(10, int(conf.get("PingInterval") or 120))
        q = parse_qs(urlparse(url).query)
        service = int((q.get("service_id") or ["0"])[0] or 0)
        parts: dict[str, list[bytes | None]] = {}
        ch = CHANNELS["feishu"]
        async with _ws_connect(url, ping_interval=None) as ws:
            _set(bot["id"], state="connected", error="", since=time.time())
            logger.info("bot_longconn_connected", bot=bot["id"], channel="feishu")

            async def pinger() -> None:
                while True:
                    await asyncio.sleep(ping_every)
                    await ws.send(encode_frame({"service": service, "method": FRAME_CONTROL,
                                                "headers": {"type": "ping"}}))
            ptask = asyncio.create_task(pinger())
            try:
                async for raw in ws:
                    if isinstance(raw, str):
                        continue
                    frame = decode_frame(raw)
                    h = frame["headers"]
                    if frame["method"] == FRAME_CONTROL:
                        if h.get("type") == "pong" and frame["payload"]:
                            try:
                                c = json.loads(frame["payload"].decode())
                                ping_every = max(10, int(c.get("PingInterval") or ping_every))
                            except ValueError:
                                pass
                        continue
                    t0 = time.monotonic()
                    payload = frame["payload"]
                    total = int(h.get("sum") or 1)
                    if total > 1:
                        mid = h.get("message_id", "")
                        buf = parts.setdefault(mid, [None] * total)
                        buf[int(h.get("seq") or 0)] = payload
                        if any(p is None for p in buf):
                            continue
                        payload = b"".join(p for p in buf if p is not None)
                        parts.pop(mid, None)
                    msg = None
                    if h.get("type") == "event":
                        try:
                            msg = ch.parse_event(json.loads(payload.decode("utf-8")))
                        except ValueError:
                            msg = None
                    # 先回 ACK（平台 3 秒内收不到会重推），任务在后台跑
                    frame["headers"] = {**h, "biz_rt": str(int((time.monotonic() - t0) * 1000))}
                    frame["payload"] = json.dumps({"code": 200, "headers": {}, "data": ""}).encode()
                    await ws.send(encode_frame(frame))
                    asyncio.create_task(self._deliver(bot, msg))
            finally:
                ptask.cancel()

    # ── 钉钉 Stream ────────────────────────────────────────────

    DINGTALK_BOT_TOPIC = "/v1.0/im/bot/messages/get"

    async def _dingtalk(self, bot: dict[str, Any]) -> None:
        from automind import __version__
        from automind.bots.channels import DingTalkChannel, _post_json
        cred = bot["credentials"]
        try:
            d = await _post_json("https://api.dingtalk.com/v1.0/gateway/connections/open", {
                "clientId": cred.get("app_key"), "clientSecret": cred.get("app_secret"),
                "subscriptions": [{"type": "CALLBACK", "topic": self.DINGTALK_BOT_TOPIC}],
                "ua": f"automind/{__version__}"})
        except ChannelError as e:
            if "authFailed" in str(e) or "401" in str(e):
                raise ChannelError("钉钉鉴权失败：Client ID（AppKey）或 Client Secret 不正确") from e
            raise
        if not d.get("endpoint") or not d.get("ticket"):
            raise ChannelError(f"钉钉 Stream 连接注册失败：{d.get('message') or d}"
                               "（检查 Client ID / Secret，并确认机器人已选 Stream 模式）")
        url = f"{d['endpoint']}?ticket={quote_plus(d['ticket'])}"
        async with _ws_connect(url, ping_interval=30, ping_timeout=30) as ws:
            _set(bot["id"], state="connected", error="", since=time.time())
            logger.info("bot_longconn_connected", bot=bot["id"], channel="dingtalk")
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                headers = m.get("headers") or {}
                topic = headers.get("topic", "")
                ack = {"code": 200, "headers": {"contentType": "application/json",
                                                "messageId": headers.get("messageId", "")},
                       "message": "OK", "data": m.get("data") or ""}
                if m.get("type") == "SYSTEM":
                    await ws.send(json.dumps(ack))
                    if topic == "disconnect":
                        return                        # 平台要求重连：退出后由外层立即重连
                    continue
                if m.get("type") == "CALLBACK" and topic == self.DINGTALK_BOT_TOPIC:
                    ack["data"] = json.dumps({"response": None})
                    await ws.send(json.dumps(ack))
                    try:
                        body = json.loads(m.get("data") or "{}")
                    except ValueError:
                        continue
                    asyncio.create_task(self._deliver(bot, DingTalkChannel.parse_message(body)))
                    continue
                ack["data"] = json.dumps({"response": None})
                await ws.send(json.dumps(ack))

    # ── 企业微信智能机器人 ──────────────────────────────────────

    async def _wecom_bot(self, bot: dict[str, Any]) -> None:
        cred = bot["credentials"]
        url = str(cred.get("ws_url") or "wss://openws.work.weixin.qq.com")
        async with _ws_connect(url, ping_interval=None) as ws:
            sub_id = uuid.uuid4().hex
            await ws.send(json.dumps({"cmd": "aibot_subscribe", "headers": {"req_id": sub_id},
                                      "body": {"bot_id": cred.get("bot_id"), "secret": cred.get("secret")}}))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if int(first.get("errcode") or 0) != 0:
                raise ChannelError(f"企业微信智能机器人鉴权失败：{first.get('errmsg') or first}"
                                   "（检查 Bot ID / Secret）")
            live = {"ws": ws, "lock": asyncio.Lock()}
            _WECOM_LIVE[bot["id"]] = live
            _set(bot["id"], state="connected", error="", since=time.time())
            logger.info("bot_longconn_connected", bot=bot["id"], channel="wecom_bot")

            async def pinger() -> None:
                while True:
                    await asyncio.sleep(30)
                    async with live["lock"]:
                        await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": uuid.uuid4().hex}}))
            ptask = asyncio.create_task(pinger())
            try:
                async for raw in ws:
                    try:
                        m = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if m.get("cmd") != "aibot_msg_callback":
                        continue
                    asyncio.create_task(self._deliver(bot, parse_wecom_bot(m)))
            finally:
                ptask.cancel()
                if _WECOM_LIVE.get(bot["id"]) is live:
                    _WECOM_LIVE.pop(bot["id"], None)


def parse_wecom_bot(frame: dict[str, Any]) -> Incoming | None:
    body = frame.get("body") or {}
    if body.get("msgtype") != "text":
        return None
    text = str((body.get("text") or {}).get("content") or "").strip()
    if text.startswith("@"):                      # 群里 "@机器人 内容"
        text = text.split(" ", 1)[1].strip() if " " in text else ""
    user = str((body.get("from") or {}).get("userid") or "")
    group = body.get("chattype") == "group"
    chat = str(body.get("chatid") or user)
    return Incoming(chat_id=chat, user_id=user, text=text, msg_id=str(body.get("msgid") or ""),
                    is_group=group,
                    reply={"req_id": (frame.get("headers") or {}).get("req_id", ""), "chatid": chat})


async def wecom_bot_send(bot: dict[str, Any], msg: Incoming, text: str, title: str = "") -> None:
    """第一条用 aibot_respond_msg（流式、finish=true）回复这条消息；之后主动推送 markdown。

    v1.11.14：正文统一过一遍 :func:`adapt_for_channel`（表格转列表、图片转链接、
    HTML 去标签），标题渲染成正文首行 ``## 标题`` —— 智能机器人 markdown 上限 20480 字节，
    围栏代码块保留；超长分片由 :class:`~automind.bots.manager.BotManager` 统一处理。
    """
    from automind.bots.md_format import adapt_for_channel

    body = adapt_for_channel("wecom_bot", text, title=title)
    live = _WECOM_LIVE.get(bot["id"])
    if not live:
        raise ChannelError("企业微信智能机器人长连接未建立，消息暂时发不出去", status=503)
    if msg.reply.get("req_id") and not msg.reply.get("responded"):
        msg.reply["responded"] = True
        frame = {"cmd": "aibot_respond_msg", "headers": {"req_id": msg.reply["req_id"]},
                 "body": {"msgtype": "stream",
                          "stream": {"id": uuid.uuid4().hex, "finish": True, "content": body}}}
    else:
        frame = {"cmd": "aibot_send_msg", "headers": {"req_id": uuid.uuid4().hex},
                 "body": {"chatid": msg.reply.get("chatid") or msg.chat_id, "msgtype": "markdown",
                          "markdown": {"content": body}}}
    async with live["lock"]:
        await live["ws"].send(json.dumps(frame, ensure_ascii=False))
