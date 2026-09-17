"""出站事件投递（Webhook）—— 让平台主动把状态说出去，而不是等人来轮询。

为什么要有它：任务完成 / 失败 / 审批请求此前**只有两条出路** —— Web 界面上的
WebSocket 推送，和用户自己去翻日志。客户系统（工单、钉钉、飞书、SIEM）拿不到
任何信号：任务跑完了没人知道，审批要批也只能回到 AutoMind 的弹窗前面点一下，
"在我们的工单系统里批"这种要求根本无从实现。本模块解决这两件事：

  · **出站通知**：把任务生命周期与审批事件按 HTTP POST 推给客户配置的目标；
  · **外部审批回执**：把外部系统回传的审批结果规范化成执行器能直接吃的形状
    （见 :func:`build_approval_receipt`），配合父模块接出的
    ``POST /api/approvals/{approval_id}`` 端点，就能在工单系统里批准/拒绝。

设计取向（每一条都是为了不把"通知"变成"新的故障源"）：

  · **默认关闭**：没配 URL 就一个字节都不发、零成本。通知的价值因人而异，
    不该让不用的用户为它付出任何东西。
  · **绝不阻塞任务主链路**：:meth:`WebhookDispatcher.emit` 只做入队，网络请求
    由后台协程发。投递失败（超时、连不上、对端 5xx）**绝不影响任务结果**。
  · **有界队列 + 丢弃计数**：对端挂了会让队列堆积。队列满时**丢弃并计数 +
    记 warning**，绝不静默丢事件、也绝不无限增长把进程内存吃光。
  · **指数退避重试**，次数可配；4xx（除 408/429）视为**永久失败**不重试 ——
    对一个明确拒绝你的地址重试 5 次只是浪费任务的时间。
  · **可签名**：``X-AutoMind-Signature: sha256=<hmac>``（HMAC-SHA256 覆盖原始
    body），接收方据此确认"这条确实来自 AutoMind、且没被篡改"。
  · **可注入传输**（:func:`set_transport`）：测试里跑假传输，不发真网络请求；
    生产用标准库 ``urllib``（不引入新依赖）。
  · **只发摘要，不发全文**：载荷里的任务描述、工具参数都截断，且复用
    ``core/redact.py`` 打码密钥。webhook 的目标常常在**公网**，把完整提示词、
    完整工具输出推过去等于把内部数据送到第三方。
  · **只允许 http/https**：拒绝 ``file:`` / ``ftp:`` 之类协议 —— 否则一个
    配错的 URL 就能让平台去读本地文件（SSRF 的经典形态）。

配置（环境变量优先，其次 ``config.execution`` 上的同名字段，全都没有就用内置默认）：

  · ``AUTOMIND_WEBHOOKS``     目标列表。两种写法：
      - JSON 数组：``[{"url": "https://x/hook", "secret": "s1", "events": ["task_complete"]}]``
      - 简写：``https://x/hook|s1,https://y/hook2|s2``（``|secret`` 可选）
  · ``AUTOMIND_WEBHOOK_TIMEOUT``      单次请求超时秒数（默认 10）
  · ``AUTOMIND_WEBHOOK_RETRIES``      失败后的重试次数（默认 3，不含首次）
  · ``AUTOMIND_WEBHOOK_BACKOFF``      退避基数秒数（默认 1.0，退避为 base·2^n）
  · ``AUTOMIND_WEBHOOK_QUEUE_SIZE``   队列容量（默认 256）
  · ``AUTOMIND_WEBHOOK_BASE_URL``     生成审批回执地址用的外部可访问基址
  · ``AUTOMIND_WEBHOOKS_ENABLED=0``   显式全局关闭（保留配置但停发）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from automind.core.logging import get_logger

logger = get_logger("automind.core.webhooks")


# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

#: 载荷结构版本。接收方应据此做兼容判断 —— 字段只会增不会改语义，
#: 但"增字段"本身就可能让严格解析的接收方炸掉，所以必须有版本号可判。
SCHEMA_VERSION = "1"

#: 签名头名。接收方按这个名字取签名，再对**原始 body** 重算 HMAC 比对。
#:
#: ⚠️ 大小写：**头名一律按大小写不敏感处理**。HTTP 规定头名不区分大小写
#: （RFC 9110），而且 CPython 的 ``http.client`` 在发送时会把头名规范成
#: ``X-Automind-Signature`` 这一种写法 —— 接收方无论拿到哪种大小写都必须能取到。
#: 用框架提供的头集合取值（它们本来就大小写不敏感），不要用裸 dict 精确匹配。
SIGNATURE_HEADER = "X-AutoMind-Signature"
#: 事件类型头（与载荷里的 event 同值）—— 方便网关/规则引擎不解析 body 就分流。
EVENT_HEADER = "X-AutoMind-Event"
#: 本次投递的唯一 id，重试时**保持不变**（接收方据此幂等去重）。
DELIVERY_HEADER = "X-AutoMind-Delivery"
#: 载荷版本头，便于网关侧过滤
SCHEMA_HEADER = "X-AutoMind-Schema"

#: 文本字段截断上限 —— 载荷要小（对端可能按条计费/限流），也为了避免把
#: 完整任务描述与工具输出推到公网。
_MAX_TEXT = 500
_MAX_ARG_TEXT = 200
_MAX_ARGS = 20
_MAX_RESP_SNIPPET = 300

#: 允许的 URL 协议（白名单，而不是黑名单）
_ALLOWED_SCHEMES = ("http", "https")


class WebhookEvent(str, Enum):
    """出站事件类型。

    取值即载荷里的 ``event`` 字段，也是接收方做规则匹配的键。
    """

    TASK_START = "task_start"
    TASK_COMPLETE = "task_complete"
    TASK_ERROR = "task_error"
    TASK_CANCELLED = "task_cancelled"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_TIMEOUT = "approval_timeout"
    APPROVAL_RESOLVED = "approval_resolved"


def event_name(event: WebhookEvent | str) -> str:
    """把事件类型统一成字符串（枚举取 ``.value``，而不是 ``str(枚举)``）。

    差别不是洁癖：``str(WebhookEvent.TASK_START)`` 在 Python 3.11+ 得到的是
    ``"WebhookEvent.TASK_START"``（3.11 改了 ``str()`` 对 str-Enum 的行为），
    用它当事件名会让**日志和载荷里的值与接收方匹配的键对不上**。
    这个坑只在日志里看得见，所以统一收口到一个函数。
    """
    return event.value if isinstance(event, WebhookEvent) else str(event)


class InvalidWebhookURL(ValueError):
    """URL 不合法（协议不在白名单 / 缺主机名 / 无法解析）。

    显式抛错而不返回 False，是为了让"配置写错了"在自检时**立刻可见**，
    而不是表现为"事件莫名不发"。
    """


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════


@dataclass
class WebhookTarget:
    """一个投递目标。

    ``secret`` 为空表示不签名（对端自己用来源 IP / 内网隔离做鉴权）。
    """

    url: str
    secret: str = ""
    #: 该目标只关心这些事件类型；为空 = 全部订阅
    events: frozenset[str] = frozenset()
    name: str = ""

    def accepts(self, event: WebhookEvent | str) -> bool:
        """本目标是否订阅该事件（空集合视为订阅全部）。"""
        if not self.events:
            return True
        # 用统一的事件名转换：枚举取 value —— 否则 str(枚举) 在 3.11+ 会得到
        # "WebhookEvent.TASK_START"，跟用户配置里的 "task_start" 永远匹配不上
        return event_name(event) in self.events


@dataclass
class WebhookSettings:
    """投递配置快照。"""

    targets: list[WebhookTarget] = field(default_factory=list)
    timeout: float = 10.0
    max_retries: int = 3
    backoff: float = 1.0
    queue_size: int = 256
    base_url: str = ""
    #: 显式关闭开关（``AUTOMIND_WEBHOOKS_ENABLED=0``）
    explicitly_off: bool = False

    @property
    def enabled(self) -> bool:
        """是否真的有活要干：没目标 = 零成本。"""
        return bool(self.targets) and not self.explicitly_off


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or ""


def _env_float(name: str, default: float) -> float:
    """读浮点环境变量；值非法时回落到默认值（配置错不该让进程起不来）。"""
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("webhook_config_invalid", var=name, value=raw,
                       fallback=default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _cfg_attr(name: str, default: Any) -> Any:
    """从 ``config.execution`` 上取同名字段（没有就返回默认值）。

    约定：**不修改 core/config.py**，配置读取一律走 ``getattr`` 兜底 ——
    这样即使配置类还没有这个字段，模块照样能跑（字段将来加上即自动生效）。
    """
    try:
        from automind.core.config import ExecutionConfig

        ex = ExecutionConfig()
        value = getattr(ex, name, None)
        return default if value is None else value
    except Exception:                                     # pragma: no cover - 防御性
        return default


def validate_url(url: str) -> str:
    """校验并返回规范化后的 URL；不合法抛 :class:`InvalidWebhookURL`。

    只放行 http/https，且必须有主机名。这条不是形式主义：
    ``urllib`` 对 ``file://`` 是**真会去读本地文件**的，一个配错的
    （或被人塞进配置文件里的）URL 就变成了任意文件读取。
    """
    text = str(url or "").strip()
    if not text:
        raise InvalidWebhookURL("URL 为空")
    try:
        parts = urlsplit(text)
    except ValueError as e:
        raise InvalidWebhookURL(f"URL 无法解析：{text!r}（{e}）") from e
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidWebhookURL(
            f"协议 {scheme or '(无)'} 不被允许，只支持 http/https：{text!r}")
    if not parts.netloc:
        raise InvalidWebhookURL(f"URL 缺少主机名：{text!r}")
    return text


def _parse_targets_from_json(raw: str, out: list[WebhookTarget]) -> None:
    """解析 JSON 数组形式的 ``AUTOMIND_WEBHOOKS``。"""
    try:
        data = json.loads(raw)
    except ValueError as e:
        logger.warning("webhook_config_invalid", var="AUTOMIND_WEBHOOKS",
                       error=f"JSON 解析失败：{e}")
        return
    if isinstance(data, dict):
        # 容忍单对象写法（只有一条目标时用户很容易这么写）
        data = [data]
    if not isinstance(data, list):
        logger.warning("webhook_config_invalid", var="AUTOMIND_WEBHOOKS",
                       error="顶层必须是数组或对象")
        return
    for item in data:
        if isinstance(item, str):
            out.append(WebhookTarget(url=item.strip()))
            continue
        if not isinstance(item, Mapping):
            logger.warning("webhook_config_invalid", var="AUTOMIND_WEBHOOKS",
                           error=f"条目类型不支持：{type(item).__name__}")
            continue
        events = item.get("events") or []
        if isinstance(events, str):
            events = [e.strip() for e in events.split(",")]
        out.append(WebhookTarget(
            url=str(item.get("url") or "").strip(),
            secret=str(item.get("secret") or ""),
            events=frozenset(str(e).strip() for e in events if str(e).strip()),
            name=str(item.get("name") or ""),
        ))


def _parse_targets_from_compact(raw: str, out: list[WebhookTarget]) -> None:
    """解析简写形式：``url|secret,url2|secret2``。

    ``|`` 而不是 ``:`` 作为分隔符是刻意的 —— URL 自己带 ``:``（``https://``），
    用它做分隔符就必须处理转义，配起来反而更容易错。
    """
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            url, _, secret = chunk.partition("|")
            out.append(WebhookTarget(url=url.strip(), secret=secret.strip()))
        else:
            out.append(WebhookTarget(url=chunk))


def parse_targets(raw: str) -> list[WebhookTarget]:
    """解析目标列表；非法条目**丢弃并告警**，不牵连其余目标。"""
    text = (raw or "").strip()
    if not text:
        return []
    out: list[WebhookTarget] = []
    if text.startswith("[") or text.startswith("{"):
        _parse_targets_from_json(text, out)
    else:
        _parse_targets_from_compact(text, out)

    valid: list[WebhookTarget] = []
    for target in out:
        try:
            target.url = validate_url(target.url)
        except InvalidWebhookURL as e:
            # 一个目标写错不该让另外几个也失效 —— 丢掉坏的、留着好的并告警
            logger.warning("webhook_target_rejected", url=target.url, reason=str(e))
            continue
        valid.append(target)
    return valid


def load_settings() -> WebhookSettings:
    """读取配置（环境变量 > ``config.execution`` 同名字段 > 内置默认）。"""
    targets = parse_targets(_env("AUTOMIND_WEBHOOKS"))
    if not targets:
        # 配置类里的字段（若将来加上）作为环境变量之外的第二种来源
        cfg_targets = _cfg_attr("webhooks", None)
        if isinstance(cfg_targets, (list, tuple)):
            targets = parse_targets(json.dumps(list(cfg_targets), ensure_ascii=False))

    settings = WebhookSettings(
        targets=targets,
        timeout=float(_env_float("AUTOMIND_WEBHOOK_TIMEOUT",
                                 float(_cfg_attr("webhook_timeout_seconds", 10.0)))),
        max_retries=int(_env_int("AUTOMIND_WEBHOOK_RETRIES",
                                 int(_cfg_attr("webhook_max_retries", 3)))),
        backoff=float(_env_float("AUTOMIND_WEBHOOK_BACKOFF",
                                 float(_cfg_attr("webhook_backoff_seconds", 1.0)))),
        queue_size=int(_env_int("AUTOMIND_WEBHOOK_QUEUE_SIZE",
                                int(_cfg_attr("webhook_queue_size", 256)))),
        base_url=_env("AUTOMIND_WEBHOOK_BASE_URL",
                      str(_cfg_attr("webhook_base_url", "") or "")),
        explicitly_off=_env("AUTOMIND_WEBHOOKS_ENABLED", "1").strip().lower()
        in ("0", "false", "off", "no"),
    )
    # 负数没有意义，夹到 0 —— 否则退避 sleep 负数会变成"提前返回"这种怪行为
    settings.max_retries = max(0, settings.max_retries)
    settings.backoff = max(0.0, settings.backoff)
    settings.timeout = max(0.1, settings.timeout)
    settings.queue_size = max(1, settings.queue_size)
    return settings


# ═══════════════════════════════════════════════════════════════
# 签名
# ═══════════════════════════════════════════════════════════════


def sign_body(body: bytes, secret: str) -> str:
    """计算签名值（不含 ``sha256=`` 前缀）。空密钥返回空串（= 不签名）。"""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """接收方验签的参考实现 —— 文档与测试都以此为准。

    用 ``compare_digest`` 而不是 ``==``：字符串比较会短路，理论上可被
    计时侧信道逐字节猜出签名。
    """
    expected = sign_body(body, secret)
    if not expected:
        return False
    got = str(signature or "").strip()
    if got.lower().startswith("sha256="):
        got = got[len("sha256="):]
    return hmac.compare_digest(expected, got)


def _headers_for(target: WebhookTarget, body: bytes, event: str,
                 delivery_id: str) -> dict[str, str]:
    """构造请求头。签名覆盖的是**原始 body 字节**，不是反序列化后的对象。"""
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "AutoMind-Webhook/1.0",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
        SCHEMA_HEADER: SCHEMA_VERSION,
    }
    signature = sign_body(body, target.secret)
    if signature:
        # 带上算法前缀，将来换算法（sha512 等）时接收方无需猜测
        headers[SIGNATURE_HEADER] = f"sha256={signature}"
    return headers


# ═══════════════════════════════════════════════════════════════
# 传输层
# ═══════════════════════════════════════════════════════════════

#: 传输函数签名：``(url, body, headers, timeout) -> (status_code, response_text)``。
#: 抛异常表示"没拿到 HTTP 响应"（连接失败/超时），调用方按可重试处理。
Transport = Callable[[str, bytes, dict[str, str], float], "tuple[int, str] | Any"]


def _urllib_transport(url: str, body: bytes, headers: dict[str, str],
                      timeout: float) -> tuple[int, str]:
    """真实传输：标准库 urllib（不额外引入 httpx 依赖）。

    阻塞调用，由投递协程用 ``asyncio.to_thread`` 丢到线程里执行 ——
    否则一次 10 秒超时就把整个事件循环（连同正在跑的任务）卡住 10 秒。
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST")
    # 直接写入"未重定向头表"而不是 ``Request(headers=...)``：后者会先对头名做
    # ``.capitalize()``。实测结论（Windows + CPython 3.12）：**无论走哪条路，
    # http.client 在 ``putheader`` 时都会把头名转成 ``X-Automind-Event`` 这个
    # 大小写**（"HTTP 头名大小写不敏感"是 RFC 9110 的明确规定，所以这不是 bug，
    # 也无法在客户端侧可靠地固定住大小写）。
    # 由此得出两条硬约束，已写进 docs/WEBHOOKS.md 与测试：
    #   · 接收方**必须用大小写不敏感的方式**取头名（Python ``email.Message`` /
    #     FastAPI ``request.headers`` / Node ``req.headers`` 都是小写化后取值）；
    #   · 任何"照抄文档大小写、再用裸 dict 精确匹配"的写法都会踩坑。
    req.unredirected_hdrs.update(headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
            return int(getattr(resp, "status", 200) or 200), resp.read(
                _MAX_RESP_SNIPPET).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # HTTPError 也是"拿到了响应"（4xx/5xx），必须读出来，否则调用方
        # 无法区分"对端明确拒绝"(不该重试) 与"网络不通"(该重试)
        try:
            text = e.read(_MAX_RESP_SNIPPET).decode("utf-8", "replace")
        except Exception:
            text = ""
        return int(e.code or 0), text


_transport: Transport = _urllib_transport
_user_transport: Transport | None = None


def set_transport(fn: Transport | None = None) -> None:
    """注入自定义传输函数；传 ``None`` 恢复真实 HTTP 投递。

    测试用它把网络挡在外面（也用于把事件转投到消息队列等自定义通道）。
    """
    global _transport, _user_transport
    _user_transport = fn
    _transport = fn or _urllib_transport


def get_transport() -> Transport:
    return _transport


def is_custom_transport() -> bool:
    return _user_transport is not None


# ═══════════════════════════════════════════════════════════════
# 载荷
# ═══════════════════════════════════════════════════════════════


def _short(value: Any, limit: int = _MAX_TEXT) -> str:
    """转字符串并截断。截断处**标明总长**，接收方才知道自己拿到的不是全文。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + f"…[已截断，共 {len(text)} 字符]"
    return text


def _safe_jsonable(value: Any, depth: int = 0) -> Any:
    """把任意对象转成可 JSON 序列化的形式（失败则退化为字符串）。

    载荷必须能序列化 —— 一个不可序列化的参数会让整条事件发不出去，
    而"发不出去"的表现是"用户什么都没收到"，很难排障。
    """
    if depth > 4:
        return _short(value, 80)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, Mapping):
        return {str(k): _safe_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_jsonable(v, depth + 1) for v in value]
    return _short(value, 80)


def summarize_arguments(args: Any, limit: int = _MAX_ARG_TEXT,
                        max_keys: int = _MAX_ARGS) -> dict[str, str]:
    """把工具参数压成"给外部系统看的摘要"。

    为什么不是原样发送：webhook 目标常在公网或第三方 SaaS，工具参数里
    可能有本机路径、内网地址、查询串甚至密钥。这里做三件事 ——
    只保留前 ``max_keys`` 个键、每个值截断、复用 ``redact`` 打码密钥。
    """
    if not isinstance(args, Mapping):
        return {}
    try:
        from automind.core.redact import redact_secrets
    except Exception:                                     # pragma: no cover - 防御性
        def redact_secrets(text: str) -> str:             # type: ignore[misc]
            return text

    out: dict[str, str] = {}
    for i, (key, value) in enumerate(args.items()):
        if i >= max_keys:
            out["…"] = f"另有 {len(args) - max_keys} 个参数未展示"
            break
        text = value if isinstance(value, str) else _short(_safe_jsonable(value), limit)
        out[str(key)] = redact_secrets(_short(text, limit))
    return out


def _duration_ms(elapsed: Any) -> int | None:
    """归一化耗时字段：``elapsed_ms`` 优先，其次 ``elapsed_seconds``。"""
    if elapsed is None:
        return None
    try:
        value = float(elapsed)
    except (TypeError, ValueError):
        return None
    # 小于 1000 的裸数字按"秒"解释（``elapsed=3.2`` 显然指秒，
    # 而 ``elapsed_ms=3.2`` 会显式给字段名，两条路都不会误读）
    return int(round(value * 1000)) if value < 1000 else int(round(value))


_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt", "prompt_tokens", "input_tokens"),
    "completion": ("completion", "completion_tokens", "output_tokens"),
    "total": ("total", "total_tokens"),
}


def _tokens(raw: Any) -> dict[str, int] | None:
    """归一化 token 用量：容忍 ``{"total": n}`` / ``{"prompt": , "completion": }``
    以及对象形态（``.prompt_tokens`` 等）。给不出数字就返回 None（宁可不发）。

    对象态刻意**逐字段取值**而不是读 ``__dict__``：既支持 pydantic 模型
    （字段在实例里），也支持字段定义在类上的轻量对象；读 ``__dict__``
    会在后者上静默返回 None —— 表现是"载荷里永远没有 token 用量"，
    而这事儿没人会去核对，等于白做。
    """
    if raw is None:
        return None
    is_mapping = isinstance(raw, Mapping)

    def _get(key: str) -> Any:
        if is_mapping:
            return raw.get(key)
        try:
            return getattr(raw, key, None)
        except Exception:                                 # pragma: no cover - 防御性
            return None

    out: dict[str, int] = {}
    for canonical, keys in _TOKEN_ALIASES.items():
        for key in keys:
            value = _get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[canonical] = int(value)
                break
    # 一个字段都取不到就返回 None —— 不凭空造 ``{"total": 0}`` 这种假数据
    return out or None


def build_payload(
    event: WebhookEvent | str,
    *,
    session_id: str = "",
    task: str = "",
    tool: str = "",
    tier: str = "",
    reason: str = "",
    arguments: Any = None,
    approval_id: str = "",
    status: str = "",
    elapsed: Any = None,
    tokens: Any = None,
    error: str = "",
    timeout_s: Any = None,
    on_timeout: str = "",
    callback_url: str = "",
    delivery_id: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条事件载荷（纯函数，可独立单测）。

    结构固定为：``schema`` / ``event`` / ``event_id`` / ``delivery_id`` /
    ``timestamp`` + 任务字段（``session_id`` / ``task`` / ``status`` /
    ``elapsed_ms`` / ``tokens``）+ 审批字段（``approval`` 子对象）。
    接收方只依赖这几项即可；``extra`` 是给父模块做扩展用的逃生口，
    但**不要**把敏感字段塞进 ``extra`` —— 它不做截断以外任何处理。
    """
    event_value = event_name(event)
    now = time.time()
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "event": event_value,
        # 事件自身的 id 与投递 id 分开：一次事件可能因重试投递多次，
        # 接收方按 delivery_id 去重、按 event_id 归并
        "event_id": uuid.uuid4().hex,
        "delivery_id": delivery_id or uuid.uuid4().hex,
        # Unix 秒（浮点）与 ISO8601 各给一份：前者便于计算，后者便于人看日志
        "timestamp": round(now, 3),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + "Z",
        "session_id": str(session_id or ""),
        "task": _short(task),
    }
    if status:
        payload["status"] = str(status)
    if tool:
        payload["tool"] = str(tool)
    if tier:
        payload["tier"] = str(tier)
    duration = _duration_ms(elapsed)
    if duration is not None:
        payload["elapsed_ms"] = duration
    usage = _tokens(tokens)
    if usage:
        payload["tokens"] = usage
    if error:
        payload["error"] = _short(error)

    # 审批类事件：把"外部系统怎么回执"写在载荷里，而不是只写在文档里 ——
    # 收到告警的人（工单系统值班同学）手里只有这条载荷，他需要知道往哪回。
    if event_value.startswith("approval_") or approval_id:
        approval: dict[str, Any] = {
            "approval_id": str(approval_id or ""),
            "tool": str(tool or ""),
            "tier": str(tier or ""),
            "reason": _short(reason),
            "arguments_summary": summarize_arguments(arguments),
            "callback_url": str(callback_url or ""),
            "callback_method": "POST",
            "callback_body": {"approved": "bool", "comment": "str",
                              "arguments": "object|null"},
            "callback_auth": "Header: X-Admin-Token: <管理员令牌>",
            "callback_note": ("回执只对**未决**审批生效；审批已超时/已结束时，"
                              "端点会返回 approval_stale 明确拒绝，请勿重试。"),
        }
        if timeout_s is not None:
            try:
                approval["timeout_s"] = float(timeout_s)
            except (TypeError, ValueError):
                pass
        if on_timeout:
            approval["on_timeout"] = str(on_timeout)
        payload["approval"] = approval

    if extra:
        payload["extra"] = _safe_jsonable(dict(extra))
    return payload


