"""工具系统基类 — AbstractTool, ToolRegistry, 工具模式生成。"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult, ToolSource

_logger = get_logger("automind.tools")


class AbstractTool(ABC):
    """所有工具的抽象基类。

    子类需要实现:
        - execute(**kwargs) → ToolResult

    可选覆盖:
        - dry_run_possible() → bool
        - get_execution_plan(**kwargs) → str
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    permission_tier: PermissionTier = PermissionTier.SAFE
    risk_score: int = 0  # 0-100
    source: ToolSource = ToolSource.BUILTIN

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult: ...

    def dry_run_possible(self) -> bool:
        """是否支持干运行 (预览而不执行)。"""
        return False

    def get_execution_plan(self, **kwargs: Any) -> str:
        """生成人类可读的执行计划预览。"""
        params_str = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        return f"[{self.name}] {self.description}\n  Parameters: {params_str}"

    def to_openai_schema(self) -> dict[str, Any]:
        """生成 OpenAI Function Calling 格式的 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", []),
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        """生成 Anthropic tool use 格式的 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", []),
            },
        }


class ToolRegistry:
    """工具注册中心 — 管理所有可用的工具。

    使用示例::

        registry = ToolRegistry()
        registry.register(MyTool())
        result = await registry.dispatch("my_tool", arg1="val1")
    """

    def __init__(self) -> None:
        self._tools: dict[str, AbstractTool] = {}

    def register(self, tool: AbstractTool) -> None:
        """注册工具。"""
        if not tool.name:
            raise ValueError("Tool must have a name")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> AbstractTool:
        """获取工具。"""
        if name not in self._tools:
            from automind.core.exceptions import ToolNotFoundError
            raise ToolNotFoundError(f"Tool '{name}' not found. Available: {self.list_names()}")
        return self._tools[name]

    def list_names(self) -> list[str]:
        """返回所有工具名称。"""
        return sorted(self._tools.keys())

    def list_all(self) -> list[AbstractTool]:
        """返回所有工具实例。"""
        return list(self._tools.values())

    def list_by_tier(self, tier: PermissionTier) -> list[AbstractTool]:
        """按权限等级筛选工具。"""
        return [t for t in self._tools.values() if t.permission_tier == tier]

    def get_openai_schemas(self) -> list[dict[str, Any]]:
        """生成 OpenAI 格式的所有工具 schema。"""
        return [t.to_openai_schema() for t in self._tools.values()]

    def get_anthropic_schemas(self) -> list[dict[str, Any]]:
        """生成 Anthropic 格式的所有工具 schema。"""
        return [t.to_anthropic_schema() for t in self._tools.values()]

    async def dispatch(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """分派工具调用。

        Args:
            tool_name: 工具名称。
            **kwargs: 工具参数。

        Returns:
            ToolResult 实例。
        """
        tool = self.get(tool_name)
        start = time.perf_counter()
        try:
            result = await tool.execute(**kwargs)
        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            _logger.error("tool_dispatch_error", tool=tool_name,
                          error=f"{type(e).__name__}: {e}", duration_ms=round(duration, 1))
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
                duration_ms=duration,
            )
        result.duration_ms = (time.perf_counter() - start) * 1000
        result.tool_name = tool_name
        _logger.info("tool_dispatch", tool=tool_name, success=result.success,
                     duration_ms=round(result.duration_ms, 1))
        return result

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self._tools)} tools: {', '.join(self.list_names())})"
