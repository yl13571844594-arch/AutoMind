"""Function Calling 集成 — LLM 工具调用 schema 生成与结果处理。"""

from __future__ import annotations

from typing import Any

from automind.core.types import ToolCall, ToolResult
from automind.tools.base import ToolRegistry


class FunctionCallHandler:
    """处理 LLM 返回的工具调用。

    职责:
        1. 将 ToolCall 转换为 ToolRegistry.dispatch() 调用
        2. 将 ToolResult 转换为 LLM 可读的工具消息
        3. 跟踪工具调用历史
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.call_history: list[tuple[ToolCall, ToolResult]] = []

    async def execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
    ) -> list[ToolResult]:
        """执行 LLM 请求的所有工具调用。

        Returns:
            按调用顺序的 ToolResult 列表。
        """
        results = []
        for tc in tool_calls:
            result = await self.registry.dispatch(tc.name, **tc.arguments)
            self.call_history.append((tc, result))
            results.append(result)
        return results

    def tool_results_to_messages(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """将工具调用结果转换为 LLM 消息格式。"""
        messages = []
        for tc, result in zip(tool_calls, results):
            content = result.output if result.success else f"Error: {result.error}"
            if isinstance(content, dict):
                import json
                content = json.dumps(content, indent=2, ensure_ascii=False)

            # OpenAI 格式
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(content),
            })
        return messages

    def tool_results_to_anthropic(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """将工具调用结果转换为 Anthropic 消息格式。"""
        content_blocks = []
        for tc, result in zip(tool_calls, results):
            output = result.output if result.success else f"Error: {result.error}"
            if isinstance(output, dict):
                import json
                output = json.dumps(output, indent=2, ensure_ascii=False)
            content_blocks.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": str(output),
            })
        return [{"role": "user", "content": content_blocks}]

    def get_call_summary(self) -> str:
        """生成工具调用历史摘要。"""
        lines = []
        for tc, result in self.call_history[-10:]:
            status = "OK" if result.success else "FAIL"
            lines.append(f"  [{status}] {tc.name}({self._format_args(tc.arguments)})")
        return "\n".join(lines)

    @staticmethod
    def _format_args(args: dict[str, Any]) -> str:
        parts = []
        for k, v in args.items():
            s = str(v)
            if len(s) > 60:
                s = s[:57] + "..."
            parts.append(f"{k}={s}")
        return ", ".join(parts)
