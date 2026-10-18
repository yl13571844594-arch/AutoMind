"""v1.7.6：长任务中途 "Connection error." 不再直接判任务失败。

回归点：
1. openai SDK 的 ``APIConnectionError``（文本恰为 "Connection error."）此前不命中
   任何重试关键字，被当成不可重试错误原样上抛。
2. provider 层重试用尽后，ReAct 任务层还要再等一等重连；真连不上时，已做过
   实事的任务交部分交付清单，而不是整轮作废。
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from automind.core.config import LLMProviderConfig
from automind.core.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMRateLimitError,
)
from automind.core.llm import (
    LLMBackend,
    OpenAIProvider,
    classify_llm_error,
    is_transient_llm_error,
)
from automind.core.types import LLMResponse, ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor
from automind.tools.base import AbstractTool, ToolRegistry

_REQ = httpx.Request("POST", "https://relay.example/v1/chat/completions")


def _status_error(cls, code: int):
    return cls("boom", response=httpx.Response(code, request=_REQ), body=None)


# ── 分类 ─────────────────────────────────────────────────────


def test_sdk_connection_error_is_transient():
    e = openai.APIConnectionError(request=_REQ)
    assert str(e) == "Connection error."          # 用户看到的正是这句
    assert classify_llm_error(e) == "connection"
    assert is_transient_llm_error(e)


@pytest.mark.parametrize("exc,kind", [
    (openai.APITimeoutError(request=_REQ), "timeout"),
    (httpx.ConnectError("x"), "connection"),
    (httpx.RemoteProtocolError("Server disconnected without sending a response."), "connection"),
    (ConnectionResetError(10054, "reset"), "connection"),
    (json.JSONDecodeError("Unterminated string", "x", 0), "parse"),
    (RuntimeError("502 Bad Gateway from upstream"), "server"),
])
def test_transient_kinds(exc, kind):
    assert classify_llm_error(exc) == kind


def test_status_codes():
    assert classify_llm_error(_status_error(openai.InternalServerError, 503)) == "server"
    assert classify_llm_error(_status_error(openai.RateLimitError, 429)) == "rate_limit"
    assert classify_llm_error(_status_error(openai.AuthenticationError, 401)) == "fatal"
    assert classify_llm_error(_status_error(openai.BadRequestError, 400)) == "fatal"


def test_our_own_errors_classified():
    assert classify_llm_error(LLMConnectionError("x", code="CONNECTION")) == "connection"
    assert classify_llm_error(LLMAuthenticationError("x")) == "fatal"


# ── provider 层重试 ──────────────────────────────────────────


def _provider():
    cfg = LLMProviderConfig(provider="custom", model="m", api_key="sk-test",
                            api_base="https://relay.example/v1")
    p = OpenAIProvider(cfg)
    return p


@pytest.fixture
def no_sleep(monkeypatch):
    async def _nosleep(self, attempt):
        return 0.0
    from automind.reflection.retry_handler import RetryHandler
    monkeypatch.setattr(RetryHandler, "sleep_before_retry", _nosleep)


@pytest.mark.asyncio
async def test_connection_error_is_retried_then_succeeds(no_sleep):
    p = _provider()
    calls = {"n": 0}

    async def create(**kw):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise openai.APIConnectionError(request=_REQ)
        return {"ok": True}

    p._client.chat.completions.create = create
    assert await p._create_with_retry({}) == {"ok": True}
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_connection_retry_exhausted_gives_actionable_error(no_sleep):
    p = _provider()

    async def create(**kw):
        raise openai.APIConnectionError(request=_REQ)

    p._client.chat.completions.create = create
    with pytest.raises(LLMConnectionError) as ei:
        await p._create_with_retry({})
    msg = str(ei.value)
    assert "relay.example" in msg and "重试" in msg and "Connection error." in msg
    assert is_transient_llm_error(ei.value)


@pytest.mark.asyncio
async def test_auth_error_not_retried(no_sleep):
    p = _provider()
    calls = {"n": 0}

    async def create(**kw):
        calls["n"] += 1
        raise _status_error(openai.AuthenticationError, 401)

    p._client.chat.completions.create = create
    with pytest.raises(LLMAuthenticationError):
        await p._create_with_retry({})
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_rate_limit_exhausted_maps_to_rate_limit_error(no_sleep):
    p = _provider()

    async def create(**kw):
        raise _status_error(openai.RateLimitError, 429)

    p._client.chat.completions.create = create
    with pytest.raises(LLMRateLimitError):
        await p._create_with_retry({})


def test_http_client_keeps_pool_limits_on_custom_transport():
    from automind.core.llm import shared_http_client
    c = shared_http_client(30.0)
    assert c is not None
    pool = c._transport._pool
    assert pool._keepalive_expiry == 15.0
    assert pool._max_connections == 50


# ── 任务层重连 ───────────────────────────────────────────────


class _EchoTool(AbstractTool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, **kw):
        return ToolResult(tool_name="echo", success=True, output=kw.get("text", ""))


class _ScriptedLLM(LLMBackend):
    """按脚本返回：'tool' = 调一次工具，'done' = 给出答案，异常实例 = 抛出。"""

    def __init__(self, script):
        super().__init__(LLMProviderConfig(provider="custom", api_key="x"))
        self.script = list(script)
        self.calls = 0

    async def generate(self, messages, tools=None, stop=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else "done"
        if isinstance(step, BaseException):
            raise step
        if step == "tool":
            return LLMResponse(text="调用工具", tool_calls=[
                ToolCall(id=f"c{self.calls}", name="echo",
                         arguments={"text": f"t{self.calls}"})])
        return LLMResponse(text="全部完成", tool_calls=None)

    async def generate_stream(self, messages, tools=None):  # pragma: no cover
        yield ""


def _executor(llm):
    reg = ToolRegistry()
    reg.register(_EchoTool())
    ex = ReActExecutor(llm, reg, max_iterations=10)
    ex.llm_reconnect_delays = (0.0, 0.0)
    return ex


def _conn_err():
    return LLMConnectionError("无法连接模型服务：Connection error.", code="CONNECTION")


@pytest.mark.asyncio
async def test_react_reconnects_and_finishes():
    llm = _ScriptedLLM(["tool", _conn_err(), "tool", "done"])
    ex = _executor(llm)
    events = []

    async def on_reconnect(ev):
        events.append(ev)

    out = await ex.run("任务", on_reconnect=on_reconnect)
    assert ex.stop_reason == "no_more_tools"
    assert "全部完成" in out
    assert [e["state"] for e in events] == ["waiting", "recovered"]


@pytest.mark.asyncio
async def test_react_outage_after_progress_delivers_partial_manifest():
    llm = _ScriptedLLM(["tool", _conn_err(), _conn_err(), _conn_err()])
    ex = _executor(llm)
    out = await ex.run("任务")
    assert ex.stop_reason == "llm_unavailable"
    assert "部分交付清单" in out and "成功动作：1" in out


@pytest.mark.asyncio
async def test_react_outage_before_any_progress_still_raises():
    llm = _ScriptedLLM([_conn_err(), _conn_err(), _conn_err()])
    ex = _executor(llm)
    with pytest.raises(LLMConnectionError):
        await ex.run("任务")


@pytest.mark.asyncio
async def test_react_fatal_error_not_retried():
    llm = _ScriptedLLM(["tool", LLMAuthenticationError("bad key")])
    ex = _executor(llm)
    with pytest.raises(LLMAuthenticationError):
        await ex.run("任务")
    assert llm.calls == 2
