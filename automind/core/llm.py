"""统一 LLM 后端 — 支持 10+ 模型提供商，单一异步接口。

支持的提供商:
    - openai (OpenAI, 及兼容 API 如 DeepSeek/Kimi/百炼/智谱/豆包)
    - anthropic (Claude)
    - google (Gemini)
    - grok (Grok)
    - ollama (本地模型)

使用示例::

    backend = LLMBackendFactory.create("openai", config)
    response = await backend.generate([{"role": "user", "content": "Hello"}])
"""

from __future__ import annotations

import contextlib
import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from automind.core.config import LLMProviderConfig
from automind.core.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextTooLargeError,
    LLMError,
    LLMProviderNotFoundError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from automind.core.logging import get_logger
from automind.core.types import LLMResponse, ToolCall

logger = get_logger("automind.llm")

# ═══════════════════════════════════════════════════════════════
# 瞬时错误分类（v1.7.6）
# ═══════════════════════════════════════════════════════════════

#: 异常类名（沿 MRO 匹配）→ 类别。按类名而不是 isinstance 判断，是为了不在
#: 这里硬依赖 openai / anthropic / httpx 任何一个 SDK（它们都是可选依赖）。
_EXC_NAME_KIND: dict[str, str] = {
    "APITimeoutError": "timeout",           # openai / anthropic（先于其父类）
    "ReadTimeout": "timeout", "WriteTimeout": "timeout",
    "PoolTimeout": "timeout", "TimeoutException": "timeout",
    "ConnectTimeout": "connection",         # 连不上 ≠ 模型慢，按连接问题处理
    "APIConnectionError": "connection",     # str() 恰为 "Connection error."
    "ConnectError": "connection", "ReadError": "connection",
    "WriteError": "connection", "RemoteProtocolError": "connection",
    "NetworkError": "connection", "ProxyError": "connection",
    "ConnectionError": "connection",        # 内置 ConnectionResetError 等的基类
    "InternalServerError": "server",        # openai/anthropic 的 5xx
    "ServiceUnavailableError": "server", "OverloadedError": "server",
    "RateLimitError": "rate_limit",
}

#: 兜底关键字（小写）。只在拿不到类型/状态码时才用 —— 中转层常把真实错误
#: 包成一段文本再抛回来。
_MSG_KIND: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rate_limit", ("429", "rate limit", "rate_limit", "too many requests")),
    ("server", ("502", "503", "504", "529", "bad gateway", "service unavailable",
                "gateway timeout", "overloaded", "server error", "upstream")),
    ("timeout", ("timed out", "timeout")),
    ("connection", ("connection error", "connection reset", "connection refused",
                    "connection aborted", "server disconnected", "peer closed",
                    "remote end closed", "remotedisconnected", "broken pipe",
                    "eof occurred", "unexpected eof", "network is unreachable",
                    "name resolution", "getaddrinfo", "ssl", "proxy",
                    "incomplete chunked read", "remote")),
    ("parse", ("unterminated", "expecting value", "incomplete", "json")),
)

#: 可以"等一等再试"的类别
TRANSIENT_KINDS = frozenset({"connection", "server", "rate_limit", "timeout", "parse"})

#: 我们自己抛出的 LLMError 上的 code → 类别（任务层重连据此判断）
_CODE_KIND = {
    "CONNECTION": "connection", "SERVER_ERROR": "server",
    "RATE_LIMIT": "rate_limit", "TIMEOUT": "timeout",
    "RETRY_EXHAUSTED": "connection",
}


def classify_llm_error(exc: BaseException) -> str:
    """把一次 LLM 调用失败归类：connection / server / rate_limit / timeout /
    parse / fatal。

    v1.7.6 起取代散落在各 provider 里的"字符串里有没有某个词"判断 —— 那套
    判断漏掉了最常见的一种：openai SDK 的 ``APIConnectionError`` 文本就是
    ``"Connection error."``，不含 "reset" / "peer closed" 中任何一个，于是被
    当成不可重试错误直接抛出。表现就是用户报告的"任务跑一段时间就提示
    Connection error."：中转偶发断开一次，整轮长任务就此作废。
    """
    if isinstance(exc, (LLMAuthenticationError, LLMContextTooLargeError,
                        LLMProviderNotFoundError)):
        return "fatal"
    if isinstance(exc, LLMConnectionError):
        return "connection"
    if isinstance(exc, LLMRateLimitError):
        return "rate_limit"
    if isinstance(exc, LLMTimeoutError):
        return "timeout"
    if isinstance(exc, LLMError):
        return _CODE_KIND.get(getattr(exc, "code", ""), "fatal")
    if isinstance(exc, json.JSONDecodeError):
        return "parse"

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return "rate_limit"
        if status in (408, 409, 425) or status >= 500:
            return "server"
        if 400 <= status < 500:
            return "fatal"      # 401/403/400/404/422：重试也不会好

    for klass in type(exc).__mro__:
        kind = _EXC_NAME_KIND.get(klass.__name__)
        if kind:
            return kind
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "connection"

    low = str(exc).lower()
    if any(k in low for k in ("401", "403", "unauthorized", "invalid api key")):
        return "fatal"
    if "context" in low and ("too long" in low or "exceed" in low):
        return "fatal"
    for kind, words in _MSG_KIND:
        if any(w in low for w in words):
            return kind
    return "fatal"


def is_transient_llm_error(exc: BaseException) -> bool:
    """这次失败是否值得"等一等再试"。"""
    return classify_llm_error(exc) in TRANSIENT_KINDS


