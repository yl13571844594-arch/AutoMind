"""v1.11.6 网络韧性：网络中断不再让 terminal "连续失败 3 次被熔断"。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from automind.core import netresilience as nr
from automind.core.types import ToolResult


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """不真的睡、不真的探测网络。"""
    async def no_sleep(_s):
        return None
    monkeypatch.setattr(nr.asyncio, "sleep", no_sleep)
    state = {"up": True}

    async def fake_up(timeout: float = 3.0, *, fresh: bool = False):
        return state["up"], "probe"
    monkeypatch.setattr(nr, "network_up", fake_up)
    return state


@pytest.mark.parametrize("text", [
    "fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com",
    "WARNING: Retrying (Retry(total=4)) after connection broken by 'NewConnectionError'",
    "npm ERR! code ECONNRESET",
    "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', port=443): Read timed out.",
    "curl: (6) Could not resolve host: example.com",
    "由于连接方在一段时间后没有正确答复或连接的主机没有反应，连接尝试失败。",
    "error: RPC failed; curl 56 GnuTLS recv error: The TLS connection was non-properly terminated. fatal: early EOF",
    "ConnectionRefusedError: [Errno 111] Connection refused to registry.example.com:443",
])
def test_network_errors_recognized(text):
    assert nr.is_network_error(text)


@pytest.mark.parametrize("text", [
    "SyntaxError: invalid syntax",
    "ModuleNotFoundError: No module named 'foo'",
    "ConnectionRefusedError: [Errno 111] Connection refused (localhost:8000)",
    "requests.exceptions.ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=5000): Max retries exceeded",
    "",
])
def test_non_network_errors_not_recognized(text):
    assert not nr.is_network_error(text)


# ── 文件锁判定（单一事实来源；fs_lock_guard 插件调用它决定是否重试）──


@pytest.mark.parametrize("text", [
    "[WinError 32] 另一个程序正在使用此文件，进程无法访问。",
    "PermissionError: [WinError 5] 拒绝访问。: 'report.xlsx'",
    "sqlite3.OperationalError: database is locked",
    "OSError: [Errno 11] Resource temporarily unavailable",
    "PermissionError: [Errno 13] Permission denied: 'data.csv'",
    "OSError: [Errno 26] Text file busy",
    "EBUSY: busy",
])
def test_transient_locks_recognized(text):
    assert nr.is_transient_lock(text)


@pytest.mark.parametrize("text", [
    "SyntaxError: invalid syntax",
    "路径越界，已拒绝",
    "FileNotFoundError: [Errno 2] No such file or directory",
    "KeyError: 'path'",
    "",
])
def test_non_lock_errors_are_not_locks(text):
    assert not nr.is_transient_lock(text)


@pytest.mark.parametrize(("cmd", "safe"), [
    ("pip install requests", True),
    ("python -m pip install -r requirements.txt", True),
    ("npm install", True),
    ("git clone https://github.com/a/b.git", True),
    ("curl -sSL https://example.com/x.json -o x.json", True),
    ("git push origin main", False),
    ("curl -X POST https://api.example.com/orders -d '{}'", False),
    ("npm publish", False),
    ("python build.py", False),
])
def test_retry_safe_commands(cmd, safe):
    assert nr.is_retry_safe("terminal", {"command": cmd}) is safe


def test_retry_safe_other_tools():
    assert nr.is_retry_safe("http_request", {"method": "GET"})
    assert not nr.is_retry_safe("http_request", {"method": "POST"})
    assert nr.is_retry_safe("web_search", {"query": "x"})
    assert not nr.is_retry_safe("terminal", {"command": "pip install x", "background": True})
    assert not nr.is_retry_safe("file_write", {"path": "a"})


def test_mirror_rewrite():
    assert "-i https://pypi.tuna.tsinghua.edu.cn/simple" in nr.with_mirror("pip install requests")
    assert nr.with_mirror("pip install -i https://x/simple requests") is None
    assert "--registry=https://registry.npmmirror.com" in nr.with_mirror("npm install lodash")
    assert nr.with_mirror("git clone x") is None


def _net_fail(tool="terminal"):
    return ToolResult(tool_name=tool, success=False, output={"stderr": "Could not resolve host: pypi.org"},
                      error="Could not resolve host: pypi.org", exit_code=1)


def test_dispatch_recovers_after_network_blip():
    calls: list[dict[str, Any]] = []
    outcomes = [_net_fail(), _net_fail(), ToolResult(tool_name="terminal", success=True,
                                                     output={"stdout": "ok"}, exit_code=0)]

    async def call_next(kw):
        calls.append(dict(kw))
        return outcomes[len(calls) - 1]

    res = asyncio.run(nr.ResilientDispatch()("terminal", {"command": "pip install requests"}, call_next))
    assert res.success and res.metadata["net_recovered"] and res.metadata["net_retries"] == 2
    assert calls[0]["command"] == "pip install requests"
    assert calls[1]["command"] == "pip install requests"            # 第一次重试先原样
    assert "pypi.tuna.tsinghua.edu.cn" in calls[2]["command"]         # 第二次起换镜像
    assert "network_note" in res.output


def test_dispatch_waits_for_network(_fast, monkeypatch):
    _fast["up"] = False
    waited = {"n": 0}

    async def fake_wait(max_wait: float = 60.0, interval: float = 3.0):
        waited["n"] += 1
        _fast["up"] = True
        return True, 12.0
    monkeypatch.setattr(nr, "wait_for_network", fake_wait)
    outcomes = [_net_fail(), ToolResult(tool_name="terminal", success=True, output={}, exit_code=0)]
    n = {"i": 0}

    async def call_next(kw):
        n["i"] += 1
        return outcomes[n["i"] - 1]
    res = asyncio.run(nr.ResilientDispatch()("terminal", {"command": "git clone https://x"}, call_next))
    assert res.success and waited["n"] == 1
    assert any("等待 12s 后恢复" in x for x in res.metadata["net_notes"])


def test_dispatch_gives_up_and_explains():
    async def call_next(kw):
        return _net_fail()
    res = asyncio.run(nr.ResilientDispatch(retries=2)("terminal", {"command": "git clone https://x"}, call_next))
    assert not res.success and res.metadata["network_error"] and res.metadata["net_retries"] == 2
    assert "wait_for_network" in res.error


def test_unsafe_command_not_retried_but_marked():
    n = {"i": 0}

    async def call_next(kw):
        n["i"] += 1
        return _net_fail()
    res = asyncio.run(nr.ResilientDispatch()("terminal", {"command": "git push origin main"}, call_next))
    assert n["i"] == 1 and res.metadata["network_error"] and res.metadata["net_retries"] == 0


def test_non_network_failure_passes_through():
    bad = ToolResult(tool_name="terminal", success=False, error="SyntaxError", exit_code=1)

    async def call_next(kw):
        return bad
    res = asyncio.run(nr.ResilientDispatch()("terminal", {"command": "pip install x"}, call_next))
    assert res is bad and "network_error" not in res.metadata


# ── 注册表中间件 + 插件 ─────────────────────────────────────────


def test_registry_runs_middleware_and_survives_broken_one():
    from automind.tools.base import AbstractTool, ToolRegistry

    class Echo(AbstractTool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

        async def execute(self, **kw):
            return ToolResult(tool_name="echo", success=True, output=kw.get("x"))

    async def upper(name, kw, nxt):
        return await nxt({**kw, "x": str(kw.get("x")).upper()})

    async def broken(name, kw, nxt):
        raise RuntimeError("boom")

    reg = ToolRegistry()
    reg.register(Echo())
    reg.set_middlewares([broken, upper])
    res = asyncio.run(reg.dispatch("echo", x="hi"))
    assert res.success and res.output == "HI"


def test_plugin_tools_do_not_shadow_builtins_and_unregister():
    from automind.tools.base import AbstractTool, ToolRegistry

    class T(AbstractTool):
        description = "t"
        parameters = {"type": "object", "properties": {}}

        def __init__(self, n):
            self.name = n

        async def execute(self, **kw):
            return ToolResult(tool_name=self.name, success=True, output=self.name)

    reg = ToolRegistry()
    builtin = T("terminal")
    reg.register(builtin)
    reg.sync_plugin_tools([T("terminal"), T("net_check")])
    assert reg.get("terminal") is builtin and "net_check" in reg
    reg.sync_plugin_tools([])
    assert "net_check" not in reg and "terminal" in reg


def test_builtin_plugin_provides_tools_and_middleware():
    from automind.core.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    assert pm.load("net_resilience") is not None
    names = {t.name for t in pm.assemble_tools()}
    assert {"net_check", "wait_for_network"} <= names
    assert any(isinstance(m, nr.ResilientDispatch) for m in pm.assemble_middlewares())


# ── 熔断器：网络类失败单独计数 + 半开 ──────────────────────────


def _executor():
    from automind.planning.react_executor import ReActExecutor
    ex = ReActExecutor.__new__(ReActExecutor)
    ex._tool_failures = {}
    return ex


def test_network_failures_do_not_trip_at_three():
    ex = _executor()
    for _ in range(3):
        ex._record_tool_outcome("terminal", False, "Could not resolve host", network=True)
    assert ex._breaker_reason("terminal") == ""
    for _ in range(3):
        ex._record_tool_outcome("terminal", False, "Could not resolve host", network=True)
    reason = ex._breaker_reason("terminal")
    assert "网络" in reason and "wait_for_network" in reason


def test_ordinary_failures_still_trip_at_three():
    ex = _executor()
    for _ in range(3):
        ex._record_tool_outcome("terminal", False, "SyntaxError")
    assert "已连续失败 3 次" in ex._breaker_reason("terminal")


def test_half_open_after_network_recovers(monkeypatch):
    ex = _executor()
    for _ in range(6):
        ex._record_tool_outcome("terminal", False, "ECONNRESET", network=True)
    assert ex._breaker_reason("terminal")

    async def up(timeout=3.0, *, fresh=False):
        return True, "ok"
    monkeypatch.setattr(nr, "network_up", up)
    assert asyncio.run(ex._network_half_open("terminal")) is True
    assert ex._breaker_reason("terminal") == ""


def test_half_open_has_a_hard_cap(monkeypatch):
    ex = _executor()
    for _ in range(ex.NET_FAILURE_CAP):
        ex._record_tool_outcome("terminal", False, "ECONNRESET", network=True)

    async def up(timeout=3.0, *, fresh=False):
        return True, "ok"
    monkeypatch.setattr(nr, "network_up", up)
    assert asyncio.run(ex._network_half_open("terminal")) is False


def test_ordinary_breaker_never_half_opens(monkeypatch):
    ex = _executor()
    for _ in range(3):
        ex._record_tool_outcome("terminal", False, "SyntaxError")
    assert asyncio.run(ex._network_half_open("terminal")) is False
