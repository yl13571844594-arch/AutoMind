"""重试与熔断 —— 失败路径的安全带，出问题时最需要它靠得住。

熔断器坏掉的表现是"看起来一切正常"：要么该拦的没拦（下游已经挂了还在猛打），
要么不该拦的拦了（偶发抖动后再也不恢复）。两种都不会报错，只能靠断言钉住。
"""

from __future__ import annotations

import time

import pytest

from automind.reflection.retry_handler import (
    CircuitBreakerConfig,
    CircuitState,
    RetryConfig,
    RetryHandler,
)

# 关掉退避等待，测试才不会真的睡几十秒
FAST = RetryConfig(max_retries=3, base_delay=0, max_delay=0, jitter=False)


class TestRetry:
    async def test_succeeds_without_retrying(self):
        calls = 0

        async def f():
            nonlocal calls
            calls += 1
            return "ok"

        h = RetryHandler(FAST)
        assert await h.execute(f) == "ok"
        assert calls == 1
        assert h.stats.successes == 1 and h.stats.total_retries == 0

    async def test_retries_until_success(self):
        calls = 0

        async def f():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ValueError("暂时失败")
            return "ok"

        h = RetryHandler(FAST)
        assert await h.execute(f) == "ok"
        assert calls == 3
        assert h.stats.total_retries == 2

    async def test_raises_last_error_after_exhausting_retries(self):
        async def f():
            raise ValueError("一直失败")

        h = RetryHandler(FAST)
        with pytest.raises(ValueError, match="一直失败"):
            await h.execute(f)
        # max_retries=3 → 首次 + 3 次重试 = 4 次尝试
        assert h.stats.attempts == 4

    async def test_sync_functions_are_supported(self):
        """execute 声明支持同步函数，别只在异步上验证。"""
        def f(a, b=0):
            return a + b

        assert await RetryHandler(FAST).execute(f, 1, b=2) == 3

    async def test_non_retryable_exception_propagates_immediately(self):
        """不在白名单里的异常应直接抛出，不浪费重试次数。"""
        cfg = RetryConfig(max_retries=3, base_delay=0, jitter=False,
                          retryable_exceptions=(ValueError,))
        calls = 0

        async def f():
            nonlocal calls
            calls += 1
            raise KeyError("不该被重试")

        h = RetryHandler(cfg)
        with pytest.raises(KeyError):
            await h.execute(f)
        assert calls == 1, "非可重试异常却重试了"


class TestBackoff:
    def test_delay_grows_exponentially(self):
        h = RetryHandler(RetryConfig(base_delay=1, exponential_base=2,
                                     max_delay=100, jitter=False))
        assert [h._compute_delay(i) for i in range(4)] == [1, 2, 4, 8]

    def test_delay_is_capped(self):
        """没有上限的话，第 10 次重试要等十几分钟。"""
        h = RetryHandler(RetryConfig(base_delay=1, exponential_base=2,
                                     max_delay=5, jitter=False))
        assert h._compute_delay(10) == 5

    def test_jitter_stays_within_bounds(self):
        """抖动是为了避免惊群，但不能抖出负数或超过上限。"""
        h = RetryHandler(RetryConfig(base_delay=2, exponential_base=2,
                                     max_delay=10, jitter=True))
        for i in range(6):
            for _ in range(20):
                d = h._compute_delay(i)
                assert 0 <= d <= 10, f"退避时长越界: {d}"


class TestCircuitBreaker:
    def _handler(self, threshold=2, recovery=30.0, half_open=1):
        return RetryHandler(
            RetryConfig(max_retries=0, base_delay=0, jitter=False),
            CircuitBreakerConfig(failure_threshold=threshold,
                                 recovery_timeout=recovery,
                                 half_open_max_requests=half_open))

    async def test_opens_after_threshold_failures(self):
        h = self._handler(threshold=2)

        async def f():
            raise ValueError("挂了")

        for _ in range(2):
            with pytest.raises(ValueError):
                await h.execute(f)
        assert h.circuit_state == CircuitState.OPEN

        # 熔断后应直接拒绝，不再打下游
        called = False

        async def g():
            nonlocal called
            called = True
            return "ok"

        with pytest.raises(RuntimeError, match="OPEN"):
            await h.execute(g)
        assert called is False, "熔断打开后仍然放行了请求"

    async def test_half_open_after_recovery_timeout(self):
        h = self._handler(threshold=1, recovery=0.05)

        async def bad():
            raise ValueError("挂了")

        with pytest.raises(ValueError):
            await h.execute(bad)
        assert h.circuit_state == CircuitState.OPEN

        time.sleep(0.2)           # 等过恢复窗口（余量放宽，CI 负载高时不至于 flaky）

        async def good():
            return "ok"

        assert await h.execute(good) == "ok"
        assert h.circuit_state == CircuitState.CLOSED, "探测成功后应恢复"

    async def test_half_open_throttles_probe_requests(self):
        """B-06 回归：半开态必须限流，否则熔断形同虚设。"""
        h = self._handler(threshold=1, recovery=0.05, half_open=1)

        async def bad():
            raise ValueError("挂了")

        with pytest.raises(ValueError):
            await h.execute(bad)
        time.sleep(0.2)

        # 第一个探测请求放行（这里让它继续失败，保持半开/重新打开）
        with pytest.raises(ValueError):
            await h.execute(bad)
        # 紧接着的第二个请求必须被挡住，而不是一起涌向已经挂掉的下游
        with pytest.raises(RuntimeError):
            await h.execute(bad)

    async def test_manual_reset(self):
        h = self._handler(threshold=1)

        async def bad():
            raise ValueError("挂了")

        with pytest.raises(ValueError):
            await h.execute(bad)
        assert h.circuit_state == CircuitState.OPEN
        h.reset_circuit()
        assert h.circuit_state == CircuitState.CLOSED

        async def good():
            return "ok"

        assert await h.execute(good) == "ok"

    async def test_success_resets_failure_count(self):
        """偶发失败不该累积到熔断 —— 中间成功过就该清零。"""
        h = self._handler(threshold=3)

        async def bad():
            raise ValueError("挂了")

        async def good():
            return "ok"

        with pytest.raises(ValueError):
            await h.execute(bad)
        await h.execute(good)
        with pytest.raises(ValueError):
            await h.execute(bad)
        with pytest.raises(ValueError):
            await h.execute(bad)
        assert h.circuit_state == CircuitState.CLOSED, \
            "中间成功过，失败计数应已清零，不该熔断"