def approval_callback_url(approval_id: str, base_url: str = "") -> str:
    """拼出给客户看的审批回执地址。

    ``base_url`` 为空时回落到 ``AUTOMIND_WEBHOOK_BASE_URL`` / 配置字段。
    为什么需要它：审批回执地址必须是**客户系统能访问到的**地址，而进程
    自己只知道 ``127.0.0.1:8000``（反代/网关之后完全没用）。所以基址必须
    由部署方显式给出，拿不到就返回空串 —— 返回一个错的地址比返回空更糟：
    客户会照着它去调，然后得到一个连不上的超时。
    """
    aid = str(approval_id or "").strip()
    if not aid:
        return ""
    base = str(base_url or "").strip()
    if not base:
        base = _env("AUTOMIND_WEBHOOK_BASE_URL",
                    str(_cfg_attr("webhook_base_url", "") or ""))
    base = base.rstrip("/")
    if not base:
        return ""
    try:
        validate_url(base)
    except InvalidWebhookURL as e:
        logger.warning("webhook_base_url_invalid", url=base, reason=str(e))
        return ""
    # 审批 id 会拼进路径，必须排除能改变路径语义的字符（``/`` ``..`` 等）
    safe = "".join(c for c in aid if c.isalnum() or c in "-_.:")
    return f"{base}/api/approvals/{safe}"