def connection_error_message(exc: BaseException, api_base: str = "",
                             attempts: int = 0) -> str:
    """给人看的连接失败说明：说清楚连的是哪、试了几次、下一步查什么。"""
    where = f"（{api_base}）" if api_base else ""
    tried = f"已自动重试 {attempts} 次仍失败" if attempts else "连接失败"
    return (f"无法连接模型服务{where}：{tried}。原始错误：{str(exc)[:160] or type(exc).__name__}。"
            "请检查：① 代理/中转地址是否可达；② 系统代理或 VPN 是否刚断开；"
            "③ 中转服务是否限制了长连接。已完成的步骤不会丢失，可点击「接着做」继续。")


# ═══════════════════════════════════════════════════════════════
# 提示缓存用量归一化（v1.11.10）
# ═══════════════════════════════════════════════════════════════

def _usage_field(usage: Any, key: str, default: Any = None) -> Any:
    """从 usage 里取一个字段：同时兼容 dict 与各家 SDK 的 pydantic 模型。

    openai SDK 的 ``Usage`` 是 ``extra="allow"`` 的模型 —— DeepSeek 的
    ``prompt_cache_hit_tokens`` 之类的扩展字段在 ``model_extra`` 里，
    ``getattr`` 能取到；中转也可能直接回 dict，所以两条路都要走。
    """
    if usage is None:
        return default
    if isinstance(usage, dict):
        return usage.get(key, default)
    value = getattr(usage, key, None)
    if value is None:
        extra = getattr(usage, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(key)
    return default if value is None else value


def extract_cache_usage(usage: Any) -> tuple[int, int, int, bool]:
    """把各家的 usage 归一化成 ``(命中 token, 写入缓存 token, 总输入 token, 是否上报)``。

    口径差异（这是"命中率"能不能算对的关键）：

    * **DeepSeek**：``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``，
      ``prompt_tokens = hit + miss``（总输入）。
    * **OpenAI / GLM（智谱）/ Kimi / 通义 / 豆包**：命中数在
      ``prompt_tokens_details.cached_tokens``（是 ``prompt_tokens`` 的子集）；
      Kimi 等也有把 ``cached_tokens`` 放在顶层的，一并兜住。
    * **Anthropic**：``cache_read_input_tokens``（命中）/
      ``cache_creation_input_tokens``（写入）；``input_tokens`` **不含**缓存读取，
      所以总输入 = input + read + creation。
    * **Google Gemini**：``cached_content_token_count``（子集），总输入为
      ``prompt_token_count``。
    * 其余（Ollama / 中转不返回）：全 0、``reported=False`` —— 上层据此显示
      "—"而不是伪造一个 0%。

    ``reported`` 区分"提供商明确说了这次没命中（0）"与"提供商压根不报缓存"：
    只有前者能显示 0%，后者显示"—"，否则 Ollama 用户会永远看到一个红色的 0%。
    """
    hit = _usage_field(usage, "prompt_cache_hit_tokens")
    miss = _usage_field(usage, "prompt_cache_miss_tokens")
    read = _usage_field(usage, "cache_read_input_tokens")
    creation = _usage_field(usage, "cache_creation_input_tokens")
    details = _usage_field(usage, "prompt_tokens_details")
    top_cached = _usage_field(usage, "cached_tokens")
    google_cached = _usage_field(usage, "cached_content_token_count")
    prompt = _usage_field(usage, "prompt_tokens")
    if prompt is None:
        prompt = _usage_field(usage, "input_tokens")          # ⚠️ 见下方 Anthropic 分支
    if prompt is None:
        prompt = _usage_field(usage, "prompt_token_count")    # Google

    reported = any(v is not None for v in (
        hit, miss, read, creation, details, top_cached, google_cached))

    cached = 0
    for value in (hit, read,
                  _usage_field(details, "cached_tokens"),
                  top_cached, google_cached):
        try:
            cached = max(cached, int(value or 0))
        except (TypeError, ValueError):
            continue

    try:
        creation_n = int(creation or 0)
    except (TypeError, ValueError):
        creation_n = 0
    try:
        prompt_n = int(prompt or 0)
    except (TypeError, ValueError):
        prompt_n = 0

    # Anthropic：input 只算未命中部分，真实总输入要补上 read + creation。
    # 判据是"出现了 Anthropic 专有字段"，而不是猜提供商名字 —— 中转代理
    # 也可能把 Anthropic 协议转成别的形态。
    if read is not None or creation is not None:
        try:
            read_n = int(read or 0)
        except (TypeError, ValueError):
            read_n = 0
        input_total = prompt_n + read_n + creation_n
        return cached, creation_n, input_total, reported

    # DeepSeek：总输入优先用 hit+miss 自校验（个别中转的 prompt_tokens 口径会漂）
    try:
        miss_n = int(miss or 0)
    except (TypeError, ValueError):
        miss_n = 0
    if cached and miss_n:
        input_total = cached + miss_n
    else:
        input_total = prompt_n
    return cached, creation_n, input_total, reported



# ═══════════════════════════════════════════════════════════════
# 共享 TLS 上下文
# ═══════════════════════════════════════════════════════════════

_SSL_CONTEXT: Any = None


def shared_ssl_context() -> Any:
    """全进程复用同一个 ``ssl.SSLContext``（首次构建后缓存）。

    新建 LLM 客户端此前实测要 ~1.15 秒，profile 显示 **97% 花在
    ``SSLContext.load_verify_locations``** —— 解析整个 CA 证书包单次约 380ms，
    而 httpx 每个客户端要建 3 个（直连 + 两个代理传输）。这直接决定了
    "切一次模型卡两三秒"：真正的开销不在 Agent 重建，在这里。

    CA 包在进程生命周期内不会变，复用同一个上下文完全安全；``verify`` 传入
    一个现成的 SSLContext 时 httpx 会原样使用，不再重新加载。

    构建失败（缺 certifi、企业环境证书异常等）时返回 None，调用方退回
    httpx 的默认行为 —— 慢，但不能因为一个优化把连接能力弄没了。
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import httpx
            _SSL_CONTEXT = httpx.create_ssl_context()
        except Exception as e:
            logger.warning("ssl_context_build_failed", error=str(e))
            _SSL_CONTEXT = False        # 用 False 记住"试过且失败"，别每次重试
    return _SSL_CONTEXT or None


def request_timeout(total: float) -> Any:
    """SDK 请求超时：总时长沿用配置，**建连单独限 15 秒**（v1.7.6）。

    此前各阶段共用一个 120 秒：代理/中转挂掉（SYN 被丢弃）时每次尝试都要在
    建连阶段干等 120 秒，而单轮调用的硬上限是 300 秒 —— 重试两次就撞上限，
    退避重试形同虚设，用户看到的是"卡了五分钟然后失败"。建连本应在几秒内
    完成，15 秒足够覆盖慢网络，又能让"连不上"尽快进入重试。
    """
    try:
        import httpx
        return httpx.Timeout(total, connect=min(15.0, total))
    except Exception:                    # pragma: no cover - httpx 缺失时退回浮点
        return total


def shared_http_client(timeout: float, base_url: str = "") -> Any:
    """构造一个复用共享 TLS 上下文的 httpx.AsyncClient（失败时返回 None）。"""
    try:
        import httpx
        ctx = shared_ssl_context()
        if ctx is None:
            return None
        kwargs: dict[str, Any] = {
            "verify": ctx,
            "timeout": request_timeout(timeout),
            # 对齐 openai / anthropic SDK 自带客户端的默认值，避免"换了个
            # http_client 之后中转代理的 3xx 跳转不再跟随"这类隐性行为差异
            "follow_redirects": True,
            # 连接池上限收紧 + 显式 keep-alive 过期：此前 100 个空闲连接长期
            # 保留，服务端（如 DeepSeek）主动断开空闲连接后，httpx 复用坏连接
            # 会 read timeout —— 表现为"测试连接/对话间歇性卡死，重启才恢复"。
            # 少量空闲连接 + 30s 过期，既保留连接复用带来的延迟收益，又让坏
            # 连接很快被淘汰，不会一直复用同一个已断开的 socket。
            # v1.7.6：keepalive 再从 30s 收到 15s。不少中转/反代（含 CDN）
            # 的空闲超时就在 15~30s 之间：长任务里工具跑了 20 秒再回来调
            # 模型，正好拿到一条已被对端关掉的连接 → "Connection error."。
            # 传输层同时对**建连失败**（DNS 抖动、握手被 RST）自动重试 2 次 ——
            # 只作用于"请求还没发出去"的阶段，不会让同一请求被服务端处理两遍。
            # 注意：传了 transport 后 httpx 会忽略客户端级的 limits，必须挂在这里。
            "transport": httpx.AsyncHTTPTransport(
                verify=ctx, retries=2,
                limits=httpx.Limits(max_connections=50,
                                    max_keepalive_connections=10,
                                    keepalive_expiry=15.0)),
        }
        if base_url:
            kwargs["base_url"] = base_url
        return httpx.AsyncClient(**kwargs)
    except Exception as e:
        logger.warning("http_client_build_failed", error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════
# 抽象基类
# ═══════════════════════════════════════════════════════════════


class LLMBackend(ABC):
    """LLM 后端抽象基类。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        self.config = config
        self._tools: list[dict[str, Any]] = []
        # 每次调用结束后上报用量的旁路回调（可选）。
        # 走**旁路**而不是改返回值/流格式：流式的 usage 此前只在流末尾以
        # `<!--STREAM_USAGE:...-->` 标记带出，界面上的 token 数因此要等整段
        # 回答生成完才跳一次；长回答期间用户看到的一直是 0。
        # 签名：async (usage: dict) -> None，usage 含 prompt/completion/total/
        # model/provider/streamed。回调异常绝不能影响主流程。
        self.usage_sink: Any = None
        # 每次调用**前**的钩子（可选）。签名 async () -> None。
        # 与 usage_sink 对称：一个管调用后的记账，一个管调用前的准入
        # （Token 预算、速率限制）。抛异常即拒绝本次调用 —— 这是它的用途，
        # 因此这里**不**吞异常。
        self.pre_call_hook: Any = None
        # 单轮调用的**硬超时**（秒）。provider 自身的 timeout 只管单个 HTTP
        # 请求；流式响应一旦服务端"连上了但不再吐字"，底层不会超时，整个任务
        # 就无限期挂着，界面上停在"执行中"永远不动。这里给一个总时长上限。
        self.call_timeout: float = 300.0
        # 心跳回调：调用在飞行中时每隔 heartbeat_interval 秒回调一次，
        # 签名 async (elapsed_seconds: float, phase: str) -> None。
        # 让界面能显示"正在思考 已 N 秒"，而不是一片死寂。
        self.heartbeat_hook: Any = None
        self.heartbeat_interval: float = 5.0

    #: 各类瞬时错误在 provider 层最多尝试几次（含首次）。连接/5xx/限流给得
    #: 最多：它们几乎都是几秒到几十秒就恢复的抖动；超时只再试一次 ——
    #: 单次就可能等满 ``config.timeout``，多试只会把整轮拖到 call_timeout。
    RETRY_ATTEMPTS: dict[str, int] = {
        "parse": 3, "timeout": 2, "rate_limit": 5, "connection": 5, "server": 5,
    }

    async def _call_with_retry(self, make_call: Any, *,
                               parse_attempts: int | None = None) -> Any:
        """执行一次 SDK 调用，对瞬时错误按类别指数退避重试（v1.7.6）。

        ``make_call`` 是**无参协程工厂**（每次重试都要一个新协程）。不可重试的
        错误交给 ``_handle_error`` 翻译后立即抛出；重试用尽则抛出带排障说明的
        ``LLMConnectionError`` / ``LLMRateLimitError`` / ``LLMTimeoutError`` ——
        这三类会被 ReAct 的任务层重连识别为"可以等一等再接着做"。
        """
        from automind.reflection.retry_handler import RetryConfig, RetryHandler

        budgets = dict(self.RETRY_ATTEMPTS)
        if parse_attempts is not None:
            budgets["parse"] = max(1, parse_attempts)
        fast = RetryHandler(RetryConfig(base_delay=0.8, max_delay=5.0))
        slow = RetryHandler(RetryConfig(base_delay=2.0, max_delay=30.0))
        failures = 0
        while True:
            try:
                return await make_call()
            except Exception as e:
                kind = classify_llm_error(e)
                if kind not in TRANSIENT_KINDS:
                    self._handle_error(e)
                    raise
                failures += 1
                if failures >= budgets.get(kind, 1):
                    raise self._exhausted_error(e, kind, failures) from e
                logger.warning("llm_transient_retry", kind=kind, attempt=failures,
                               provider=self.config.provider, error=str(e)[:200])
                await (fast if kind == "parse" else slow).sleep_before_retry(failures - 1)

    def _exhausted_error(self, e: BaseException, kind: str, attempts: int) -> Exception:
        """重试用尽后的最终异常：类型表达"是哪类问题"，文本告诉人"该查什么"。"""
        base = str(getattr(self, "api_base", "") or self.config.api_base or "")
        if kind in ("connection", "server"):
            return LLMConnectionError(
                connection_error_message(e, base, attempts),
                code="CONNECTION" if kind == "connection" else "SERVER_ERROR")
        if kind == "rate_limit":
            return LLMRateLimitError(
                f"模型接口持续限流（已重试 {attempts} 次）：{str(e)[:160]}。"
                "请稍后再试，或在设置里降低并发/更换模型。", code="RATE_LIMIT")
        if kind == "timeout":
            return LLMTimeoutError(
                f"模型接口响应超时（已重试 {attempts} 次）：{str(e)[:160]}。"
                "可能是网络较慢或上下文过长。", code="TIMEOUT")
        # 注意：这里**不能**报 LLMProviderNotFoundError —— "提供商不存在"会让
        # 排障方向完全跑偏，而真实原因是响应体损坏。
        return LLMError(
            f"模型响应异常（已重试 {attempts} 次）：{str(e)[:160]}。"
            f"常见原因：服务端返回了不完整的响应体（中转/兼容接口常见）、"
            f"或网络不稳定。请稍后重试或更换模型/中转地址。",
            code="RETRY_EXHAUSTED")

    def _handle_error(self, error: Exception) -> None:
        """把 SDK 异常翻译成 AutoMind 异常（默认实现：按分类映射）。"""
        if isinstance(error, LLMError):
            raise error         # 已经翻译过（如重试用尽的结果）—— 别再包一层
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            raise LLMAuthenticationError(
                f"API Key 无效或无权限（HTTP {status}）：{str(error)[:200]}",
                code="AUTH_ERROR") from error
        kind = classify_llm_error(error)
        if kind in ("connection", "server"):
            raise LLMConnectionError(
                connection_error_message(error, str(getattr(self, "api_base", "") or "")),
                code="CONNECTION" if kind == "connection" else "SERVER_ERROR") from error
        if kind == "rate_limit":
            raise LLMRateLimitError(str(error), code="RATE_LIMIT") from error
        if kind == "timeout":
            raise LLMTimeoutError(str(error), code="TIMEOUT") from error
        raise error

    async def _with_heartbeat(self, coro: Any, phase: str) -> Any:
        """在 coro 执行期间周期性发心跳，并施加 call_timeout 硬超时。"""
        import asyncio
        import time as _t

        if self.heartbeat_hook is None:
            return await asyncio.wait_for(coro, timeout=self.call_timeout)

        started = _t.monotonic()
        task = asyncio.ensure_future(coro)

        async def _beat() -> None:
            while not task.done():
                await asyncio.sleep(self.heartbeat_interval)
                if task.done():
                    break
                try:
                    await self.heartbeat_hook(_t.monotonic() - started, phase)
                except Exception as e:
                    logger.warning("heartbeat_hook_failed", error=str(e))

        beat = asyncio.ensure_future(_beat())
        try:
            return await asyncio.wait_for(task, timeout=self.call_timeout)
        finally:
            beat.cancel()
            # 等它真正结束，避免留下悬挂任务（"Task was destroyed" 噪音）
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat

    async def _report_usage(self, prompt: int, completion: int,
                            streamed: bool = False, *,
                            cached: int = 0, cache_creation: int = 0,
                            input_total: int = 0,
                            cache_reported: bool = False) -> None:
        """把本次调用的用量交给 usage_sink（失败只记日志，不打断生成）。

        v1.11.10：除 prompt/completion 外再带上提示缓存口径。``input_total``
        是**总输入**（含命中），与 ``prompt`` 在 Anthropic 上不是同一个数 ——
        命中率必须按总输入算，否则分母偏小、命中率虚高。``cache_reported``
        区分"报了 0 命中"与"压根不报缓存"，界面据此显示 0% 还是"—"。
        """
        if self.usage_sink is None:
            return
        try:
            await self.usage_sink({
                "prompt_tokens": int(prompt or 0),
                "completion_tokens": int(completion or 0),
                "total_tokens": int(prompt or 0) + int(completion or 0),
                "cached_tokens": int(cached or 0),
                "cache_creation_tokens": int(cache_creation or 0),
                "input_tokens": int(input_total or prompt or 0),
                "cache_reported": bool(cache_reported),
                "model": getattr(self, "_model", "") or self.config.model,
                "provider": self.config.provider,
                "streamed": streamed,
            })
        except Exception as e:
            logger.warning("usage_sink_failed", error=str(e))

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """给每个 provider 的 generate / generate_stream 自动挂上用量上报。

        用 __init_subclass__ 统一包一层，而不是去改 4 个 provider 的 8 个返回点
        —— 少改 8 处就少 8 处漏掉的机会，将来新增 provider 也自动带上。
        包装器**原样透传**每个 chunk，绝不改动流内容与既有的
        `<!--STREAM_USAGE:...-->` 标记格式（约束要求）。
        """
        super().__init_subclass__(**kwargs)
        import functools

        gen = cls.__dict__.get("generate")
        if gen is not None and not getattr(gen, "_usage_wrapped", False):
            @functools.wraps(gen)
            async def _gen(self: Any, *a: Any, **k: Any) -> Any:
                _t0 = time.monotonic()
                if self.pre_call_hook is not None:
                    await self.pre_call_hook()      # 预算/限流：拒绝就让它抛
                try:
                    resp = await self._with_heartbeat(gen(self, *a, **k), "thinking")
                except TimeoutError as e:
                    raise LLMTimeoutError(
                        f"LLM 单轮调用超过 {self.call_timeout:.0f} 秒仍未返回，已中止。"
                        "常见原因：模型服务无响应、网络中断、或上下文过长。"
                    ) from e
                # 可重放轨迹（默认关闭时 record_call 只做一次布尔判断）。这里能同时
                # 拿到"确切入参 + 真实响应 + 耗时"，且不需要改任何 provider 的签名。
                try:
                    from automind.core.replay import record_call, split_generate_args
                    _msgs, _tools = split_generate_args(a, k)
                    record_call(self, resp, time.monotonic() - _t0,
                                messages=_msgs, tools=_tools)
                except Exception:
                    pass        # 记录问题绝不能让模型调用失败
                await self._report_usage(
                    getattr(resp, "prompt_tokens", 0),
                    getattr(resp, "completion_tokens", 0),
                    cached=getattr(resp, "cached_tokens", 0),
                    cache_creation=getattr(resp, "cache_creation_tokens", 0),
                    input_total=getattr(resp, "input_tokens", 0),
                    cache_reported=getattr(resp, "cache_reported", False))
                return resp
            _gen._usage_wrapped = True       # type: ignore[attr-defined]
            cls.generate = _gen              # type: ignore[assignment]

        gs = cls.__dict__.get("generate_stream")
        if gs is not None and not getattr(gs, "_usage_wrapped", False):
            @functools.wraps(gs)
            async def _gs(self: Any, *a: Any, **k: Any) -> Any:
                import json as _j
                import re as _re
                if self.pre_call_hook is not None:
                    await self.pre_call_hook()
                import time as _tm
                started = _tm.monotonic()
                last_beat = started
                p = c = 0
                cached = 0
                input_total = 0
                cache_reported = False
                async for chunk in gs(self, *a, **k):
                    now = _tm.monotonic()
                    # 流式没法用 wait_for 包整体（那会连正常的长回答一起掐掉），
                    # 改为每收到一块就检查累计时长 —— 超了立刻中止。
                    if now - started > self.call_timeout:
                        raise LLMTimeoutError(
                            f"LLM 流式响应超过 {self.call_timeout:.0f} 秒仍未结束，已中止。")
                    if (self.heartbeat_hook is not None
                            and now - last_beat >= self.heartbeat_interval):
                        last_beat = now
                        try:
                            await self.heartbeat_hook(now - started, "streaming")
                        except Exception as e:
                            logger.warning("heartbeat_hook_failed", error=str(e))
                    # 顺路把标记里的用量抄出来 —— chunk 本身原样 yield 出去
                    if isinstance(chunk, str) and "STREAM_USAGE:" in chunk:
                        m = _re.search(r"STREAM_USAGE:(\{.*?\})", chunk)
                        if m:
                            try:
                                u = _j.loads(m.group(1))
                                p = u.get("prompt_tokens", 0)
                                c = u.get("completion_tokens", 0)
                                cached = u.get("cached_tokens", 0)
                                input_total = u.get("input_tokens", 0)
                                cache_reported = bool(u.get("cache_reported", False))
                            except ValueError:
                                pass
                    yield chunk
                await self._report_usage(p, c, streamed=True, cached=cached,
                                         input_total=input_total,
                                         cache_reported=cache_reported)
            _gs._usage_wrapped = True        # type: ignore[attr-defined]
            cls.generate_stream = _gs        # type: ignore[assignment]

    @abstractmethod
    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """生成回复 (非流式)。"""

    @abstractmethod
    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """生成回复 (流式)。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本嵌入向量。"""
        raise NotImplementedError(f"{self.__class__.__name__} does not support embeddings")

    async def close(self) -> None:
        """释放后端持有的网络资源（默认无操作，子类按需覆盖）。"""
        return None

    def token_count(self, text: str) -> int:
        """估算文本的 token 数量。"""
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return len(text) // 4

    def supports_tools(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False

    def register_tools(self, tools: list[dict[str, Any]]) -> None:
        self._tools = tools


# ═══════════════════════════════════════════════════════════════
# OpenAI Provider (含所有 OpenAI 兼容 API)
# ═══════════════════════════════════════════════════════════════


class OpenAIProvider(LLMBackend):
    """OpenAI 及所有兼容 API 提供商。

    覆盖: OpenAI, DeepSeek, Kimi (Moonshot), 阿里百炼 (DashScope),
           智谱 (GLM), 豆包 (ByteDance), 及其他 OpenAI 兼容服务。
    """

    PROVIDER_API_BASES: dict[str, str] = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "bailian": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "doubao": "https://ark.cn-beijing.volces.com/api/v3",
        "custom": "",
    }

    PROVIDER_DEFAULT_MODELS: dict[str, str] = {
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "kimi": "moonshot-v1-128k",
        "moonshot": "moonshot-v1-128k",
        "bailian": "qwen-max",
        "dashscope": "qwen-max",
        "qwen": "qwen-max",
        "zhipu": "glm-4-plus",
        "glm": "glm-4-plus",
        "doubao": "doubao-pro-128k",
    }

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        from openai import AsyncOpenAI

        api_key = config.api_key
        if not api_key:
            # 自定义/中转代理常常使用占位 Key；仅在确实为空时报错
            raise LLMAuthenticationError(
                f"未配置 API Key（提供商 '{config.provider}'）。"
                f"请在「API Keys」面板填写，或设置对应的环境变量。"
            )

        # 优先使用用户自定义 api_base（中转/代理），否则用内置默认值
        # 仅当自定义 api_base 是有效 URL 时才使用
        raw_base = config.api_base or ""
        if raw_base and (raw_base.startswith("http://") or raw_base.startswith("https://")):
            api_base = raw_base
        else:
            api_base = self.PROVIDER_API_BASES.get(config.provider, "")
        if not api_base:
            raise LLMProviderNotFoundError(
                f"提供商 '{config.provider}' 缺少 API 地址（api_base）。"
                f"使用自定义/中转代理时，请在模型配置中填写 API 地址。"
            )

        model = (
            config.model
            or self.PROVIDER_DEFAULT_MODELS.get(config.provider, "")
            or "gpt-4o"
        )

        # 复用共享 TLS 上下文：不传 http_client 时 openai 会新建 3 个 SSL 上下文，
        # 单次约 1.15 秒 —— 这才是"切模型卡两三秒"的真正来源
        http_client = shared_http_client(config.timeout)
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=request_timeout(config.timeout),
            max_retries=config.max_retries,
            default_headers=config.extra_headers or None,
            **({"http_client": http_client} if http_client is not None else {}),
        )
        self._model = model
        self.api_base = api_base

    async def close(self) -> None:
        """关闭底层 httpx 连接池。

        此前基类的 no-op 意味着客户端从不释放；会话级 Agent 会为每个会话建一个
        客户端，不关就是持续泄漏连接与文件句柄。
        """
        with contextlib.suppress(Exception):
            await self._client.close()

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        tools = tools or self._tools
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]
        if stop:
            kwargs["stop"] = stop
        if self.config.extra_body:
            kwargs["extra_body"] = self.config.extra_body

        resp = await self._create_with_retry(kwargs)

        choice = resp.choices[0]
        tool_calls = None
        if choice.message.tool_calls:
            from automind.core.json_utils import parse_tool_arguments_checked
            tool_calls = []
            for tc in choice.message.tool_calls:
                args, arg_err = parse_tool_arguments_checked(tc.function.arguments)
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name,
                                           arguments=args, arguments_error=arg_err))

        cached, cache_creation, input_total, cache_reported = extract_cache_usage(
            getattr(resp, "usage", None))
        return LLMResponse(
            text=choice.message.content or "",
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            finish_reason=choice.finish_reason or "stop",
            provider=self.config.provider,
            model=self._model,
            raw=resp.model_dump(),
            # v1.11.10：DeepSeek / GLM / OpenAI 等的提示缓存命中口径
            cached_tokens=cached,
            cache_creation_tokens=cache_creation,
            input_tokens=input_total,
            cache_hit=cached > 0,
            cache_reported=cache_reported,
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """流式生成文本。最后一个 chunk 前附加 usage 元数据（以 STREAM_USAGE: 前缀）。"""
        tools = tools or self._tools
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]

        import json as _json
        last_usage = None
        try:
            stream = await self._create_with_retry(kwargs)
        except Exception as e:
            self._handle_error(e)
            return
        try:
            async for chunk in stream:
                try:
                    # 提取 usage（DeepSeek / OpenAI 在最后 chunk 中返回）
                    if hasattr(chunk, "usage") and chunk.usage:
                        last_usage = chunk.usage
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                except (_json.JSONDecodeError, AttributeError, IndexError):
                    continue
            # 流结束后将 usage 编码为特殊标记
            if last_usage:
                cached, cache_creation, input_total, cache_reported = \
                    extract_cache_usage(last_usage)
                usage_json = _json.dumps({
                    "prompt_tokens": getattr(last_usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(last_usage, "completion_tokens", 0),
                    # v1.11.10：流式也把缓存口径带出去（旧客户端忽略未知键即可）
                    "cached_tokens": cached,
                    "cache_creation_tokens": cache_creation,
                    "input_tokens": input_total,
                    "cache_reported": cache_reported,
                })
                yield f"\n<!--STREAM_USAGE:{usage_json}-->"
        except _json.JSONDecodeError:
            return
        except Exception as e:
            self._handle_error(e)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(
            model=self.config.extra_body.get("embedding_model", "text-embedding-3-small"),
            input=texts,
        )
        return [d.embedding for d in resp.data]

    async def _create_with_retry(self, kwargs: dict[str, Any], attempts: int = 3):
        """调用 chat.completions.create，对瞬时错误自动重试。

        * 响应体损坏/截断（中转常见的 ``JSONDecodeError``）→ 最多 ``attempts`` 次；
        * 429 限流 → 指数退避（v1.7.5）；
        * **v1.7.6：连接断开 / 中转 5xx → 指数退避重试**。此前靠关键字
          "connection reset" / "peer closed" 识别网络错误，而 SDK 最常见的那个
          ``APIConnectionError`` 文本只是 ``"Connection error."`` —— 一个词都
          不中，于是不重试、直接让任务失败。现在按异常类型/状态码归类
          （见 :func:`classify_llm_error`）。
        """
        return await self._call_with_retry(
            lambda: self._client.chat.completions.create(**kwargs),
            parse_attempts=attempts)

    def _handle_error(self, error: Exception) -> None:
        if isinstance(error, LLMError):
            raise error
        msg = str(error).lower()
        if "401" in msg or "403" in msg or "unauthorized" in msg or "invalid api key" in msg:
            raise LLMAuthenticationError(str(error), code="AUTH_ERROR") from error
        if "context" in msg and ("too long" in msg or "exceed" in msg):
            raise LLMContextTooLargeError(str(error), code="CONTEXT_TOO_LARGE") from error
        super()._handle_error(error)


# ═══════════════════════════════════════════════════════════════
# Anthropic Provider
# ═══════════════════════════════════════════════════════════════


class AnthropicProvider(LLMBackend):
    """Anthropic Claude API 提供商。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        from anthropic import AsyncAnthropic

        api_key = config.api_key
        if not api_key:
            raise LLMAuthenticationError(
                "Anthropic API key not configured. Set ANTHROPIC_API_KEY env var."
            )

        http_client = shared_http_client(config.timeout)   # 同 OpenAI：省掉 TLS 重建
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=request_timeout(config.timeout),
            **({"http_client": http_client} if http_client is not None else {}),
        )
        self._model = config.model or "claude-sonnet-4-20250514"

    async def close(self) -> None:
        """关闭底层 httpx 连接池（同 OpenAIProvider：不关就持续泄漏连接）。"""
        with contextlib.suppress(Exception):
            await self._client.close()

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        system_prompt, user_messages = self._split_messages(messages)
        tools = tools or self._tools

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": user_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._convert_tools_anthropic(tools)
        if stop:
            kwargs["stop_sequences"] = stop

        # v1.7.6：此前 Anthropic 路径只有 SDK 自带的 2 次快速重试，中转抖动
        # 几秒就让任务失败；现在与 OpenAI 路径共用同一套分类退避。
        resp = await self._call_with_retry(
            lambda: self._client.messages.create(**kwargs))

        text = ""
        tool_calls = None
        for block in resp.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or {},
                ))

        cached, cache_creation, input_total, cache_reported = extract_cache_usage(
            getattr(resp, "usage", None))
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.input_tokens if resp.usage else 0,
            completion_tokens=resp.usage.output_tokens if resp.usage else 0,
            finish_reason=resp.stop_reason or "stop",
            provider="anthropic",
            model=self._model,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else {},
            # Anthropic 的 input_tokens 不含缓存读取，真正的总输入由 helper 归一化
            cached_tokens=cached,
            cache_creation_tokens=cache_creation,
            input_tokens=input_total,
            cache_hit=cached > 0,
            cache_reported=cache_reported,
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        system_prompt, user_messages = self._split_messages(messages)
        tools = tools or self._tools

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": user_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            self._handle_error(e)

    def _split_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """分离 system prompt 和对话消息。"""
        system_parts = []
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                user_messages.append(m)
        return "\n".join(system_parts), user_messages

    def _convert_tools_anthropic(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 OpenAI 格式 tools 转为 Anthropic 格式。"""
        converted = []
        for tool in tools:
            converted.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": {
                    "type": "object",
                    "properties": tool.get("parameters", {}).get("properties", {}),
                    "required": tool.get("parameters", {}).get("required", []),
                },
            })
        return converted

    def _handle_error(self, error: Exception) -> None:
        if isinstance(error, LLMError):
            raise error
        msg = str(error).lower()
        if "401" in msg or "403" in msg or "invalid" in msg and "key" in msg:
            raise LLMAuthenticationError(str(error), code="AUTH_ERROR") from error
        if "prompt is too long" in msg or ("context" in msg and "exceed" in msg):
            raise LLMContextTooLargeError(str(error), code="CONTEXT_TOO_LARGE") from error
        super()._handle_error(error)


