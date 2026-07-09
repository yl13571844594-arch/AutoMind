"""异步事件总线 — 模块间解耦通信。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from automind.core.types import EventType

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """轻量级异步发布/订阅事件总线。

    使用示例::

        bus = EventBus()

        @bus.on(EventType.TOOL_START)
        async def on_tool_start(payload: dict) -> None:
            print(f"Tool starting: {payload}")

        await bus.emit(EventType.TOOL_START, {"tool": "terminal"})
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []

    def on(self, event_type: EventType) -> Callable[[EventHandler], EventHandler]:
        """装饰器：注册事件处理器。"""

        def decorator(handler: EventHandler) -> EventHandler:
            self.subscribe(event_type, handler)
            return handler

        return decorator

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """注册事件处理器 (非装饰器方式)。"""
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """注册全局事件处理器 (接收所有事件)。"""
        self._global_handlers.append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """取消注册。"""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
        if handler in self._global_handlers:
            self._global_handlers.remove(handler)

    async def emit(self, event_type: EventType, payload: dict[str, Any] | None = None) -> None:
        """发送事件 — 所有匹配的处理器并发执行。"""
        payload = payload or {}
        payload.setdefault("event_type", event_type.value)

        tasks: list[asyncio.Task[None]] = []
        for handler in self._handlers.get(event_type, []):
            tasks.append(asyncio.create_task(handler(payload)))
        for handler in self._global_handlers:
            tasks.append(asyncio.create_task(handler(payload)))

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=30.0)
            for task in pending:
                task.cancel()
            # 传播异常 (不阻断其他处理器)
            for task in done:
                if task.exception():
                    # 使用 logging 而非 print
                    import logging
                    logging.getLogger("automind.events").warning(
                        "Event handler error: %s", task.exception()
                    )

    def clear(self) -> None:
        """清除所有处理器。"""
        self._handlers.clear()
        self._global_handlers.clear()