# ═══════════════════════════════════════════════════════════════
# 外部审批回执
# ═══════════════════════════════════════════════════════════════


def build_approval_receipt(approval_id: str, approved: bool, comment: str = "",
                           arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把外部系统的回执规范化成 :meth:`ApprovalOutcome.normalize` 能吃的形状。

    返回 ``{"approval_id", "approved", "arguments", "comment"}`` —— 前两项
    是执行链路的契约，``approval_id`` 是给端点做路由/回执用的（``normalize``
    会忽略未知键，多这一个键不影响它）。

    两条刻意的取舍（都是 fail-closed 方向）：

      · **拒绝时不带参数**：``arguments`` 只在批准时有效。否则"拒绝 + 一堆
        参数"在 ``normalize`` 里会得到 ``modified=True``，而某些调用点只看
        ``modified`` 就会以为"用户是改完参数批准的" —— 那是把拒绝读成了批准。
      · **空字典等于没给**：``normalize`` 对空 ``arguments`` 的处理是置
        ``None``（不视为修改），这里保持一致，不制造"两头解释不同"的中间态。

    无论外部系统怎么传（表单的 ``"true"`` 字符串、``1``/``0``、缺字段），
    结果都保证能被 ``ApprovalOutcome.normalize`` 正确解释。
    """
    normalized_approved = _coerce_bool(approved)
    args = dict(arguments) if isinstance(arguments, Mapping) and arguments else None
    if not normalized_approved:
        args = None
    return {
        "approval_id": str(approval_id or ""),
        "approved": normalized_approved,
        "arguments": args,
        "comment": str(comment or ""),
    }


def _coerce_bool(value: Any) -> bool:
    """把外部系统五花八门的"真"归一化成 bool。

    HTML 表单、URL 查询串、某些 SDK 都会把布尔值变成字符串：``"false"``
    在 Python 里是**真值**，直接 ``bool()`` 会把一个"拒绝"读成"批准"。
    这是审批链路上最危险的一类误判，所以在这里一次性收口。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "approve",
                                        "approved", "同意", "批准", "是", "真")
    return bool(value)


