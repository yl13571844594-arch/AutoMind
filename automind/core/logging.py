"""结构化日志 — 优先 structlog，缺失时优雅降级到标准库 logging。

统一调用约定（两种实现下签名一致）::

    logger = get_logger("automind.agent")
    logger.info("step_end", goal="创建文件", status="ok")
    logger.warning("backtrack", goal_id="g1", reason="...")

structlog 存在 → 真正的结构化输出（JSON / 彩色控制台）；
structlog 缺失 → 标准库 logging，kwargs 以 ``key=value`` 追加到消息尾部。
核心库因此不强依赖任何日志三方包（工业级可移植性）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False


def configure_logging(level: str = "INFO", debug: bool = False) -> None:
    """配置日志。structlog 可用时配置结构化管线，否则配置标准库。

    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR)。
        debug: 是否启用调试模式 (美化输出)。
    """
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    if not _HAS_STRUCTLOG:
        return

    timestamper = structlog.processors.TimeStamper(fmt="ISO")
    if debug:
        # 开发模式：彩色控制台输出
        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.CallsiteParameterAdder(
                [structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
                 structlog.processors.CallsiteParameter.FUNC_NAME],
            ),
            timestamper,
            structlog.dev.ConsoleRenderer(),
        ]
    else:
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            timestamper,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class _StdlibStructAdapter:
    """标准库 logging 适配器 — 兼容 structlog 的 ``logger.info(event, **kw)`` 签名。"""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @staticmethod
    def _fmt(event: str, kw: dict[str, Any]) -> str:
        if not kw:
            return event
        pairs = " ".join(f"{k}={v!r}" for k, v in kw.items())
        return f"{event} {pairs}"

    def debug(self, event: str, **kw: Any) -> None:
        self._logger.debug(self._fmt(event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self._logger.info(self._fmt(event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self._logger.warning(self._fmt(event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self._logger.error(self._fmt(event, kw))

    def exception(self, event: str, **kw: Any) -> None:
        self._logger.exception(self._fmt(event, kw))

    def bind(self, **kw: Any) -> "_StdlibStructAdapter":
        """structlog 兼容占位 — 标准库模式下忽略绑定上下文。"""
        return self


def get_logger(name: str | None = None) -> Any:
    """获取日志记录器（structlog 或标准库适配器，调用签名一致）。"""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name or "automind")
    return _StdlibStructAdapter(logging.getLogger(name or "automind"))
