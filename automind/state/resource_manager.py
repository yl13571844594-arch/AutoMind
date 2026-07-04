"""资源管理 — Token 计数、速率限制、超时控制。"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from automind.core.types import LLMResponse, TokenUsage


class TokenCounter:
    """Token 消耗追踪器。"""

    def __init__(self, budget: int = 200000) -> None:
        self.budget = budget
        self.tokens_used = TokenUsage()

    def track(self, response: LLMResponse) -> None:
        """追踪一次 LLM 调用的消耗。"""
        self.tokens_used.prompt_tokens += response.prompt_tokens
        self.tokens_used.completion_tokens += response.completion_tokens

    def remaining(self) -> int:
        """剩余 token 预算。"""
        return max(0, self.budget - self.tokens_used.total)

    def is_exhausted(self) -> bool:
        """预算是否用完。"""
        return self.tokens_used.total >= self.budget

    def usage_fraction(self) -> float:
        """已使用比例 (0-1)。"""
        return min(1.0, self.tokens_used.total / max(1, self.budget))


class TokenBucketRateLimiter:
    """Token 桶速率限制器。"""

    def __init__(self, rate: float = 10.0, burst: float = 20.0) -> None:
        self.rate = rate  # 每秒填充的 token 数
        self.burst = burst  # 最大桶容量
        self._tokens = burst
        self._last_refill = time.monotonic()
        # B-11 修复：加锁保护临界区，避免并发下 _tokens 变为负数（超额放行）。
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> bool:
        """尝试获取 token。

        Returns:
            True 如果成功获取。
        """
        # 在锁内检查/扣减；不足时在锁外 sleep 后重试，
        # 保证令牌永不为负，且不阻塞其他协程的检查。
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                wait_time = (tokens - self._tokens) / self.rate
            await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now


class TimeoutGuard:
    """超时控制上下文管理器。"""

    @asynccontextmanager
    async def timeout(self, seconds: float) -> AsyncIterator[None]:
        """异步超时上下文。

        使用示例::

            guard = TimeoutGuard()
            async with guard.timeout(30.0):
                result = await long_running_task()
        """
        try:
            async with asyncio.timeout(seconds):
                yield
        except asyncio.TimeoutError:
            raise TimeoutError(f"Operation timed out after {seconds}s")


class ResourceManager:
    """统一资源管理器 — Token + 速率 + 超时。"""

    def __init__(
        self,
        token_budget: int = 200000,
        rate_limit: float = 10.0,
        default_timeout: float = 120.0,
    ) -> None:
        self.tokens = TokenCounter(budget=token_budget)
        self.rate_limiter = TokenBucketRateLimiter(rate=rate_limit)
        self.timeout_guard = TimeoutGuard()
        self.default_timeout = default_timeout
        self._start_time = time.monotonic()

    async def before_llm_call(self) -> None:
        """LLM 调用前的检查 — 速率限制 + Token 预算。"""
        await self.rate_limiter.acquire()
        if self.tokens.is_exhausted():
            raise RuntimeError(
                f"Token budget exhausted: {self.tokens.tokens_used.total}/{self.tokens.budget}"
            )

    def after_llm_call(self, response: LLMResponse) -> None:
        """LLM 调用后的记录。"""
        self.tokens.track(response)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def get_stats(self) -> dict:
        return {
            "tokens_used": self.tokens.tokens_used.total,
            "token_budget": self.tokens.budget,
            "token_usage_pct": round(self.tokens.usage_fraction() * 100, 1),
            "elapsed_seconds": round(self.elapsed, 1),
        }