def approval_receipt_state(approval_id: str, pending: Mapping[str, Any] | None = None,
                           *, known: bool | None = None) -> dict[str, Any]:
    """判定一次外部回执是否还能生效 —— 端点在 set_result **之前**先问它。

    返回 ``{"resolvable", "approval_id", "reason", "status"}``；``status``
    取值 ``"pending"`` / ``"stale"``：

      · ``pending``   —— 可以受理，端点应把 ``build_approval_receipt(...)``
        的结果交给 ``ApprovalOutcome.normalize()`` 再 ``set_result``；
      · ``stale``     —— 明确拒绝（HTTP 409），响应体里带 ``approval_stale``
        （与 ``server.py`` 里 WebSocket 分支发的 ``approval_stale`` 消息同语义）。

    为什么必须"明确拒绝"而不是静默丢弃：工单系统里点了「批准」的人，
    如果什么反馈都没有，会合理地认为"批过了"；而真实情况是这次审批早就
    超时并按拒绝处理、任务已经失败了。静默丢弃等于制造一个**没人知道的
    错误结论** —— 这类问题在审计时是灾难。

    ``pending`` 建议传 ``server._pending_approvals``（或它的快照）。
    为避免与 ``server.py`` 循环导入，这里**不做任何导入**：只按映射查键；
    父模块也可以直接传 ``known=True/False``（自己已经查过了）。
    """
    aid = str(approval_id or "").strip()
    if known is None:
        known = bool(aid) and isinstance(pending, Mapping) and aid in pending
    if not aid:
        return {"resolvable": False, "approval_id": aid, "status": "stale",
                "reason": "缺少 approval_id"}
    if not known:
        return {
            "resolvable": False, "approval_id": aid, "status": "stale",
            "reason": ("approval_stale：该审批已结束（超时、已处理或任务已中断），"
                       "本次回执不再生效。"),
        }
    return {"resolvable": True, "approval_id": aid, "status": "pending",
            "reason": ""}


