"""IM 渠道适配（v1.11.6）：把各平台的回调统一成 ``Incoming``，把回复按平台要求发回去。

每个渠道两件事：

* ``handle(bot, req) -> (HttpReply, Incoming | None)``：验签 / 解密 / 平台握手（URL 校验），
  返回给平台的 HTTP 应答，以及解析出的用户消息（握手类请求没有消息）。
  **平台都要求几秒内应答**，所以这里只解析、不执行；任务在后台跑，结果主动推回去。
* ``send(bot, msg, text, title)``：把一段 Markdown / 文本发回到这条消息所在的会话。

支持的渠道：飞书、钉钉、企业微信（自建应用）、微信公众号（含测试号）、通用 Webhook。
个人微信没有官方机器人接口 —— 可用 wechaty 等框架接收消息后转发到「通用 Webhook」。
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from automind.bots import crypto
from automind.bots.md_format import adapt_for_channel, plain_text, render_title
from automind.core.logging import get_logger

logger = get_logger("automind.bots.channels")


@dataclass
class HttpReq:
    method: str
    query: dict[str, str]
    headers: dict[str, str]          # 键一律小写
    body: bytes


@dataclass
class HttpReply:
    status: int = 200
    body: Any = ""                   # dict → JSON；str → 文本
    content_type: str = ""


@dataclass
class Incoming:
    """一条用户消息（与平台无关的形态）。"""

    chat_id: str                     # 会话（群 / 单聊）标识：同一会话的消息共享上下文
    user_id: str
    text: str
    user_name: str = ""
    msg_id: str = ""
    is_group: bool = False
    #: 回复时要用的平台上下文（飞书 message_id、钉钉 sessionWebhook、企微 userid ……）
    reply: dict[str, Any] = field(default_factory=dict)


class ChannelError(Exception):
    def __init__(self, msg: str, status: int = 400) -> None:
        super().__init__(msg)
        self.status = status


def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as e:
        raise ChannelError("请求体不是合法 JSON") from e
    if not isinstance(data, dict):
        raise ChannelError("请求体应为 JSON 对象")
    return data


def _xml(text: str) -> dict[str, str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ChannelError("请求体不是合法 XML") from e
    return {child.tag: (child.text or "") for child in root}


def split_utf8(text: str, limit_bytes: int) -> list[str]:
    """按 UTF-8 字节数切分（企业微信 / 公众号的上限按字节算），优先在换行处断开。"""
    out: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit_bytes:
            # 单行就超长：按字符硬切
            cut = ""
            for ch in line:
                if len((cut + ch).encode("utf-8")) > limit_bytes:
                    break
                cut += ch
            if cur:
                out.append(cur)
                cur = ""
            out.append(cut)
            line = line[len(cut):]
        if len((cur + line).encode("utf-8")) > limit_bytes:
            out.append(cur)
            cur = line
        else:
            cur += line
    if cur.strip():
        out.append(cur)
    return [c.rstrip("\n") for c in out if c.strip()] or [""]


# ═══════════════════════════════════════════════════════════════
# 发送用的 HTTP 与令牌缓存
# ═══════════════════════════════════════════════════════════════

_tokens: dict[str, tuple[str, float]] = {}


async def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None,
                     timeout: float = 15.0) -> dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=payload, headers=headers or {})
    try:
        data = r.json()
    except ValueError:
        data = {"_text": r.text[:300]}
    if r.status_code >= 400:
        raise ChannelError(f"HTTP {r.status_code}：{str(data)[:300]}", status=502)
    return data if isinstance(data, dict) else {"_data": data}


async def _get_json(url: str, params: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, params=params)
    data = r.json()
    return data if isinstance(data, dict) else {}


def _cached(key: str) -> str:
    tok, exp = _tokens.get(key, ("", 0.0))
    return tok if tok and time.time() < exp - 120 else ""


# ═══════════════════════════════════════════════════════════════
# 渠道基类
# ═══════════════════════════════════════════════════════════════


class Channel:
    key = ""
    label = ""
    icon = "🤖"
    #: 单条消息的字节上限（超过就分条发）
    limit_bytes = 15000
    #: 界面表单：每项 (键, 名称, 是否敏感, 是否必填, 说明)
    fields: tuple[tuple[str, str, bool, bool, str], ...] = ()
    #: 平台侧配置步骤（界面上照着做）
    guide: tuple[str, ...] = ()
    #: v1.11.7 接收方式：stream = 本机主动连平台的长连接（**不需要公网地址**）；
    #: webhook = 平台回调本机（需要公网可达）。第一个是默认值。
    transports: tuple[str, ...] = ("webhook",)
    #: 长连接模式下的配置步骤
    stream_guide: tuple[str, ...] = ()
    #: 只在长连接模式下必填的字段
    stream_required: tuple[str, ...] = ()

    def spec(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "icon": self.icon,
                "fields": [{"key": k, "label": lab, "secret": sec, "required": req, "help": h}
                           for k, lab, sec, req, h in self.fields],
                "guide": list(self.guide), "stream_guide": list(self.stream_guide),
                "transports": list(self.transports), "stream_required": list(self.stream_required)}

    def missing(self, creds: dict[str, Any], transport: str = "") -> list[str]:
        transport = transport or self.transports[0]
        out = []
        for k, lab, _s, req, _h in self.fields:
            need = req or (transport == "stream" and k in self.stream_required)
            if need and not str(creds.get(k) or "").strip():
                out.append(lab)
        return out

    async def handle(self, bot: dict[str, Any], req: HttpReq) -> tuple[HttpReply, Incoming | None]:
        raise NotImplementedError

    async def send(self, bot: dict[str, Any], msg: Incoming, text: str, title: str = "") -> None:
        raise NotImplementedError


# ── 飞书 ──────────────────────────────────────────────────────


class FeishuChannel(Channel):
    key = "feishu"
    label = "飞书"
    icon = "🪶"
    limit_bytes = 20000
    fields = (
        ("app_id", "App ID", False, True, "开发者后台 → 凭证与基础信息"),
        ("app_secret", "App Secret", True, True, "同上"),
        ("verification_token", "Verification Token", True, False, "事件与回调 → 加密策略（填了就校验）"),
        ("encrypt_key", "Encrypt Key", True, False, "事件与回调 → 加密策略（开启加密时必填）"),
        ("api_base", "API 地址", False, False, "默认 https://open.feishu.cn；Lark 国际版填 https://open.larksuite.com"),
    )
    transports = ("stream", "webhook")
    stream_guide = (
        "飞书开放平台 → 创建企业自建应用 → 添加「机器人」能力，复制 App ID / App Secret 填到这里并保存",
        "权限管理：开通 im:message、im:message.group_at_msg、im:message:send_as_bot",
        "事件与回调 → 订阅方式选「使用长连接接收事件」（先在这里看到「已连接」，平台才允许保存该选项）",
        "添加事件 im.message.receive_v1，发布应用版本；把机器人拉进群或直接单聊下达任务",
    )
    guide = (
        "飞书开放平台 → 创建企业自建应用 → 添加「机器人」能力",
        "权限管理：开通 im:message（获取与发送单聊、群组消息）、im:message.group_at_msg（群里 @ 机器人）",
        "事件与回调 → 请求地址填下面的「回调地址」，订阅事件 im.message.receive_v1",
        "发布应用版本，把机器人拉进群或直接单聊即可下达任务",
    )
    _seen: dict[str, float] = {}

    def _api(self, bot: dict[str, Any]) -> str:
        return str(bot["credentials"].get("api_base") or "https://open.feishu.cn").rstrip("/")

    async def handle(self, bot, req):
        cred = bot["credentials"]
        data = _json(req.body)
        enc_key = str(cred.get("encrypt_key") or "")
        if "encrypt" in data:
            if not enc_key:
                raise ChannelError("飞书推送的是加密消息，但没有配置 Encrypt Key")
            sig = req.headers.get("x-lark-signature", "")
            if sig:
                expect = crypto.feishu_signature(req.headers.get("x-lark-request-timestamp", ""),
                                                 req.headers.get("x-lark-request-nonce", ""),
                                                 enc_key, req.body)
                if expect != sig:
                    raise ChannelError("飞书请求签名校验失败", status=401)
            try:
                data = json.loads(crypto.feishu_decrypt(enc_key, str(data["encrypt"])))
            except crypto.CryptoUnavailable:
                raise
            except Exception as e:
                raise ChannelError(f"飞书消息解密失败（Encrypt Key 不匹配？）：{e}", status=401) from e
        token = data.get("token") or (data.get("header") or {}).get("token") or ""
        want = str(cred.get("verification_token") or "")
        if want and token != want:
            raise ChannelError("Verification Token 不匹配", status=401)
        if data.get("type") == "url_verification":
            return HttpReply(body={"challenge": data.get("challenge", "")}), None
        return HttpReply(body={"ok": True}), self.parse_event(data)

    def parse_event(self, data: dict[str, Any]) -> Incoming | None:
        """事件体（schema 2.0）→ Incoming。回调与长连接共用；同一事件只处理一次。"""
        header = data.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        eid = str(header.get("event_id") or "")
        now = time.time()
        if eid and eid in self._seen:               # 飞书超时会重推同一事件
            return None
        if eid:
            self._seen[eid] = now
        for k, t in list(self._seen.items()):
            if now - t > 3600:
                self._seen.pop(k, None)
        ev = data.get("event") or {}
        m = ev.get("message") or {}
        if m.get("message_type") != "text":
            return Incoming(chat_id=str(m.get("chat_id") or ""), user_id=_feishu_sender(ev), text="",
                            msg_id=str(m.get("message_id") or ""),
                            reply={"message_id": m.get("message_id")})
        try:
            text = str(json.loads(m.get("content") or "{}").get("text") or "")
        except ValueError:
            text = ""
        text = re.sub(r"@_user_\d+", "", text).strip()     # 群里 @ 机器人的占位
        return Incoming(
            chat_id=str(m.get("chat_id") or ""), user_id=_feishu_sender(ev), text=text,
            msg_id=str(m.get("message_id") or ""), is_group=m.get("chat_type") == "group",
            reply={"message_id": m.get("message_id"), "chat_id": m.get("chat_id")})

    async def _token(self, bot) -> str:
        cred = bot["credentials"]
        key = f"feishu:{cred.get('app_id')}"
        tok = _cached(key)
        if tok:
            return tok
        d = await _post_json(f"{self._api(bot)}/open-apis/auth/v3/tenant_access_token/internal",
                             {"app_id": cred.get("app_id"), "app_secret": cred.get("app_secret")})
        if d.get("code") not in (0, None) or not d.get("tenant_access_token"):
            raise ChannelError(f"飞书获取 tenant_access_token 失败：{d.get('msg') or d}", status=502)
        _tokens[key] = (d["tenant_access_token"], time.time() + float(d.get("expire") or 7200))
        return d["tenant_access_token"]

    async def send(self, bot, msg, text, title=""):
        tok = await self._token(bot)
        body = adapt_for_channel(self.key, text)
        card = {"config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": body}]}
        if title:
            head = render_title(self.key, title)
            if head:
                card["header"] = {"title": {"tag": "plain_text", "content": head}}
        payload = {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        mid = msg.reply.get("message_id")
        if mid:
            url = f"{self._api(bot)}/open-apis/im/v1/messages/{mid}/reply"
        else:
            url = f"{self._api(bot)}/open-apis/im/v1/messages?receive_id_type=chat_id"
            payload["receive_id"] = msg.chat_id
        d = await _post_json(url, payload, headers={"Authorization": f"Bearer {tok}"})
        if d.get("code") not in (0, None):
            raise ChannelError(f"飞书发送失败：{d.get('msg') or d}", status=502)


def _feishu_sender(ev: dict[str, Any]) -> str:
    sid = ((ev.get("sender") or {}).get("sender_id") or {})
    return str(sid.get("open_id") or sid.get("user_id") or sid.get("union_id") or "")


# ── 钉钉 ──────────────────────────────────────────────────────


class DingTalkChannel(Channel):
    key = "dingtalk"
    label = "钉钉"
    icon = "📌"
    limit_bytes = 15000
    fields = (
        ("app_key", "AppKey（Client ID）", False, False, "应用凭证里的 Client ID；长连接模式必填"),
        ("app_secret", "AppSecret（Client Secret）", True, True, "应用凭证里的 Client Secret"),
    )
    transports = ("stream", "webhook")
    stream_required = ("app_key",)
    stream_guide = (
        "钉钉开放平台 → 创建企业内部应用 → 添加「机器人」能力，复制 Client ID / Client Secret 填到这里",
        "机器人配置 → 消息接收模式选「Stream 模式」（本机主动连钉钉，不需要公网地址）",
        "发布机器人，在群里 @ 机器人或单聊下达任务",
    )
    guide = (
        "钉钉开放平台 → 创建企业内部应用 → 添加「机器人」能力",
        "消息接收模式选「HTTP 模式」，消息接收地址填下面的「回调地址」",
        "发布机器人，在群里 @ 机器人或单聊下达任务（回复通过消息里的 sessionWebhook 发回）",
    )

    async def handle(self, bot, req):
        secret = str(bot["credentials"].get("app_secret") or "")
        ts = req.headers.get("timestamp", "")
        sign = req.headers.get("sign", "")
        if not ts or not sign:
            raise ChannelError("缺少钉钉签名头 timestamp / sign", status=401)
        try:
            skew = abs(time.time() * 1000 - int(ts))
        except ValueError as e:
            raise ChannelError("timestamp 不合法", status=401) from e
        if skew > 3600 * 1000:
            raise ChannelError("钉钉请求已过期", status=401)
        if crypto.dingtalk_sign(secret, ts) != sign:
            raise ChannelError("钉钉签名校验失败（AppSecret 不匹配？）", status=401)
        return HttpReply(body={"msgtype": "empty"}), self.parse_message(_json(req.body))

    @staticmethod
    def parse_message(d: dict[str, Any]) -> Incoming:
        """机器人消息体 → Incoming（HTTP 回调与 Stream 长连接的消息体相同）。"""
        text = str(((d.get("text") or {}).get("content")) or "").strip()
        return Incoming(
            chat_id=str(d.get("conversationId") or ""),
            user_id=str(d.get("senderStaffId") or d.get("senderId") or ""),
            user_name=str(d.get("senderNick") or ""), text=text,
            msg_id=str(d.get("msgId") or ""), is_group=str(d.get("conversationType")) == "2",
            reply={"webhook": d.get("sessionWebhook"),
                   "expires": d.get("sessionWebhookExpiredTime"),
                   "at": d.get("senderStaffId") or ""})

    async def send(self, bot, msg, text, title=""):
        url = msg.reply.get("webhook")
        if not url:
            raise ChannelError("这条钉钉消息没有 sessionWebhook，无法回复", status=502)
        exp = msg.reply.get("expires")
        if exp and time.time() * 1000 > float(exp):
            raise ChannelError("钉钉 sessionWebhook 已过期（约 1.5 小时有效），请重新 @ 机器人", status=502)
        body = adapt_for_channel(self.key, text)
        head = render_title(self.key, title) or "AutoMind"
        d = await _post_json(url, {"msgtype": "markdown",
                                   "markdown": {"title": head, "text": body}})
        if d.get("errcode") not in (0, None):
            raise ChannelError(f"钉钉发送失败：{d.get('errmsg') or d}", status=502)


# ── 企业微信（自建应用）─────────────────────────────────────────


class WeComChannel(Channel):
    key = "wecom"
    label = "企业微信"
    icon = "💼"
    limit_bytes = 2000            # 应用消息 markdown 内容上限 2048 字节
    fields = (
        ("corp_id", "企业 ID（CorpID）", False, True, "管理后台 → 我的企业"),
        ("agent_id", "AgentId", False, True, "应用管理 → 自建应用"),
        ("secret", "Secret", True, True, "自建应用的 Secret"),
        ("token", "Token", True, True, "应用 → 接收消息 → 设置 API 接收"),
        ("encoding_aes_key", "EncodingAESKey", True, True, "同上（43 位）"),
    )
    guide = (
        "（推荐改用「企业微信智能机器人」渠道：长连接接入，不需要公网地址、也不需要配置可信 IP）",
        "企业微信管理后台 → 应用管理 → 创建自建应用",
        "应用 → 接收消息 → 设置 API 接收：URL 填下面的「回调地址」，Token / EncodingAESKey 与这里一致",
        "应用 → 企业可信 IP：加上本机的公网出口 IP（否则发消息会被拒）",
        "在企业微信里打开该应用直接发消息下达任务",
    )

    def _crypt(self, bot) -> crypto.WXCrypt:
        c = bot["credentials"]
        return crypto.WXCrypt(str(c.get("token") or ""), str(c.get("encoding_aes_key") or ""),
                              str(c.get("corp_id") or ""))

    async def handle(self, bot, req):
        q = req.query
        wx = self._crypt(bot)
        if req.method == "GET":
            echo = q.get("echostr", "")
            if not wx.verify(q.get("msg_signature", ""), q.get("timestamp", ""), q.get("nonce", ""), echo):
                raise ChannelError("企业微信 URL 校验签名不匹配（Token 不一致？）", status=401)
            return HttpReply(body=wx.decrypt(echo), content_type="text/plain"), None
        outer = _xml(req.body.decode("utf-8", errors="replace"))
        enc = outer.get("Encrypt", "")
        if not wx.verify(q.get("msg_signature", ""), q.get("timestamp", ""), q.get("nonce", ""), enc):
            raise ChannelError("企业微信消息签名不匹配", status=401)
        inner = _xml(wx.decrypt(enc))
        if inner.get("MsgType") != "text":
            return HttpReply(body="success", content_type="text/plain"), None
        return HttpReply(body="success", content_type="text/plain"), Incoming(
            chat_id=inner.get("FromUserName", ""), user_id=inner.get("FromUserName", ""),
            text=inner.get("Content", "").strip(), msg_id=inner.get("MsgId", ""),
            reply={"touser": inner.get("FromUserName", "")})

    async def _token(self, bot) -> str:
        c = bot["credentials"]
        key = f"wecom:{c.get('corp_id')}:{c.get('agent_id')}"
        tok = _cached(key)
        if tok:
            return tok
        d = await _get_json("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                            {"corpid": c.get("corp_id"), "corpsecret": c.get("secret")})
        if d.get("errcode") or not d.get("access_token"):
            raise ChannelError(f"企业微信获取 access_token 失败：{d.get('errmsg') or d}", status=502)
        _tokens[key] = (d["access_token"], time.time() + float(d.get("expires_in") or 7200))
        return d["access_token"]

    async def send(self, bot, msg, text, title=""):
        tok = await self._token(bot)
        body = adapt_for_channel(self.key, text, title=title)
        d = await _post_json(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
            {"touser": msg.reply.get("touser") or msg.user_id, "msgtype": "markdown",
             "agentid": int(str(bot["credentials"].get("agent_id") or 0) or 0),
             "markdown": {"content": body}})
        if d.get("errcode") not in (0, None):
            raise ChannelError(f"企业微信发送失败：{d.get('errmsg') or d}", status=502)


# ── 企业微信智能机器人（长连接）────────────────────────────────


class WeComBotChannel(Channel):
    key = "wecom_bot"
    label = "企业微信智能机器人"
    icon = "🤝"
    limit_bytes = 20000           # 流式 / markdown 内容上限 20480 字节
    transports = ("stream",)
    fields = (
        ("bot_id", "Bot ID", False, True, "智能机器人 → API 模式（长连接）里的 BotID"),
        ("secret", "Secret", True, True, "同上，只显示一次，请妥善保存"),
        ("ws_url", "长连接地址", False, False, "默认 wss://openws.work.weixin.qq.com；私有部署企业在管理端查看"),
    )
    stream_guide = (
        "企业微信客户端 → 工作台 → 智能机器人 → 创建机器人 → 选「API 模式」→ 连接方式选「长连接」",
        "复制 Bot ID 与 Secret 填到这里并保存，看到「已连接」即可",
        "单聊机器人，或把它拉进群 @ 它下达任务（不需要公网地址、不需要配置可信 IP）",
    )

    async def handle(self, bot, req):
        raise ChannelError("企业微信智能机器人使用长连接接收消息，不需要回调地址", status=404)

    async def send(self, bot, msg, text, title=""):
        from automind.bots.longconn import wecom_bot_send
        await wecom_bot_send(bot, msg, text, title=title)


# ── 微信公众号（服务号 / 测试号）────────────────────────────────


class WeChatMPChannel(Channel):
    key = "wechat_mp"
    label = "微信（公众号）"
    icon = "💬"
    limit_bytes = 1900            # 客服消息文本上限约 2048 字节
    fields = (
        ("app_id", "AppID", False, True, "公众号 / 测试号的 AppID"),
        ("app_secret", "AppSecret", True, True, "用于获取 access_token 发送客服消息"),
        ("token", "Token", True, True, "服务器配置里填的 Token"),
        ("encoding_aes_key", "EncodingAESKey", True, False, "消息加解密方式选「安全模式」时必填"),
    )
    guide = (
        "微信公众平台（或「接口测试号」）→ 基本配置 → 服务器配置：URL 填下面的「回调地址」，Token 与这里一致",
        "IP 白名单加上本机的公网出口 IP",
        "关注公众号后直接发消息下达任务；结果通过客服消息接口推送（需认证服务号或测试号，48 小时内有互动）",
        "个人微信没有官方机器人接口：可用 wechaty 等框架收消息后转发到「通用 Webhook」渠道",
    )

    async def handle(self, bot, req):
        c = bot["credentials"]
        q = req.query
        token = str(c.get("token") or "")
        if not hmac_eq(crypto.wx_signature(token, q.get("timestamp", ""), q.get("nonce", "")),
                       q.get("signature", "")):
            raise ChannelError("微信签名校验失败（Token 不一致？）", status=401)
        if req.method == "GET":
            return HttpReply(body=q.get("echostr", ""), content_type="text/plain"), None
        outer = _xml(req.body.decode("utf-8", errors="replace"))
        if q.get("encrypt_type") == "aes" or "Encrypt" in outer:
            wx = crypto.WXCrypt(token, str(c.get("encoding_aes_key") or ""), str(c.get("app_id") or ""))
            enc = outer.get("Encrypt", "")
            if not wx.verify(q.get("msg_signature", ""), q.get("timestamp", ""), q.get("nonce", ""), enc):
                raise ChannelError("微信安全模式消息签名不匹配", status=401)
            outer = _xml(wx.decrypt(enc))
        if outer.get("MsgType") != "text":
            return HttpReply(body="success", content_type="text/plain"), None
        return HttpReply(body="success", content_type="text/plain"), Incoming(
            chat_id=outer.get("FromUserName", ""), user_id=outer.get("FromUserName", ""),
            text=outer.get("Content", "").strip(), msg_id=outer.get("MsgId", ""),
            reply={"openid": outer.get("FromUserName", "")})

    async def _token(self, bot) -> str:
        c = bot["credentials"]
        key = f"wxmp:{c.get('app_id')}"
        tok = _cached(key)
        if tok:
            return tok
        d = await _get_json("https://api.weixin.qq.com/cgi-bin/token",
                            {"grant_type": "client_credential", "appid": c.get("app_id"),
                             "secret": c.get("app_secret")})
        if d.get("errcode") or not d.get("access_token"):
            raise ChannelError(f"微信获取 access_token 失败：{d.get('errmsg') or d}", status=502)
        _tokens[key] = (d["access_token"], time.time() + float(d.get("expires_in") or 7200))
        return d["access_token"]

    async def send(self, bot, msg, text, title=""):
        tok = await self._token(bot)
        body = adapt_for_channel(self.key, text, title=title)
        d = await _post_json(
            f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={tok}",
            {"touser": msg.reply.get("openid") or msg.user_id, "msgtype": "text",
             "text": {"content": body}})
        if d.get("errcode") not in (0, None):
            raise ChannelError(f"微信客服消息发送失败：{d.get('errmsg') or d}", status=502)


def hmac_eq(a: str, b: str) -> bool:
    import hmac as _h
    return _h.compare_digest(a or "", b or "")


def _plain(md: str) -> str:
    """公众号文本消息不渲染 Markdown：去掉标记、保留结构（v1.11.14 起由适配层实现）。"""
    return plain_text(md)


# ── 通用 Webhook ───────────────────────────────────────────────


class WebhookChannel(Channel):
    key = "webhook"
    label = "通用 Webhook"
    icon = "🔗"
    limit_bytes = 60000
    fields = (
        ("callback_url", "结果回推地址", False, False,
         "可选：执行过程与结果以 JSON POST 到这里（{chat_id, kind, title, text}）"),
        ("secret", "调用密钥", True, False, "可选：填了则请求头需带 X-Bot-Secret"),
    )
    guide = (
        "向「回调地址」POST JSON：{\"text\": \"任务内容\", \"chat_id\": \"会话标识\", \"user_id\": \"发送人\"}",
        "带 \"wait\": true 时同步等任务结束，响应里直接返回全部回复（最长 10 分钟）",
        "也可配置「结果回推地址」，过程与结果会逐条 POST 过去 —— 适合对接个人微信框架（wechaty）、Slack、自建系统",
    )

    async def handle(self, bot, req):
        secret = str(bot["credentials"].get("secret") or "")
        if secret and not hmac_eq(req.headers.get("x-bot-secret", ""), secret):
            raise ChannelError("X-Bot-Secret 不匹配", status=401)
        d = _json(req.body)
        text = str(d.get("text") or "").strip()
        if not text:
            raise ChannelError("缺少 text")
        chat = str(d.get("chat_id") or d.get("user_id") or "webhook")
        return HttpReply(body={"ok": True, "accepted": True}), Incoming(
            chat_id=chat[:120], user_id=str(d.get("user_id") or chat)[:120],
            user_name=str(d.get("user_name") or "")[:60], text=text,
            msg_id=str(d.get("msg_id") or ""), reply={"wait": bool(d.get("wait"))})

    async def send(self, bot, msg, text, title=""):
        url = str(bot["credentials"].get("callback_url") or "")
        if not url:
            return                       # 没配回推：结果只进日志 / 同步响应
        from automind.tools._toolkit import check_url
        # 回推地址是管理员在设置里配的（不是消息里带来的），允许内网系统
        check_url(url, allow_private=True)
        await _post_json(url, {"bot": bot.get("id"), "chat_id": msg.chat_id, "user_id": msg.user_id,
                               "kind": msg.reply.get("kind", "message"), "title": title, "text": text})


CHANNELS: dict[str, Channel] = {c.key: c for c in (
    FeishuChannel(), DingTalkChannel(), WeComBotChannel(), WeComChannel(), WeChatMPChannel(),
    WebhookChannel())}


def get_channel(key: str) -> Channel:
    ch = CHANNELS.get(key)
    if ch is None:
        raise ChannelError(f"不支持的渠道：{key}")
    return ch
