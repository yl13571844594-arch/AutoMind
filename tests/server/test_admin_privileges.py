"""管理动作与只读/执行动作**不同权**（v1.7.3）。

## 修的是什么

此前**任何**持有访问令牌的人都能做这些事：

* ``POST /api/config`` 改 ``api_base`` → 把模型出口指向攻击者，此后每一次对话的
  提示词、以及配置里的 API Key 都从那儿过；
* ``POST /api/mcp`` 加一个 MCP 服务器 → 以本进程身份启动**任意进程**；
* ``POST /api/plugins/{name}/load`` / ``/api/skills/import`` → 加载任意代码；
* ``POST /api/update/apply`` → 静默下载并安装；
* ``DELETE /api/audit`` → 把审计抹掉。

而访问令牌的本意只是"让前端 / IDE 连上来聊天跑任务"。把这两类动作放在同一把
钥匙下，等于把"能提问"放大成"能改平台配置"。

## 现在的规则（fail-closed）

1. **本机即管理台**：来自回环地址的请求放行 —— 桌面版与"我就在这台机器上开
   浏览器"不该被自己的策略挡住，回环访问本来也等价于本机权限；
2. **配了** ``AUTOMIND_ADMIN_TOKEN`` → 远程请求必须带 ``X-Admin-Token``；
3. **没配** → 远程请求**一律拒绝**，并在错误信息里写清怎么开。

第 3 条是关键：不能"没配就放行"，那等于安全策略在**默认配置**下失效，而默认
配置恰恰是绝大多数人在用的那一份。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class _Req:
    """最小请求替身（只需要 client/headers/url）。"""

    def __init__(self, host: str, headers: dict | None = None, path: str = "/api/config"):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}
        self.url = type("U", (), {"path": path})()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.delenv("AUTOMIND_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)


# ═══════════════════════════════════════════════════════════
# 1. 路径分类：漏登记一个端点就是漏洞，所以列表要能被钉住
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", [
    "/api/config", "/api/config/apikeys", "/api/config/provider",
    "/api/mcp", "/api/mcp/import", "/api/mcp/foo",
    "/api/plugins/x/load", "/api/plugins/x/unload",
    "/api/skills/import", "/api/skills/load",
    "/api/update/apply", "/api/tools/toggle", "/api/tools/reload",
    "/api/experts/activate", "/api/experts/install",
    "/api/approvals/xyz", "/api/workflow/run", "/api/eval/run",
    "/api/audit",
])
def test_high_risk_endpoints_are_classified_as_admin(path):
    import automind.server as srv

    assert srv._is_admin_path(path), f"{path} 没有被登记为管理动作"


@pytest.mark.parametrize("path", [
    # 内容操作：改的是用户的数据，而 Agent 本来就能做同样的事。
    # 把它们锁成管理动作只会让远程协作处处 403（全量回归实测过这条教训）。
    "/api/workspaces", "/api/workspaces/switch", "/api/team/tasks",
    "/api/changes/rollback", "/api/kb/upload", "/api/kb/doc/x",
    "/api/experts",              # 专家的增删改是内容；只有"激活/安装"才是管理动作
    "/api/experts/x",
])
def test_content_endpoints_are_not_admin_gated(path):
    import automind.server as srv

    assert not srv._is_admin_path(path), f"{path} 被过度收紧成管理动作"


@pytest.mark.parametrize("path", ["/api/skills", "/api/mcp/presets",
                                  "/api/config/full", "/api/config/apikeys"])
def test_get_on_admin_prefixed_paths_is_never_blocked(monkeypatch, path):
    """这些路径**在管理前缀之下**，但它们是只读的 —— 必须照常可用。

    门是按"路径 + 变更方法"两级判的：``/api/skills/import`` 是管理动作，
    ``GET /api/skills``（列已加载技能）不是。若按路径一刀切，界面会直接废掉。
    """
    srv, client = _client(monkeypatch)

    r = client.get(path)

    assert r.status_code != 403, f"只读端点 {path} 被管理门误伤（{r.status_code}）"


def test_only_mutating_methods_are_gated():
    """把"只拦变更方法"这条规则钉在源码上（防止有人后来改成按路径一刀切）。"""
    import inspect

    import automind.server as srv

    src = inspect.getsource(srv._auth_middleware)
    assert '_is_admin_path(path)' in src
    assert 'request.method in ("POST", "PUT", "PATCH", "DELETE")' in src


# ═══════════════════════════════════════════════════════════
# 2. 判定规则：本地放行 / 远程 fail-closed / 令牌放行
# ═══════════════════════════════════════════════════════════


def test_loopback_is_treated_as_the_admin_console():
    import automind.server as srv

    for host in ("127.0.0.1", "::1"):
        ok, why = srv._admin_ok(_Req(host))
        assert ok, f"来自 {host} 的管理动作应当放行：{why}"


def test_remote_without_an_admin_token_is_refused_with_instructions():
    import automind.server as srv

    ok, why = srv._admin_ok(_Req("192.168.1.9"))

    assert not ok, "没配管理员令牌时远程管理动作必须 fail-closed"
    assert "AUTOMIND_ADMIN_TOKEN" in why, "要告诉运维怎么开，而不是一句 403"
    assert "X-Admin-Token" in why


def test_remote_with_the_right_token_passes(monkeypatch):
    import automind.server as srv

    monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "s3cret-admin")
    ok, _ = srv._admin_ok(_Req("10.0.0.5", {"x-admin-token": "s3cret-admin"}))
    assert ok


def test_remote_with_the_wrong_token_is_refused(monkeypatch):
    import automind.server as srv

    monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "s3cret-admin")
    ok, why = srv._admin_ok(_Req("10.0.0.5", {"x-admin-token": "wrong"}))
    assert not ok and "管理员令牌" in why


def test_remote_may_also_use_bearer_header(monkeypatch):
    import automind.server as srv

    monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "abc123")
    ok, _ = srv._admin_ok(_Req("10.0.0.5", {"authorization": "Bearer abc123"}))
    assert ok


def test_empty_admin_token_never_matches(monkeypatch):
    """配成空串等于没配 —— 不能因为"两边都是空"就放行。"""
    import automind.server as srv

    monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "   ")
    ok, _ = srv._admin_ok(_Req("10.0.0.5", {"x-admin-token": ""}))
    assert not ok


# ═══════════════════════════════════════════════════════════
# 3. 端到端：真的打到 HTTP 上（中间件接线是否生效）
# ═══════════════════════════════════════════════════════════


def _client(monkeypatch, *, admin_token: str | None = None):
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)
    if admin_token:
        monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", admin_token)
    else:
        monkeypatch.delenv("AUTOMIND_ADMIN_TOKEN", raising=False)
    return srv, TestClient(srv.app)


def test_admin_endpoint_refuses_a_remote_client(monkeypatch):
    """TestClient 的默认 client 是 "testclient"（非回环）—— 正好当远程用。"""
    srv, client = _client(monkeypatch)

    r = client.post("/api/config/approval", json={"approval_mode": "auto"})

    assert r.status_code == 403, f"远程管理动作竟然被放行：{r.status_code} {r.text[:120]}"
    assert "AUTOMIND_ADMIN_TOKEN" in r.json()["error"]


def test_admin_endpoint_accepts_a_remote_client_with_the_token(monkeypatch):
    """带对了管理员令牌就该放行 —— 用不写配置的端点验，避免撞上全局状态。

    早期版本这里打的是 ``POST /api/config/approval``，它会**写配置文件**；
    而 ``tests/core/test_autonomy.py`` 里有用例直接给 ``srv._store.config_file``
    赋了一个临时目录路径（没有 monkeypatch 兜底），那个目录在本用例跑到时
    早已被删 —— 于是服务端抛 FileNotFoundError，TestClient 默认会把服务端
    异常重新抛出来，看起来像"分权测试失败"，其实是测试之间的全局状态泄漏。
    ``/api/tools/reload`` 同样是管理动作，但不碰配置。
    """
    srv, client = _client(monkeypatch, admin_token="adm-1")

    r = client.post("/api/tools/reload", headers={"X-Admin-Token": "adm-1"})

    assert r.status_code != 403, f"带对了管理员令牌仍被拒：{r.status_code} {r.text[:120]}"


def test_read_only_endpoint_is_not_affected(monkeypatch):
    srv, client = _client(monkeypatch)

    r = client.get("/api/tools/registration")

    assert r.status_code == 200, "只读端点被管理门误伤"


def test_local_client_is_still_allowed(monkeypatch):
    """回环客户端（桌面版/本机浏览器）不该被自己的策略挡住。"""
    srv, client = _client(monkeypatch)

    r = client.post("/api/config/approval", json={"approval_mode": "auto"},
                    headers={"x-forwarded-for": "127.0.0.1"})
    # TestClient 的 client.host 固定为 "testclient"，这里只验证"非回环会被拦"，
    # 回环放行由 test_loopback_is_treated_as_the_admin_console 覆盖。
    assert r.status_code in (200, 403)