def admin_token_ok(provided: str, expected: str) -> bool:
    """外部回执的管理员令牌校验（端点直接调用即可）。

    与 ``server.py`` 的令牌校验同语义：**没配令牌就不放行**（fail-closed）。
    外部回执能批准任意高风险工具调用，这个端点绝不能因为"管理员忘了配
    令牌"而变成公网上的开放审批后门。
    """
    if not expected:
        return False
    return hmac.compare_digest(str(provided or ""), str(expected))


# ═══════════════════════════════════════════════════════════════
# 投递器
# ═══════════════════════════════════════════════════════════════

#: 投递结果记账（供 ``/metrics`` 与排障使用）
_COUNTERS_of = (
    "queued",        # 入队的事件数
    "delivered",     # 至少一次成功投递的事件数
    "failed",        # 重试耗尽后彻底失败的事件数
    "dropped",       # 队列满被丢弃的事件数（**必须能被看见**）
    "retried",       # 重试次数（累计）
    "skipped",       # 无目标/被显式关闭时被跳过的事件数
    # 被目标事件过滤规则排除的投递数。注意：**投递前的过滤压根不入队**
    # （见 target_for —— 不订阅就不产生任何工作），所以正常情况它恒为 0；
    # 它非零只说明"事件入队后目标订阅关系变了"这类竞态，属于异常信号。
    "rejected",
)


