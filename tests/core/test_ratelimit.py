"""滑动窗口限流器测试（§14.11-4）。"""

from automind.core.ratelimit import SlidingWindowLimiter


class TestSlidingWindowLimiter:
    def test_disabled_when_zero(self):
        lim = SlidingWindowLimiter(0)
        assert not lim.enabled
        for _ in range(100):
            assert lim.allow("ip") is True

    def test_allows_up_to_limit(self):
        lim = SlidingWindowLimiter(3, window_seconds=60)
        t = 1000.0
        assert lim.allow("ip", now=t) is True
        assert lim.allow("ip", now=t) is True
        assert lim.allow("ip", now=t) is True
        assert lim.allow("ip", now=t) is False  # 第 4 次超限

    def test_window_slides(self):
        lim = SlidingWindowLimiter(2, window_seconds=10)
        assert lim.allow("ip", now=0) is True
        assert lim.allow("ip", now=1) is True
        assert lim.allow("ip", now=2) is False
        # 超过窗口后旧记录过期
        assert lim.allow("ip", now=11) is True

    def test_keys_independent(self):
        lim = SlidingWindowLimiter(1, window_seconds=60)
        assert lim.allow("a", now=0) is True
        assert lim.allow("b", now=0) is True
        assert lim.allow("a", now=0) is False

    def test_retry_after(self):
        lim = SlidingWindowLimiter(1, window_seconds=10)
        assert lim.allow("ip", now=0) is True
        assert lim.retry_after("ip", now=3) == 7.0

    def test_reset(self):
        lim = SlidingWindowLimiter(1, window_seconds=60)
        assert lim.allow("ip", now=0) is True
        assert lim.allow("ip", now=0) is False
        lim.reset("ip")
        assert lim.allow("ip", now=0) is True
