"""会话级 Agent 池（§7.4）— 执行态多用户隔离。

问题：全局单例 `_agent` 被所有并发请求共享，`_current_plan` / ReAct 状态 /
approval_callback 会互相覆盖。本模块为每个 `session_id` 维护独立的 Agent 实例，
使并发会话的执行态互不污染。

设计要点：
    - **默认关闭**（由 `AUTOMIND_SESSION_POOL` 开启），关闭时行为与旧版完全一致，零回归；
    - 容量上限 + LRU 逐出（超出时关闭最久未用的会话 Agent）；
    - 每个会话 Agent 从基础配置克隆 LLM/权限/执行参数，独立持有 plan/ReAct 状态；
    - `acquire` 惰性创建，`aclose_all` 在关停时释放全部资源。

线程模型：FastAPI 单事件循环内串行调度，池操作无需加锁。
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any, Callable

from automind.core.logging import get_logger

logger = get_logger("automind.server.pool")


def pool_enabled() -> bool:
    """会话池是否启用（环境变量 AUTOMIND_SESSION_POOL=1/true）。"""
    return os.environ.get("AUTOMIND_SESSION_POOL", "").lower() in ("1", "true", "yes")


class SessionAgentPool:
    """按 session_id 维护独立 Agent 实例的池（容量上限 + LRU 逐出）。"""

    def __init__(self, factory: Callable[[], Any], max_agents: int = 8) -> None:
        self._factory = factory
        self.max_agents = max_agents
        self._agents: OrderedDict[str, Any] = OrderedDict()
        self._last_used: dict[str, float] = {}

    def acquire(self, session_id: str) -> Any:
        """取用（或惰性创建）某会话的 Agent；更新 LRU 顺序。"""
        sid = session_id or "default"
        if sid in self._agents:
            self._agents.move_to_end(sid)
            self._last_used[sid] = time.time()
            return self._agents[sid]

        agent = self._factory()
        self._agents[sid] = agent
        self._last_used[sid] = time.time()
        logger.info("session_agent_created", session=sid, active=len(self._agents))
        self._evict_if_needed()
        return agent

    def _evict_if_needed(self) -> None:
        """超出容量时逐出最久未用的会话 Agent（同步移除，异步资源尽力释放）。"""
        while len(self._agents) > self.max_agents:
            old_sid, old_agent = self._agents.popitem(last=False)
            self._last_used.pop(old_sid, None)
            logger.info("session_agent_evicted", session=old_sid)
            self._schedule_close(old_agent)

    @staticmethod
    def _schedule_close(agent: Any) -> None:
        import asyncio
        try:
            loop = asyncio.get_running_loop()  # 仅在运行中的事件循环里调度
        except RuntimeError:
            return  # 无运行循环（如同步测试）→ 交由 GC，不产生未 await 协程告警
        loop.create_task(_safe_close(agent))

    def release(self, session_id: str) -> None:
        """显式释放某会话的 Agent（如前端关闭会话）。"""
        sid = session_id or "default"
        agent = self._agents.pop(sid, None)
        self._last_used.pop(sid, None)
        if agent is not None:
            self._schedule_close(agent)
            logger.info("session_agent_released", session=sid)

    def active_count(self) -> int:
        return len(self._agents)

    def sessions(self) -> list[str]:
        return list(self._agents.keys())

    async def aclose_all(self) -> None:
        """关停时释放全部会话 Agent。"""
        for sid, agent in list(self._agents.items()):
            await _safe_close(agent)
        self._agents.clear()
        self._last_used.clear()


async def _safe_close(agent: Any) -> None:
    try:
        await agent.close()
    except Exception as e:
        logger.warning("session_agent_close_failed", error=str(e))
