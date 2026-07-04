"""轻量请求速率限制 — 滑动窗口计数器（§14.11-4）。

纯内存实现，无第三方依赖（不引入 slowapi/redis），适合单进程部署防滥用。
按 key（通常是客户端 IP）维护时间戳队列，超过窗口内允许次数即拒绝。

服务端按环境变量 ``AUTOMIND_RATE_LIMIT``（每分钟允许次数，0=关闭）选择性启用，
默认关闭以保持本地易用与既有行为不变。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """滑动窗口限流器。

    Args:
        max_requests: 窗口内允许的最大请求数。<= 0 表示不限流（直接放行）。
        window_seconds: 窗口长度（秒），默认 60。
    """

    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return self.max_requests > 0

    def allow(self, key: str, now: float | None = None) -> bool:
        """登记一次请求；在限额内返回 True，超限返回 False。"""
        if not self.enabled:
            return True
        now = time.monotonic() if now is None else now
        q = self._hits[key]
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= self.max_requests:
            return False
        q.append(now)
        return True

    def retry_after(self, key: str, now: float | None = None) -> float:
        """距离下一次可用还需等待的秒数（用于 Retry-After 头）。"""
        if not self.enabled:
            return 0.0
        now = time.monotonic() if now is None else now
        q = self._hits.get(key)
        if not q or len(q) < self.max_requests:
            return 0.0
        return max(0.0, round(q[0] + self.window - now, 1))

    def reset(self, key: str | None = None) -> None:
        """清空某 key（或全部）的计数 — 主要用于测试。"""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
