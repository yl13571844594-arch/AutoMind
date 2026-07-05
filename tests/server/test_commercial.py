"""商用能力测试 — 会话隔离 / 鉴权 / 并发上限 / 健康检查。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import automind.server as srv
    # 隔离配置与会话文件，避免污染真实数据
    srv._store.config_file = tmp_path / "cfg.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._store.chats_dir = tmp_path / "chats"
    srv._store.session_histories.clear()
    srv._AUTH_TOKEN = ""
    srv._agent = None
    return TestClient(srv.app), srv


class TestHealth:
    def test_health_no_auth(self, client):
        c, srv = client
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["auth_required"] is False
        assert "max_concurrent" in body


class TestSessionIsolation:
    def test_histories_are_per_session(self, client):
        c, srv = client
        srv._get_session_history("alice").append({"role": "user", "content": "hi from alice"})
        srv._save_session_history("alice")
        srv._get_session_history("bob").append({"role": "user", "content": "hi from bob"})
        srv._save_session_history("bob")

        a = c.get("/api/chat/history?session_id=alice").json()["messages"]
        b = c.get("/api/chat/history?session_id=bob").json()["messages"]
        assert a[0]["content"] == "hi from alice"
        assert b[0]["content"] == "hi from bob"
        # alice 重置不影响 bob
        c.delete("/api/chat/history?session_id=alice")
        assert c.get("/api/chat/history?session_id=alice").json()["messages"] == []
        assert c.get("/api/chat/history?session_id=bob").json()["messages"][0]["content"] == "hi from bob"

    def test_default_session_backward_compat(self, client):
        c, srv = client
        # 默认会话沿用单文件，行为不变
        assert c.get("/api/chat/history").json()["messages"] == []


class TestAuth:
    def test_blocks_without_token(self, client):
        c, srv = client
        srv._AUTH_TOKEN = "secret123"
        assert c.get("/api/status").status_code == 401
        # 健康检查不需鉴权
        assert c.get("/api/health").status_code == 200

    def test_allows_with_bearer(self, client):
        c, srv = client
        srv._AUTH_TOKEN = "secret123"
        r = c.get("/api/status", headers={"Authorization": "Bearer secret123"})
        assert r.status_code == 200

    def test_allows_with_query_token(self, client):
        c, srv = client
        srv._AUTH_TOKEN = "secret123"
        assert c.get("/api/status?token=secret123").status_code == 200
