<<<<<<< HEAD
"""服务端安全加固集成测试 — 限流 / 脱敏 / WS 源校验（§14.11）。"""

import pytest
from fastapi.testclient import TestClient

from automind.core.ratelimit import SlidingWindowLimiter


@pytest.fixture
def srv_mod(tmp_path):
    import automind.server as srv
    srv._store.config_file = tmp_path / "cfg.json"
    srv._AUTH_TOKEN = ""
    srv._agent = None
    # 还原全局开关，避免测试间串扰
    srv._rate_limiter = SlidingWindowLimiter(0)
    srv._REDACT_SECRETS = False
    srv._WS_ALLOWED_ORIGINS = set()
    srv._task_history.clear()
    return srv


class TestRateLimit:
    def test_disabled_by_default(self, srv_mod):
        c = TestClient(srv_mod.app)
        # 默认关闭：连续多次空任务都走到 handler（400），不会出现 429
        for _ in range(5):
            r = c.post("/api/run", json={})
            assert r.status_code == 400

    def test_429_when_exceeded(self, srv_mod):
        srv_mod._rate_limiter = SlidingWindowLimiter(1, window_seconds=60)
        c = TestClient(srv_mod.app)
        first = c.post("/api/run", json={})
        assert first.status_code == 400  # 中间件放行，handler 因空任务返回 400
        second = c.post("/api/run", json={})
        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_only_affects_run(self, srv_mod):
        srv_mod._rate_limiter = SlidingWindowLimiter(1, window_seconds=60)
        c = TestClient(srv_mod.app)
        # 健康检查不受限流影响
        for _ in range(5):
            assert c.get("/api/health").status_code == 200


class TestRedactionWiring:
    def test_push_history_redacts_when_enabled(self, srv_mod):
        srv_mod._REDACT_SECRETS = True
        rec = {"output": "leaked sk-proj-ABCDEF1234567890abcdefGHIJ end"}
        srv_mod._push_history(rec)
        assert "sk-proj-ABCDEF1234567890abcdefGHIJ" not in rec["output"]
        assert srv_mod._task_history[-1] is rec

    def test_push_history_noop_when_disabled(self, srv_mod):
        srv_mod._REDACT_SECRETS = False
        original = "leaked sk-proj-ABCDEF1234567890abcdefGHIJ end"
        rec = {"output": original}
        srv_mod._push_history(rec)
        assert rec["output"] == original
=======
"""服务端安全加固集成测试 — 限流 / 脱敏 / WS 源校验（§14.11）。"""

import pytest
from fastapi.testclient import TestClient

from automind.core.ratelimit import SlidingWindowLimiter


@pytest.fixture
def srv_mod(tmp_path):
    import automind.server as srv
    srv._store.config_file = tmp_path / "cfg.json"
    srv._AUTH_TOKEN = ""
    srv._agent = None
    # 还原全局开关，避免测试间串扰
    srv._rate_limiter = SlidingWindowLimiter(0)
    srv._REDACT_SECRETS = False
    srv._WS_ALLOWED_ORIGINS = set()
    srv._task_history.clear()
    return srv


class TestRateLimit:
    def test_disabled_by_default(self, srv_mod):
        c = TestClient(srv_mod.app)
        # 默认关闭：连续多次空任务都走到 handler（400），不会出现 429
        for _ in range(5):
            r = c.post("/api/run", json={})
            assert r.status_code == 400

    def test_429_when_exceeded(self, srv_mod):
        srv_mod._rate_limiter = SlidingWindowLimiter(1, window_seconds=60)
        c = TestClient(srv_mod.app)
        first = c.post("/api/run", json={})
        assert first.status_code == 400  # 中间件放行，handler 因空任务返回 400
        second = c.post("/api/run", json={})
        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_only_affects_run(self, srv_mod):
        srv_mod._rate_limiter = SlidingWindowLimiter(1, window_seconds=60)
        c = TestClient(srv_mod.app)
        # 健康检查不受限流影响
        for _ in range(5):
            assert c.get("/api/health").status_code == 200


class TestRedactionWiring:
    def test_push_history_redacts_when_enabled(self, srv_mod):
        srv_mod._REDACT_SECRETS = True
        rec = {"output": "leaked sk-proj-ABCDEF1234567890abcdefGHIJ end"}
        srv_mod._push_history(rec)
        assert "sk-proj-ABCDEF1234567890abcdefGHIJ" not in rec["output"]
        assert srv_mod._task_history[-1] is rec

    def test_push_history_noop_when_disabled(self, srv_mod):
        srv_mod._REDACT_SECRETS = False
        original = "leaked sk-proj-ABCDEF1234567890abcdefGHIJ end"
        rec = {"output": original}
        srv_mod._push_history(rec)
        assert rec["output"] == original
>>>>>>> f7b98f9b6ecabf8d800f9c0521948f7f5db79dbc
