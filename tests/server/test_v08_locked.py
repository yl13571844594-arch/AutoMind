"""v0.8 商业端点在社区版下的 403 降级测试。"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    from automind import server
    return TestClient(server.app)


LOCKED_ENDPOINTS = [
    # (method, path, feature)
    ("GET", "/api/templates/custom", "custom_templates"),
    ("POST", "/api/templates/custom", "custom_templates"),
    ("DELETE", "/api/templates/custom/x", "custom_templates"),
    ("GET", "/api/audit/export", "audit_export"),
    ("POST", "/api/auth/login", "sso_ldap"),
    ("GET", "/api/auth/session", "sso_ldap"),
    ("POST", "/api/auth/logout", "sso_ldap"),
    ("GET", "/api/rbac", "rbac"),
    ("POST", "/api/rbac/role", "rbac"),
    ("DELETE", "/api/rbac/role/x", "rbac"),
    ("GET", "/api/rbac/check", "rbac"),
    ("GET", "/api/gateway", "model_gateway"),
    ("POST", "/api/gateway", "model_gateway"),
    ("GET", "/api/gateway/check", "model_gateway"),
]


class TestLockedEndpoints:
    @pytest.mark.parametrize("method,path,feature", LOCKED_ENDPOINTS)
    def test_community_gets_403_with_upgrade_hint(self, client, method, path, feature):
        r = client.request(method, path, json={} if method in ("POST",) else None)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
        body = r.json()
        assert body["feature"] == feature
        assert "error" in body and body["edition"] == "community"

    def test_status_reports_new_feature_flags(self, client):
        flags = client.get("/api/status").json()["features"]
        for key in ("custom_templates", "audit_export",
                    "sso_ldap", "rbac", "model_gateway"):
            assert key in flags and flags[key] is False
