"""会话级 Agent 池测试（§7.4 执行态多用户隔离）。"""

import asyncio

from automind.server_pool import SessionAgentPool, pool_enabled


class _FakeAgent:
    def __init__(self, n):
        self.n = n
        self.closed = False
        self._current_plan = None  # 执行态：应逐会话隔离

    async def close(self):
        self.closed = True


def _factory():
    _factory.count += 1
    return _FakeAgent(_factory.count)
_factory.count = 0


class TestPool:
    def setup_method(self):
        _factory.count = 0

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("AUTOMIND_SESSION_POOL", raising=False)
        assert pool_enabled() is False

    def test_enabled_by_env(self, monkeypatch):
        monkeypatch.setenv("AUTOMIND_SESSION_POOL", "1")
        assert pool_enabled() is True

    def test_same_session_reuses_agent(self):
        pool = SessionAgentPool(_factory, max_agents=4)
        a1 = pool.acquire("s1")
        a2 = pool.acquire("s1")
        assert a1 is a2
        assert _factory.count == 1

    def test_different_sessions_isolated(self):
        pool = SessionAgentPool(_factory, max_agents=4)
        a = pool.acquire("alice")
        b = pool.acquire("bob")
        assert a is not b
        # 执行态互不污染
        a._current_plan = "alice-plan"
        assert b._current_plan is None

    def test_lru_eviction(self):
        pool = SessionAgentPool(_factory, max_agents=2)
        a = pool.acquire("s1")
        pool.acquire("s2")
        pool.acquire("s1")   # s1 变为最近使用
        pool.acquire("s3")   # 超容量 → 逐出最久未用的 s2
        assert set(pool.sessions()) == {"s1", "s3"}
        assert a is pool.acquire("s1")  # s1 仍在

    def test_release(self):
        pool = SessionAgentPool(_factory, max_agents=4)
        pool.acquire("s1")
        assert pool.active_count() == 1
        pool.release("s1")
        assert pool.active_count() == 0

    def test_aclose_all(self):
        pool = SessionAgentPool(_factory, max_agents=4)
        a = pool.acquire("s1")
        b = pool.acquire("s2")
        asyncio.run(pool.aclose_all())
        assert a.closed and b.closed
        assert pool.active_count() == 0


class TestServerIntegration:
    def test_pool_off_returns_global_agent(self, monkeypatch):
        monkeypatch.delenv("AUTOMIND_SESSION_POOL", raising=False)
        import automind.server as srv

        class _Base:
            _interaction = _mode = None
            approval_callback = None
            event_sink = None
        base = _Base()
        assert srv._acquire_run_agent(base, "any") is base  # 默认零改动