# ═══════════════════════════════════════════════════════════════
# Google Gemini Provider
# ═══════════════════════════════════════════════════════════════


class GoogleProvider(LLMBackend):
    """Google Gemini API 提供商。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        import google.generativeai as genai

        api_key = config.api_key
        if not api_key:
            raise LLMAuthenticationError(
                "Google API key not configured. Set GOOGLE_API_KEY env var."
            )

        genai.configure(api_key=api_key)
        self._model_name = config.model or "gemini-2.5-flash"
        self._model = genai.GenerativeModel(self._model_name)

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        contents = self._convert_messages(messages)

        try:
            resp = await self._model.generate_content_async(
                contents,
                generation_config={
                    "temperature": self.config.temperature,
                    "max_output_tokens": self.config.max_tokens,
                },
            )
        except Exception as e:
            self._handle_error(e)

        cached, cache_creation, input_total, cache_reported = extract_cache_usage(
            getattr(resp, "usage_metadata", None))
        return LLMResponse(
            text=resp.text or "",
            prompt_tokens=resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0,
            completion_tokens=resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0,
            provider="google",
            model=self._model_name,
            raw={"text": resp.text},
            # Gemini 的 cached_content_token_count 是 prompt_token_count 的子集
            cached_tokens=cached,
            cache_creation_tokens=cache_creation,
            input_tokens=input_total,
            cache_hit=cached > 0,
            cache_reported=cache_reported,
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        contents = self._convert_messages(messages)
        try:
            resp = await self._model.generate_content_async(
                contents,
                generation_config={
                    "temperature": self.config.temperature,
                    "max_output_tokens": self.config.max_tokens,
                },
                stream=True,
            )
            async for chunk in resp:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            self._handle_error(e)

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts = []
        for m in messages:
            role = "user" if m["role"] != "model" else "model"
            parts.append({"role": role, "parts": [m["content"]]})
        return parts

    def _handle_error(self, error: Exception) -> None:
        msg = str(error).lower()
        if "401" in msg or "403" in msg or "api key" in msg:
            raise LLMAuthenticationError(str(error), code="AUTH_ERROR") from error
        super()._handle_error(error)


# ═══════════════════════════════════════════════════════════════
# Grok Provider
# ═══════════════════════════════════════════════════════════════


class GrokProvider(OpenAIProvider):
    """Grok API (OpenAI 兼容)。"""

    PROVIDER_API_BASES = {"grok": "https://api.x.ai/v1"}
    PROVIDER_DEFAULT_MODELS = {"grok": "grok-3"}


# ═══════════════════════════════════════════════════════════════
# Ollama Provider (本地模型)
# ═══════════════════════════════════════════════════════════════


class OllamaProvider(LLMBackend):
    """Ollama 本地模型提供商。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        import httpx
        base_url = config.api_base or "http://localhost:11434"
        self._client = (shared_http_client(config.timeout, base_url)
                        or httpx.AsyncClient(base_url=base_url, timeout=config.timeout))
        self._model = config.model or "llama3.2"

    async def close(self) -> None:
        """关闭底层 httpx 连接池。"""
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self._handle_error(e)

        return LLMResponse(
            text=data.get("message", {}).get("content", ""),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            provider="ollama",
            model=self._model,
            raw=data,
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        data = json.loads(line)
                        content = data.get("message", {}).get("content", "")
                        if content:
                            yield content
        except Exception as e:
            self._handle_error(e)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for text in texts:
            resp = await self._client.post("/api/embeddings", json={
                "model": self._model,
                "prompt": text,
            })
            resp.raise_for_status()
            data = resp.json()
            embeddings.append(data.get("embedding", []))
        return embeddings

    def supports_tools(self) -> bool:
        return False

    def _handle_error(self, error: Exception) -> None:
        msg = str(error).lower()
        if "connect" in msg or "refused" in msg:
            raise LLMConnectionError(
                f"Cannot connect to Ollama at {self.config.api_base or 'http://localhost:11434'}. "
                f"Is Ollama running?",
                code="CONNECTION_REFUSED",
            ) from error
        super()._handle_error(error)


# ═══════════════════════════════════════════════════════════════
# 工厂
# ═══════════════════════════════════════════════════════════════


class LLMBackendFactory:
    """LLM 后端工厂 — 根据提供商名称创建对应的后端实例。"""

    _providers: dict[str, type[LLMBackend]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "google": GoogleProvider,
        "gemini": GoogleProvider,
        "grok": GrokProvider,
        "deepseek": OpenAIProvider,
        "kimi": OpenAIProvider,
        "moonshot": OpenAIProvider,
        "bailian": OpenAIProvider,
        "dashscope": OpenAIProvider,
        "qwen": OpenAIProvider,
        "zhipu": OpenAIProvider,
        "glm": OpenAIProvider,
        "doubao": OpenAIProvider,
        "ollama": OllamaProvider,
        # 自定义 OpenAI 标准接口（支持中转/代理）
        "custom": OpenAIProvider,
        "openai_compatible": OpenAIProvider,
    }

    @classmethod
    def create(cls, provider: str, config: LLMProviderConfig) -> LLMBackend:
        """创建 LLM 后端实例。

        Args:
            provider: 提供商名称 (openai, anthropic, google, deepseek, ollama, etc.)。
            config: LLM 提供商配置。

        Returns:
            对应提供商的 LLMBackend 实例。

        Raises:
            LLMProviderNotFoundError: 未知的提供商。
        """
        provider_lower = provider.lower()
        provider_cls = cls._providers.get(provider_lower)
        if provider_cls is None:
            available = ", ".join(sorted(cls._providers.keys()))
            raise LLMProviderNotFoundError(
                f"Unknown LLM provider '{provider}'. Available: {available}"
            )
        # 确保 provider 名称写入 config
        config = config.model_copy(update={"provider": provider_lower})
        return provider_cls(config)

    @classmethod
    def register(cls, name: str, backend_cls: type[LLMBackend]) -> None:
        """注册自定义 LLM 后端。"""
        cls._providers[name.lower()] = backend_cls

    @classmethod
    def available_providers(cls) -> list[str]:
        """返回所有已注册的提供商名称。"""
        return sorted(cls._providers.keys())
