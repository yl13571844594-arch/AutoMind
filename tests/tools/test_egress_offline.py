"""出站工具在离线 / 白名单模式下的行为（v1.11.14）。

覆盖：
  * ``http_request`` / ``web_fetch`` / ``web_search`` / ``browser`` 在 offline
    下**当场返回失败结果**（不是抛异常、更不是先超时再报错），且没有任何网络调用；
  * allowlist 下放行 / 拒绝、逐跳重定向校验；
  * ``allow_private`` 只影响 SSRF 私网校验，**不能**绕过出网策略；
  * ``check_url`` 在离线时不做 DNS（策略判定在 SSRF 之前）；
  * 出网审计：工具名、目标、脱敏（query 里的 token 不落盘）。

一律用 ``httpx.MockTransport`` 或 monkeypatch 的 ``need`` 隔离网络，不依赖外网。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import automind.tools.browser as browser_mod
import automind.tools.net_tools as nt
from automind.core import offline
from automind.core.offline import EgressBlocked
from automind.tools._toolkit import BlockedTarget, check_url
from automind.tools.browser import BrowserTool, WebFetchTool
from automind.tools.net_tools import HttpRequestTool, WebSearchTool

_ALLOW_HOST = "93.184.216.34"       # 公网 IP 字面量：check_url 不需要 DNS
_DENY_HOST = "93.184.216.35"

_ENV_KEYS = (
    "AUTOMIND_OFFLINE", "AUTOMIND_NETWORK_POLICY",
    "AUTOMIND_EGRESS_ALLOW", "AUTOMIND_EGRESS_ALLOW_FILE", "AUTOMIND_EGRESS_DENY",
    "AUTOMIND_EGRESS_AUDIT", "AUTOMIND_EGRESS_STRICT", "AUTOMIND_DATA_DIR",
    "AUTOMIND_PROBE_HOSTS", "AUTOMIND_NET_PROBE",
    "AUTOMIND_SEARCH_PROVIDER", "AUTOMIND_SEARCH_API_KEY", "AUTOMIND_SEARCH_ENDPOINT",
    "DEEPSEEK_API_KEY", "AUTOMIND_DEEPSEEK_API_KEY",
    "DEEPSEEK_SEARCH_BASE_URL", "AUTOMIND_DEEPSEEK_SEARCH_BASE",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """所有相关环境变量都从零开始，避免污染同一 pytest 进程里的其它用例。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "data"))
    yield


def _run(coro):
    return asyncio.run(coro)


def _forbid_httpx(monkeypatch):
    """断言"策略拒绝后不应加载 httpx"：need() 一旦被调用就判失败。"""
    def _need(module: str):
        raise AssertionError(f"策略拒绝后不应加载 {module}，更不应发起请求")
    monkeypatch.setattr(nt, "need", _need)


def _patch_httpx(monkeypatch, handler, calls: list[str] | None = None):
    """把 httpx.AsyncClient 换成挂 MockTransport 的实例（不联网）。"""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


def _audit_records(tmp_path) -> list[dict]:
    log = tmp_path / "data" / "logs" / "egress.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# ═══════════════════════════════════════════════════════════════
# 1. check_url：策略层 + SSRF 层的分工
# ═══════════════════════════════════════════════════════════════


def test_check_url_offline_blocks_before_dns(monkeypatch):
    """离线判定必须在 DNS 之前：否则"离线不发起任何连接"就是空话。"""
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")

    def _no_dns(*args, **kwargs):
        raise AssertionError("离线模式下不应做 DNS 解析")

    monkeypatch.setattr("automind.tools._toolkit.socket.getaddrinfo", _no_dns)
    with pytest.raises(EgressBlocked) as exc:
        check_url("https://api.deepseek.com/v1")
    assert exc.value.reason == "offline_policy"