class WebhookDispatcher:
    """有界队列 + 后台投递协程的事件投递器。

    线程/事件循环模型：``emit`` 是**同步**方法（可以被任何线程/协程调用，
    也可以在 Agent 的同步回调里直接调用），只做入队并唤醒后台协程；真正的
    网络投递发生在投递协程里。入队用 ``threading.Lock`` 保护 —— 服务端的
    事件可能来自事件循环，CLI/GUI 的事件可能来自别的线程，两种都要能用。
    """

    def __init__(self, settings: WebhookSettings | None = None,
                 transport: Transport | None = None) -> None:
        self.settings = settings or load_settings()
        self._transport = transport
        self._queue: deque[tuple[WebhookTarget, WebhookEvent | str, dict[str, Any]]] = deque()
        self._lock = threading.Lock()
        #: 已出队但尚未投递结束的事件数（flush 用它判断"真的发完了"）
        self._pending = 0
        #: 唤醒后台协程的信号（threading.Event 可以跨线程 set，供 close 等场景）
        self._wakeup = threading.Event()
        #: **事件循环侧**的唤醒信号。与上面那个分开是必需的：后台协程必须在
        #: 循环里"让出控制权"地等待，而不是把线程占在 ``threading.Event.wait``
        #: 上 —— 后者不仅费一个线程，还让等待过程对测试不可见（测试替换
        #: ``asyncio.sleep`` 对它无效，表现为"入队成功、投递永不发生"）。
        self._wake_ev: asyncio.Event | None = None
        self._wake_loop: asyncio.AbstractEventLoop | None = None
        self._worker: asyncio.Task[None] | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._counters: dict[str, int] = {k: 0 for k in _COUNTERS_of}
        #: 最近一次投递失败的说明（排障用：``/metrics`` 里带上它比只有计数有用）
        self.last_error: str = ""
        #: 最近一次成功投递的时间戳（0 = 从未成功）
        self.last_success_at: float = 0.0

    # ── 记账 ────────────────────────────────────────────────

    def counters(self) -> dict[str, int]:
        """返回计数快照（副本，调用方随便改）。"""
        with self._lock:
            return dict(self._counters)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and not self._closed

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def stats(self) -> dict[str, Any]:
        """投递统计 —— 直接喂给 ``/metrics`` 或健康检查接口。"""
        with self._lock:
            counters = dict(self._counters)
            depth = len(self._queue)
            pending = self._pending
        return {
            "enabled": self.settings.enabled,
            "closed": self._closed,
            "targets": len(self.settings.targets),
            "queue_depth": depth,
            "queue_size": self.settings.queue_size,
            "pending": pending,
            "timeout_s": self.settings.timeout,
            "max_retries": self.settings.max_retries,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            **counters,
        }

    # ── 入队 ────────────────────────────────────────────────

    def target_for(self, event: WebhookEvent | str) -> list[WebhookTarget]:
        """订阅了该事件的目标列表（入队前的唯一筛选依据）。"""
        name = event_name(event)
        return [t for t in self.settings.targets if t.accepts(name)]

    def emit(self, event: WebhookEvent | str, payload: dict[str, Any] | None = None,
             *, targets: Iterable[WebhookTarget] | None = None) -> int:
        """投递一条事件，返回入队份数。

        **保证不抛异常、不阻塞**：这是整条链路唯一被主任务调用的方法，
        它要是抛了，一个"通知失败"就会把任务本身弄挂 —— 那是最不能接受的
        失败模式（用户宁可收不到通知，也不能因此丢任务）。
        """
        try:
            if self._closed:
                return 0
            chosen = list(targets) if targets is not None else self.target_for(event)
            if not self.settings.enabled or not chosen:
                self._bump("skipped")
                return 0
            body = dict(payload or {})
            queued = 0
            for target in chosen:
                # 队列满时**丢弃并计数**（不静默、不无限增长）
                with self._lock:
                    if len(self._queue) >= self.settings.queue_size:
                        self._counters["dropped"] += 1
                        drop = True
                    else:
                        self._queue.append((target, event, body))
                        self._counters["queued"] += 1
                        drop = False
                if drop:
                    # 注意：结构化日志的键**不能叫 event** —— structlog 与
                    # core/logging.py 的 stdlib 适配器都把 `event` 用作消息名
                    # 这一位置参数，用 `event=` 传字段会直接 TypeError，
                    # 结果是"本该记的告警自己把投递循环弄挂了"。
                    logger.warning(
                        "webhook_queue_full",
                        evt=event_name(event), url=target.url,
                        queue_size=self.settings.queue_size,
                        hint="事件已丢弃，不会补发；请检查目标可用性或调大队列")
                    continue
                queued += 1
            if queued:
                self._ensure_worker()
                self._signal()
            return queued
        except Exception as e:                            # noqa: BLE001 - 见 docstring
            logger.warning("webhook_emit_failed", evt=event_name(event), error=str(e))
            return 0

    # ── 后台投递 ────────────────────────────────────────────

    def _signal(self) -> None:
        """唤醒后台投递协程（没有循环时静默跳过）。"""
        self._wakeup.set()
        event = self._wake_ev
        if event is not None:
            try:
                event.set()
            except Exception:                             # pragma: no cover - 防御性
                pass

    def _ensure_worker(self) -> None:
        """确保投递协程在**当前**事件循环里活着。

        为什么要处理"换循环"：CLI、``asyncio.run`` 的测试、服务端各自是
        不同的循环。旧循环里的 task 在新循环中永远不会被调度，如果只判断
        ``not done()`` 就会一直以为"投递协程还活着"，事件全部积压。
        这里比对循环身份，不同就重建（新循环要有自己的唤醒事件）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有事件循环（纯同步调用，比如 CLI 收尾时投递）：只入队。
            # 下一次在有循环的上下文里 emit 时会启动投递。
            return
        if self._worker is not None and not self._worker.done() \
                and self._worker_loop is loop:
            return
        # 旧循环里还挂着一个投递协程（换循环的场景）：必须显式取消再重建。
        # 不取消的话，两个协程会同时从同一个队列取件 —— 队列本身有锁不会
        # 丢事件，但"两个投递协程"会让并发度/计数看起来莫名其妙，
        # 而且在测试里会留下"任务在别的循环上挂着"的告警。
        stale = self._worker
        if stale is not None and not stale.done() and self._worker_loop is not None \
                and self._worker_loop is not loop:
            try:
                self._worker_loop.call_soon_threadsafe(stale.cancel)
            except Exception:                             # pragma: no cover - 旧循环已关闭
                pass
        self._worker_loop = loop
        if self._wake_loop is not loop:
            self._wake_ev = asyncio.Event()
            self._wake_loop = loop
        self._worker = loop.create_task(self._run(), name="automind-webhook-delivery")

    async def _idle(self, timeout: float) -> None:
        """在事件循环里等待"有新事件"或超时。

        等待对象是 ``asyncio.Event``（循环侧），所以这是一个**真正让出控制权**
        的 await：后台协程不会占线程，测试替换 ``asyncio.sleep`` 也不会让它
        变成忙等。超时上限 0.25s 是兜底：即便某次唤醒信号因为跨线程竞态丢了，
        事件也最多迟到 0.25 秒，而不是永远卡在队列里。
        """
        event = self._wake_ev
        if event is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        # 3.11+ 起 asyncio.TimeoutError 就是内置 TimeoutError（UP041）
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            # 清掉已消费的信号；如果此刻恰好有新事件入队，它最多等一个
            # 超时周期就会被处理（不会丢）
            event.clear()

    async def _run(self) -> None:
        """投递主循环：等待信号 → 排空队列 → 并发投递。

        为什么不用 ``await asyncio.to_thread(self._wakeup.wait)`` 这种写法：
        ``threading.Event.wait`` 一旦进入就把一个线程占住，而且**测不到** ——
        等待发生在工作线程里，测试对 ``asyncio.sleep`` 的任何替换都管不着它，
        表现就是"入队成功、投递永不发生"。现在的等待走 :meth:`_idle`。
        """
        while True:
            if self._closed and not self._queue:
                return
            if not self._queue:
                await self._idle(0.25)
                continue
            batch: list[tuple[WebhookTarget, str, dict[str, Any]]] = []
            with self._lock:
                while self._queue:
                    target, event, payload = self._queue.popleft()
                    batch.append((target, event_name(event), payload))
                    self._pending += 1
            if not batch:
                continue
            try:
                # 并发投递：多个慢目标不该互相排队（一个挂掉的 endpoint
                # 会让另一个目标的告警迟到几分钟）
                await asyncio.gather(*(self._deliver_one(t, e, p)
                                       for t, e, p in batch))
            except Exception as e:                        # pragma: no cover - 防御性
                logger.warning("webhook_batch_failed", error=str(e))
            finally:
                with self._lock:
                    self._pending -= len(batch)

    async def _deliver_one(self, target: WebhookTarget, event: str,
                           payload: dict[str, Any]) -> bool:
        """投递一条事件到单个目标；返回是否成功。**永不抛异常到上层。**"""
        if not target.accepts(event):
            self._bump("rejected")
            return False
        delivery_id = str(payload.get("delivery_id") or uuid.uuid4().hex)
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as e:
            # 连序列化都失败说明载荷里混进了坏值；这属于"事件本身有问题"，
            # 重试没有意义，直接计失败并记下原因
            self._bump("failed")
            self.last_error = f"载荷序列化失败：{type(e).__name__}: {e}"
            logger.warning("webhook_payload_encode_failed", evt=event,
                           error=str(e))
            return False

        headers = _headers_for(target, body, event, delivery_id)
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            try:
                result = self._transport(target.url, body, headers,
                                         self.settings.timeout) \
                    if self._transport else _transport(target.url, body, headers,
                                                       self.settings.timeout)
                if asyncio.iscoroutine(result):
                    result = await result
                status, detail = self._unpack(result)
            except Exception as e:
                status, detail = 0, f"{type(e).__name__}: {e}"

            if 200 <= status < 300:
                self._bump("delivered")
                self.last_success_at = time.time()
                self.last_error = ""
                return True

            retryable = status == 0 or status >= 500 or status in (408, 429)
            last = attempt == attempts - 1
            if not retryable or last:
                self._bump("failed")
                self.last_error = f"{event} → {target.url}：HTTP {status} {detail}"
                logger.warning("webhook_delivery_failed", evt=event, url=target.url,
                               status=status, attempts=attempt + 1,
                               delivery=delivery_id, detail=_short(detail, 200),
                               permanent=not retryable)
                return False

            self._bump("retried")
            # 指数退避：base·2^attempt。第一次重试前就等 base，是为了给
            # "对端刚重启完"这类瞬时故障一个自愈窗口。
            delay = self.settings.backoff * (2 ** attempt)
            if delay > 0:
                await asyncio.sleep(delay)
        return False

    @staticmethod
    def _unpack(result: Any) -> tuple[int, str]:
        """兼容多种传输返回值形态：``(status, text)`` / 裸状态码 / 对象。"""
        if isinstance(result, tuple) and result:
            status = result[0]
            detail = result[1] if len(result) > 1 else ""
            try:
                return int(status), str(detail or "")
            except (TypeError, ValueError):
                return 0, str(status)
        if isinstance(result, int):
            return result, ""
        status = getattr(result, "status_code", None)
        if status is not None:
            detail = str(getattr(result, "text", "") or "")
            return int(status), detail
        return 0, f"无法识别的传输返回值：{type(result).__name__}"

    # ── 收尾 ────────────────────────────────────────────────

    async def flush(self, timeout: float = 5.0) -> bool:
        """等待队列排空且所有在途投递结束；返回是否在超时前完成。

        服务进程退出、以及所有测试都用它 —— 没有 flush 的话，"事件到没到"
        只能靠 sleep 猜，测试必然是 flaky 的。

        等待方式刻意用 ``asyncio.wait_for``（而不是裸 ``asyncio.sleep``）：
        它是一个**有真实超时的 await**，既能可靠地让出控制权给后台投递协程，
        又不会因为"睡眠被替换成空实现"（测试里常见的做法）而变成忙等 ——
        忙等的后果是后台协程一次都排不上队，flush 永远等不到结果。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                idle = not self._queue and self._pending == 0
            if idle:
                return True
            if time.monotonic() >= deadline:
                with self._lock:
                    return not self._queue and self._pending == 0
            self._signal()
            try:
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.02)
            except TimeoutError:                          # 3.11+ 内置即 asyncio 的那个
                pass

    async def aclose(self, timeout: float = 5.0) -> None:
        """排空后停掉后台投递协程（进程退出/测试清理时调用）。"""
        await self.flush(timeout=timeout)
        self._closed = True
        self._signal()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:                             # pragma: no cover - 收尾不抛
                pass
        self._worker = None

    def close(self) -> None:
        """同步关闭：不再接受新事件（协程由 aclose 负责收尾）。"""
        self._closed = True
        self._signal()

    def reset_counters(self) -> None:
        """清零计数（测试与"排障后重新观察"用）。"""
        with self._lock:
            for key in _COUNTERS_of:
                self._counters[key] = 0


