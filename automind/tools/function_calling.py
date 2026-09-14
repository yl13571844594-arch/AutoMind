"""Function Calling 集成 — LLM 工具调用 schema 生成与结果处理。"""

from __future__ import annotations

from typing import Any

from automind.core.types import ToolCall, ToolResult
from automind.tools.base import ToolRegistry
from automind.tools.output_budget import limited_tool_content, limits_from_config


class FunctionCallHandler:
    """处理 LLM 返回的工具调用。

    职责:
        1. 将 ToolCall 转换为 ToolRegistry.dispatch() 调用
        2. 将 ToolResult 转换为 LLM 可读的工具消息
        3. 跟踪工具调用历史

    v1.6.4：结果转消息时**先按体积夹住**（见 ``tools/output_budget.py``）。
    此前这里 ``str(content)`` 原样进上下文，一次大文件读取/长命令输出就会在
    下一轮整体重发，是 token 成本与上下文超载的最大单一来源。
    """

    def __init__(self, registry: ToolRegistry, output_limits: dict[str, int] | None = None) -> None:
        self.registry = registry
        self.call_history: list[tuple[ToolCall, ToolResult]] = []
        #: 工具输出体积限额（None = 按 ExecutionConfig 默认值）
        self.output_limits = output_limits or limits_from_config()
        #: 累积的截断账目（供观测中心展示"这次任务省下多少上下文"）
        self.truncations: list[dict[str, Any]] = []
        self.dropped_chars = 0

    # ── 结果 → 消息 ─────────────────────────────────────────

    def _content_of(self, tc: ToolCall, result: ToolResult) -> str:
        """单条结果的最终文本（含体积夹取与截断账目）。"""
        raw = result.output if result.success else f"Error: {result.error}"
        text, stat = limited_tool_content(raw, self.output_limits, tool=tc.name)
        if stat.get("truncated"):
            self.truncations.append(stat)
            self.dropped_chars += int(stat.get("dropped_chars", 0))
        return text

    def tool_results_to_messages(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """将工具调用结果转换为 LLM 消息格式（自动限制单条体积）。"""
        messages = []
        for tc, result in zip(tool_calls, results):
            # OpenAI 格式
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": self._content_of(tc, result),
            })
        return messages

    def tool_results_to_anthropic(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """将工具调用结果转换为 Anthropic 消息格式（自动限制单条体积）。"""
        content_blocks = []
        for tc, result in zip(tool_calls, results):
            content_blocks.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": self._content_of(tc, result),
            })
        return [{"role": "user", "content": content_blocks}]

    def savings_report(self) -> dict[str, Any]:
        """体积治理账目（推入观测中心用）。"""
        return {
            "truncated_results": len(self.truncations),
            "dropped_chars": self.dropped_chars,
            "est_tokens_saved": int(self.dropped_chars / 3.5),
            "max_chars": int(self.output_limits.get("max_chars", 0)),
        }

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
