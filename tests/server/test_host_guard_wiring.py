"""Host 准入**接线**是否真的生效（v1.7.4）。

单元判定在 ``tests/test_http_guard.py``；这里只问一件事：
那条判定有没有真的挂在请求链路上。历史上的教训是本仓库反复出现的同一类 ——
判定写好了、测试也绿，但**从来没被调用**（预算链路、危险命令前缀放行都是如此）。

因此这里用真实 HTTP 请求打进去，而不是直接调判定函数。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)
    monkeypatch.delenv("AUTOMIND_TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("AUTOMIND_HOST", raising=False)
    return TestClient(srv.app)


def test_foreign_host_is_refused_on_an_api_endpoint(client):
    r = client.get("/api/health", headers={"host": "evil.example:8765"})

    assert r.status_code == 403, "重绑定 Host 竟然被放行到了业务端点"
    assert "AUTOMIND_TRUSTED_HOSTS" in r.json()["error"]


def test_foreign_host_is_refused_even_on_the_index(client):
    """首页也要挡：否则重绑定页面能先把**前端脚本**取回去，再由脚本调 /api。

    "只保护 /api/" 的写法在这里是漏的 —— 首页不是 /api/，但它带着 /api 的客户端。
    """
    r = client.get("/", headers={"host": "evil.example"})

    assert r.status_code == 403


def test_foreign_host_is_refused_before_the_token_check(client, monkeypatch):
    """顺序：先判 Host，再判令牌 —— 否则配了令牌的部署里，重绑定页面照样能
    用「无令牌 → 401」这条路径探明"这里跑着 AutoMind、版本多少"。"""
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "secret-token", raising=False)
    r = client.get("/api/status", headers={"host": "evil.example"})

    assert r.status_code == 403
    assert "令牌" not in r.json()["error"]


def test_localhost_and_loopback_still_work(client):
    for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765"):
        r = client.get("/api/health", headers={"host": host})
        assert r.status_code == 200, f"{host} 被误伤：{r.status_code}"


def test_trusted_hosts_env_opens_the_door(client, monkeypatch):
    monkeypatch.setenv("AUTOMIND_TRUSTED_HOSTS", "agent.corp.local")
    assert client.get("/api/health",
                      headers={"host": "agent.corp.local"}).status_code == 200
    # 只放行登记过的那个，别的照拒
    assert client.get("/api/health",
                      headers={"host": "other.corp.local"}).status_code == 403
