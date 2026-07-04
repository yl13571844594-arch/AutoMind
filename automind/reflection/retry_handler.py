"""重试处理器 — 指数退避、熔断器、回退策略。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class CircuitState(str, Enum):
    CLOSED = "closed"  # 正常工作
    OPEN = "open"  # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 尝试恢复


@dataclass
class RetryConfig:
    """重试配置。"""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,)


@dataclass
class CircuitBreakerConfig:
    """熔断器配置。"""

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_requests: int = 1


@dataclass
class RetryStats:
    """重试统计。"""

    attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_retries: int = 0
    last_failure_time: float = 0.0
    last_error: str = ""


class RetryHandler:
    """重试处理器 — 指数退避 + 熔断器。

    使用示例::

        handler = RetryHandler(RetryConfig(max_retries=3))
        result = await handler.execute(my_async_func, arg1, arg2)
    """

    def __init__(
        self,
        retry_config: RetryConfig | None = None,
        circuit_config: CircuitBreakerConfig | None = None,
    ) -> None:
        self.retry_config = retry_config or RetryConfig()
        self.circuit_config = circuit_config or CircuitBreakerConfig()
        self.stats = RetryStats()
        self._circuit_state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_count = 0

    async def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """执行函数，自动重试和熔断。

        Args:
            func: 异步或同步函数。
            *args: 位置参数。
            **kwargs: 关键字参数。

        Returns:
            函数返回值。

        Raises:
            最后的异常 (如果所有重试都失败)。
        """
        # 熔断检查
        if self._circuit_state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self.circuit_config.recovery_timeout:
                self._circuit_state = CircuitState.HALF_OPEN
                self._half_open_count = 0
            else:
                raise RuntimeError("Circuit breaker is OPEN — refusing request")

        if self._circuit_state == CircuitState.HALF_OPEN:
            if self._half_open_count >= self.circuit_config.half_open_max_requests:
                raise RuntimeError("Circuit breaker HALF_OPEN — too many test requests")
            # B-06 修复：计入本次半开探测请求，否则节流永不触发、熔断形同虚设。
            self._half_open_count += 1

        last_error = None

        for attempt in range(self.retry_config.max_retries + 1):
            try:
                self.stats.attempts += 1

                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                # 成功
                self.stats.successes += 1
                self._on_success()
                return result

            except self.retry_config.retryable_exceptions as e:
                last_error = e
                self.stats.failures += 1
                self.stats.last_failure_time = time.time()
                self.stats.last_error = str(e)
                self._on_failure()

                if attempt < self.retry_config.max_retries:
                    delay = self._compute_delay(attempt)
                    await asyncio.sleep(delay)
                    self.stats.total_retries += 1

        raise last_error  # type: ignore[misc]

    def reset_circuit(self) -> None:
        """手动重置熔断器。"""
        self._circuit_state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_count = 0

    @property
    def circuit_state(self) -> CircuitState:
        return self._circuit_state

    # ── 内部方法 ──────────────────────────────────────────

    def _compute_delay(self, attempt: int) -> float:
        delay = self.retry_config.base_delay * (
            self.retry_config.exponential_base ** attempt
        )
        delay = min(delay, self.retry_config.max_delay)
        if self.retry_config.jitter:
            import random
            delay = delay * (0.5 + random.random())
        return delay

    def _on_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.circuit_config.failure_threshold:
            self._circuit_state = CircuitState.OPEN

    def _on_success(self) -> None:
        self._failure_count = 0
        if self._circuit_state == CircuitState.HALF_OPEN:
            self._circuit_state = CircuitState.CLOSED
            self._half_open_count = 0