def test_check_url_allowlist_does_not_disable_private_guard(monkeypatch):
    """白名单命中私网主机也不代表可以绕过 SSRF 防护（两件事）。"""
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", "127.0.0.1")
    with pytest.raises(BlockedTarget) as exc:
        check_url("http://127.0.0.1:8765/api/health")
    assert not isinstance(exc.value, EgressBlocked)      # 是 SSRF 拒绝，不是策略拒绝


def test_check_url_allow_private_does_not_bypass_allowlist(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", _ALLOW_HOST)
    with pytest.raises(EgressBlocked):
        check_url(f"https://{_DENY_HOST}/x", allow_private=True)


# ═══════════════════════════════════════════════════════════════
# 2. http_request
# ═══════════════════════════════════════════════════════════════


def test_http_request_offline_returns_failure_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    _forbid_httpx(monkeypatch)
    r = _run(HttpRequestTool().execute(
        url="https://api.deepseek.com/v1?api_key=TOPSECRET"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert r.output["policy_mode"] == "offline"
    assert "离线" in r.error
    # 审计留痕：拒绝要能追溯，且 query 里的密钥不能落盘
    records = _audit_records(tmp_path)
    assert records and records[-1]["event"] == "deny"
    assert records[-1]["tool"] == "http_request"
    raw = (tmp_path / "data" / "logs" / "egress.log").read_text(encoding="utf-8")
    assert "TOPSECRET" not in raw


def test_http_request_allowlist_denied_without_network(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", "api.deepseek.com")
    _forbid_httpx(monkeypatch)
    r = _run(HttpRequestTool().execute(url=f"https://{_DENY_HOST}/collect"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert "白名单" in r.error


def test_http_request_allowlist_allows_and_audits(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", _ALLOW_HOST)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    _patch_httpx(monkeypatch, handler)
    r = _run(HttpRequestTool().execute(
        url=f"https://{_ALLOW_HOST}/v1/chat?token=TOPSECRET"))
    assert r.success, r.error
    assert r.output["status"] == 200 and r.output["body"] == "ok"
    assert seen and _ALLOW_HOST in seen[0]
    records = _audit_records(tmp_path)
    allow = [rec for rec in records if rec["event"] == "allow"]
    assert allow and allow[-1]["tool"] == "http_request"
    assert allow[-1]["target"] == f"https://{_ALLOW_HOST}:443/v1/chat"
    assert "TOPSECRET" not in (tmp_path / "data" / "logs" / "egress.log").read_text(
        encoding="utf-8")


def test_http_request_open_mode_unchanged(monkeypatch):
    """默认 open：不设置任何新环境变量时行为与 v1.11.13 一致。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hi")

    _patch_httpx(monkeypatch, handler)
    r = _run(HttpRequestTool().execute(url=f"https://{_ALLOW_HOST}/x"))
    assert r.success and r.output["status"] == 200
    assert offline.is_open()


def test_http_request_redirect_each_hop_checked(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", _ALLOW_HOST)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": f"https://{_DENY_HOST}/evil"})

    _patch_httpx(monkeypatch, handler)
    r = _run(HttpRequestTool().execute(url=f"https://{_ALLOW_HOST}/start"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert r.output["redirect_target"] == f"https://{_DENY_HOST}:443/evil"
    assert "重定向" in r.output["reason"]
    assert len(calls) == 1            # 第二跳被拒绝，不允许发起请求


def test_http_request_ssrf_still_enforced(monkeypatch):
    """原有 SSRF 语义不能因为接出站策略而丢失。"""
    r = _run(HttpRequestTool().execute(url="http://169.254.169.254/latest/meta-data/"))
    assert r.success is False
    assert r.output.get("blocked") is True
    assert "egress_denied" not in r.output or not r.output.get("egress_denied")


# ═══════════════════════════════════════════════════════════════
# 3. web_fetch
# ═══════════════════════════════════════════════════════════════


def test_web_fetch_offline_returns_failure_without_network(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    r = _run(WebFetchTool().execute(url="https://example.com/article"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert "离线" in r.error


def test_web_fetch_ssrf_guard_added(monkeypatch):
    """WebFetchTool 此前**没有** SSRF 校验，v1.11.14 补上。"""
    r = _run(WebFetchTool().execute(url="http://127.0.0.1:8765/api/status"))
    assert r.success is False
    assert r.output.get("blocked") is True


def test_web_fetch_allowlist_allows(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", _ALLOW_HOST)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>标题</title><p>正文</p></html>")

    _patch_httpx(monkeypatch, handler)
    r = _run(WebFetchTool().execute(url=f"https://{_ALLOW_HOST}/doc"))
    assert r.success, r.error
    assert r.output["title"] == "标题"
    assert "正文" in r.output["text"]
    assert r.output["policy_mode"] == "allowlist"


def test_web_fetch_redirect_hop_denied(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", _ALLOW_HOST)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": f"https://{_DENY_HOST}/evil"})

    _patch_httpx(monkeypatch, handler)
    r = _run(WebFetchTool().execute(url=f"https://{_ALLOW_HOST}/doc"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert len(calls) == 1


# ═══════════════════════════════════════════════════════════════
# 4. web_search
# ═══════════════════════════════════════════════════════════════


def test_web_search_offline_returns_failure_without_network(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    _forbid_httpx(monkeypatch)
    r = _run(WebSearchTool().execute(query="AutoMind"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert "离线" in r.error
    assert r.output["attempts"]                    # 逐后端说明被拒绝原因


def test_web_search_allowlist_empty_denies_all(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    _forbid_httpx(monkeypatch)
    r = _run(WebSearchTool().execute(query="AutoMind"))
    assert r.success is False
    assert r.output["egress_denied"] is True
    assert "策略" in r.error or "白名单" in r.error


def test_web_search_allowlist_filters_backends(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", "api.deepseek.com")
    monkeypatch.setattr(nt, "_deepseek_search_key", lambda: "test-key")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert "api.deepseek.com" in str(request.url), "只允许请求白名单内的后端"
        return httpx.Response(200, json={"results": [
            {"title": "A", "url": "https://a.example/x", "snippet": "s"}]})

    def factory(**kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(nt, "_client_factory", factory)
    r = _run(WebSearchTool().execute(query="AutoMind"))
    assert r.success, r.error
    assert r.output["provider"] == "deepseek"
    assert r.output["policy"]["mode"] == "allowlist"
    assert seen and all("api.deepseek.com" in url for url in seen)


# ═══════════════════════════════════════════════════════════════
# 5. browser
# ═══════════════════════════════════════════════════════════════


class _FakeResponse:
    status = 200


class _FakePage:
    """足够让 ``BrowserTool._navigate`` 跑完的最小页面替身。"""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kwargs):
        self.goto_calls.append(url)
        self.url = url
        return _FakeResponse()

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def title(self) -> str:
        return "fake"


def test_browser_navigate_offline_blocked_before_goto(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    tool = BrowserTool(headless=True)
    page = _FakePage()
    r = _run(tool._navigate(page, "https://example.com/"))
    assert r.success is False
    assert "离线" in r.error
    assert page.goto_calls == []              # 关键：没有发起导航


def test_browser_navigate_intranet_allowed(monkeypatch):
    """内网 Web 系统自动化是合法场景：allowlist 下内网地址默认放行。"""
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", "api.deepseek.com")
    tool = BrowserTool(headless=True)
    page = _FakePage()
    r = _run(tool._navigate(page, "http://127.0.0.1:8765/app"))
    assert r.success, r.error
    assert page.goto_calls == ["http://127.0.0.1:8765/app"]


def test_browser_module_imports_offline_policy():
    """browser 工具必须真的引用出网策略（防止有人把它从 import 里删掉）。"""
    assert browser_mod.offline is offline