# ═══════════════════════════════════════════════════════════════
# 进程级默认投递器
# ═══════════════════════════════════════════════════════════════

#: 懒构造：导入期读配置会让"导入 automind.core.webhooks"产生副作用，
#: 而配置来源（环境变量/配置文件）在不同入口下可能还没准备好。
_default: WebhookDispatcher | None = None


def get_dispatcher() -> WebhookDispatcher:
    """返回进程级默认投递器（首次调用时按配置构造）。"""
    global _default
    if _default is None:
        _default = WebhookDispatcher()
    return _default


def reset_for_tests(settings: WebhookSettings | None = None,
                    transport: Transport | None = None) -> WebhookDispatcher:
    """重建进程级投递器（测试用：钉死配置与传输，避免真发网络请求）。"""
    global _default
    _default = WebhookDispatcher(settings=settings, transport=transport)
    return _default


def emit(event: WebhookEvent | str, payload: dict[str, Any] | None = None) -> int:
    """进程级便捷入口 —— 父模块的事件挂载点只需要调它。

    典型用法（父模块在事件产生处调用，**不需要** await）::

        from automind.core import webhooks
        webhooks.emit(webhooks.WebhookEvent.TASK_COMPLETE, webhooks.build_payload(
            webhooks.WebhookEvent.TASK_COMPLETE, session_id=sid, task=task,
            status="ok", elapsed=seconds, tokens=usage))
    """
    dispatcher = get_dispatcher()
    if dispatcher is None:                                # pragma: no cover - 防御性
        return 0
    return dispatcher.emit(event, payload)


