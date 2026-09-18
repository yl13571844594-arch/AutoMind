"""Host 头准入 —— DNS 重绑定防护（v1.7.4）。

## 修的是什么

服务默认绑回环，此前据"客户端 IP 是回环"直接认定"这是本机用户"，并把管理
动作、目录浏览、令牌端点都挂在这个判断上。**DNS 重绑定**能伪造这个前提：
攻击者域名先解析到自己的服务器（页面正常跑起来），TTL 归零后改成
``127.0.0.1``；浏览器再请求时 TCP 连的是本机、``Host`` 却仍是攻击者域名。
于是"回环"成立，而请求其实是外部页面发出的。

判据换成 Host 之后，浏览器**无法**伪造它：Host 是它自己按 URL 填的，
重绑定场景下必然是攻击者那个域名。

## 这里钉住的边界

* 回环的几种写法（含端口、IPv6 方括号、尾点、127.0.0.0/8）必须放行 ——
  否则用户装完第一件事就是被自己的服务 403；
* 外部域名必须拒，且拒绝信息要写清怎么放行（逃生门）；
* ``AUTOMIND_TRUSTED_HOSTS`` 是那个逃生门（反代 / 内网域名 / 隧道）；
* 绑到全网卡时不做 Host 判定 —— 那种部署 Host 只能是外部名字，
  按 Host 拦会把正常用法全拦掉，准入交给 auth_token。
"""

from __future__ import annotations

import pytest

from automind.core import http_guard


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AUTOMIND_TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("AUTOMIND_HOST", raising=False)


# ═══════════════════════════════════════════════════════════
# 1. 解析：端口 / 方括号 / 尾点 / userinfo
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("netloc,expect", [
    ("127.0.0.1:8765", "127.0.0.1"),
    ("localhost", "localhost"),
    ("LocalHost:80", "localhost"),
    ("localhost.", "localhost"),
    ("[::1]:8765", "::1"),
    ("[::1]", "::1"),
    ("evil.example:8765", "evil.example"),
    ("", ""),
])
def test_split_host_port(netloc, expect):
    assert http_guard.split_host_port(netloc) == expect


def test_userinfo_is_stripped_not_trusted():
    """``http://127.0.0.1@evil.com/`` —— 真正的主机是 evil.com。

    前缀匹配式的判定在这里会放行（字符串确实以 127.0.0.1 开头），
    必须先剥掉 ``user@`` 再比。
    """
    assert http_guard.split_host_port("127.0.0.1@evil.com") == "evil.com"


# ═══════════════════════════════════════════════════════════
# 2. 放行：本机用户不该被自己的策略挡住
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765",
    "LOCALHOST", "localhost.", "[::1]", "[::1]:8765",
    # 127.0.0.0/8 整个网段都是本机：浏览器用 .1，代理/容器可能用别的
    "127.0.0.5:8765", "127.1.2.3",
    # TestClient 的占位 Host（不可达地址，见 http_guard 里的说明）
    "testserver",
])
def test_loopback_hosts_are_allowed(host):
    ok, why = http_guard.host_allowed(host)
    assert ok, f"{host} 不该被拦：{why}"


def test_missing_host_falls_back_to_other_judgements():
    """HTTP/1.0 客户端可能不带 Host —— 没有判据时不在这里下结论。"""
    ok, _ = http_guard.host_allowed("")
    assert ok


# ═══════════════════════════════════════════════════════════
# 3. 拒绝：重绑定的特征
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:8765", "attacker.test",
    "127.0.0.1.evil.example",      # 前缀伪装
    "localhost.evil.example",      # 前缀伪装（后缀不同）
    "xn--127-0-0-1.evil.example",  # 同形字
])
def test_foreign_hosts_are_refused(host):
    ok, why = http_guard.host_allowed(host)
    assert not ok, f"{host} 是外部域名，必须拒（DNS 重绑定特征）"
    # 拒绝信息必须给出出路，不能只丢一个 403
    assert "AUTOMIND_TRUSTED_HOSTS" in why
    assert "127.0.0.1" in why


# ═══════════════════════════════════════════════════════════
# 4. 逃生门：反代 / 内网域名 / 隧道
# ═══════════════════════════════════════════════════════════


def test_trusted_hosts_exact_match(monkeypatch):
    monkeypatch.setenv("AUTOMIND_TRUSTED_HOSTS", "agent.corp.local, box.lan:8765")
    assert http_guard.host_allowed("agent.corp.local")[0]
    assert http_guard.host_allowed("agent.corp.local:8765")[0]
    assert http_guard.host_allowed("box.lan")[0]
    assert not http_guard.host_allowed("other.lan")[0]


def test_trusted_hosts_wildcard(monkeypatch):
    monkeypatch.setenv("AUTOMIND_TRUSTED_HOSTS", "*.example.com")
    assert http_guard.host_allowed("agent.example.com")[0]
    assert http_guard.host_allowed("a.b.example.com")[0]
    assert http_guard.host_allowed("example.com")[0], "通配应含裸域"
    assert not http_guard.host_allowed("example.com.evil.test")[0]


def test_trusted_hosts_star_disables_the_guard(monkeypatch):
    monkeypatch.setenv("AUTOMIND_TRUSTED_HOSTS", "*")
    assert http_guard.host_allowed("anything.example")[0]


def test_trusted_hosts_is_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("AUTOMIND_TRUSTED_HOSTS", "  a.lan ,, b.lan  ")
    assert http_guard.host_allowed("a.lan")[0]
    assert http_guard.host_allowed("b.lan")[0]


# ═══════════════════════════════════════════════════════════
# 5. 绑全网卡时不做 Host 判定（否则正常部署全被拦）
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "*", ""])
def test_wildcard_bind_disables_host_check(monkeypatch, bind):
    monkeypatch.setenv("AUTOMIND_HOST", bind)
    if bind == "":
        # 空值 = 未设置 = 默认回环，此时判定仍然生效
        assert http_guard.binds_to_loopback()
        assert not http_guard.host_allowed("evil.example")[0]
        return
    assert not http_guard.binds_to_loopback()
    assert http_guard.host_allowed("evil.example")[0], \
        "绑全网卡是显式选择，Host 只能是外部名字，此时由 auth_token 负责准入"


def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("AUTOMIND_HOST", raising=False)
    assert http_guard.bind_host() == "127.0.0.1"
    assert http_guard.binds_to_loopback()
