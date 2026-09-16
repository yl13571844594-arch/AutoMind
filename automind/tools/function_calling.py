"""Function Calling 集成 — LLM 工具调用 schema 生成与结果处理。"""

from __future__ import annotations

import json
from typing import Any

from automind.core.types import ToolCall, ToolResult
from automind.tools.base import ToolRegistry
from automind.tools.output_budget import limited_tool_content, limits_from_config


def _call_key(tc: ToolCall) -> str:
    """调用指纹（工具名 + 规范化参数）—— 用于跨轮结果去重。"""
    try:
        blob = json.dumps(tc.arguments or {}, sort_keys=True,
                          ensure_ascii=False, default=str)
    except Exception:
        try:
            blob = repr(sorted((tc.arguments or {}).items()))
        except Exception:
            blob = "<?>"
    return f"{tc.name}::{blob}"


class FunctionCallHandler:
    """处理 LLM 返回的工具调用。

    职责:
        1. 将 ToolCall 转换为 ToolRegistry.dispatch() 调用
        2. 将 ToolResult 转换为 LLM 可读的工具消息
        3. 跟踪工具调用历史

    v1.6.4：结果转消息时**先按体积夹住**（见 ``tools/output_budget.py``）。
    此前这里 ``str(content)`` 原样进上下文，一次大文件读取/长命令输出就会在
    下一轮整体重发，是 token 成本与上下文超载的最大单一来源。

    v1.7.0：**同参调用的结果不再原样重发**。文件内容/网页正文这类大块文本，
    只要模型用完全相同的参数又调了一次（重试、绕回、忘了已经读过），
    第二次起只留一条几十字的"同上"引用 —— 此前唯一的防线是体积截断与
    旧消息折叠，而折叠要等到预算用到 80% 才触发。
    """

    def __init__(self, registry: ToolRegistry, output_limits: dict[str, int] | None = None) -> None:
        self.registry = registry
        self.call_history: list[tuple[ToolCall, ToolResult]] = []
        #: 工具输出体积限额（None = 按 ExecutionConfig 默认值）
        self.output_limits = output_limits or limits_from_config()
        #: 累积的截断账目（供观测中心展示"这次任务省下多少上下文"）
        self.truncations: list[dict[str, Any]] = []
        self.dropped_chars = 0
        #: 调用指纹 → 上一次渲染出的内容（跨轮去重用）
        self._rendered: dict[str, str] = {}
        #: 去重账目：复用次数 / 因此省下的字符数
        self.deduped_results = 0
        self.deduped_chars = 0

    # ── 结果 → 消息 ─────────────────────────────────────────

    def _content_of(self, tc: ToolCall, result: ToolResult) -> str:
        """单条结果的最终文本（含体积夹取、跨轮去重与截断账目）。"""
        raw = result.output if result.success else f"Error: {result.error}"
        text, stat = limited_tool_content(raw, self.output_limits, tool=tc.name)
        if stat.get("truncated"):
            self.truncations.append(stat)
            self.dropped_chars += int(stat.get("dropped_chars", 0))
        return self._dedupe(tc, text)

    def _dedupe(self, tc: ToolCall, text: str) -> str:
        """完全相同的调用 → 不再重发正文，只留一条引用说明。

        安全性：指纹包含**规范化后的全部参数**，参数不同即不复用；
        被替换掉的只是"同一份内容再贴一遍"，原始结果仍可从那个更早的
        tool 消息读到（模型也被告知了这一点）。
        """
        key = _call_key(tc)
        previous = self._rendered.get(key)
        if previous is None:
            self._rendered[key] = text
            return text
        if previous == text:
            note = (
                "（本次调用与之前**完全相同**，结果已在上面给出过，"
                "此处不再重复相同内容以节省上下文。"
                "若你需要的是最新状态或更完整的内容，请改变参数"
                "——例如换 offset/limit 分段读取、换路径、加过滤条件。）")
            self.deduped_results += 1
            return note
        # 参数相同但结果变了（文件被改过、命令输出不同）—— 视为新信息，照常下发。
        self._rendered[key] = text
        return (f"（注意：与之前同样的调用，但结果**已经变化**，"
                f"以下是本次的最新结果。）\n{text}")

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
            # v1.7.0：跨轮去重账目（"这条重复结果没再进上下文"）
            "deduped_results": self.deduped_results,
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