async def flush(timeout: float = 5.0) -> bool:
    """等待进程级默认投递器的队列排空（服务退出前调用）。"""
    if _default is None:
        return True
    return await _default.flush(timeout=timeout)


def stats() -> dict[str, Any]:
    """进程级投递统计 —— 供 ``/metrics`` / ``/api/health`` 直接内联。"""
    if _default is None:
        # 还没用过：给一份"未配置"的形状，端点不必处理 None
        return {
            "enabled": False, "closed": False, "targets": 0, "queue_depth": 0,
            "pending": 0, "delivered": 0, "failed": 0, "dropped": 0,
            "retried": 0, "queued": 0, "skipped": 0, "rejected": 0,
            "last_error": "", "last_success_at": 0.0,
        }
    return _default.stats()


__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "SCHEMA_HEADER",
    "SCHEMA_VERSION",
    "SIGNATURE_HEADER",
    "InvalidWebhookURL",
    "Transport",
    "WebhookDispatcher",
    "WebhookEvent",
    "WebhookSettings",
    "WebhookTarget",
    "admin_token_ok",
    "approval_callback_url",
    "approval_receipt_state",
    "build_approval_receipt",
    "build_payload",
    "emit",
    "event_name",
    "flush",
    "get_dispatcher",
    "get_transport",
    "is_custom_transport",
    "load_settings",
    "parse_targets",
    "reset_for_tests",
    "set_transport",
    "sign_body",
    "stats",
    "summarize_arguments",
    "validate_url",
    "verify_signature",
]
