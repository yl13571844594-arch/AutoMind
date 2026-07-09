"""上下文管理器 — 滑动窗口、摘要压缩、任务状态栈。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from automind.core.types import Message, Role


class ContextManager:
    """管理对话上下文 — 滑动窗口 + Token 预算 + 摘要压缩。

    设计要点:
        - 保留 system prompt 始终在窗口内
        - 超过阈值时，压缩最旧的非 system 消息
        - 支持异步摘要生成 (需要 LLM)

    使用示例::

        ctx = ContextManager(max_tokens=128000)
        ctx.add(Message(role=Role.USER, content="Hello"))
        messages = ctx.get_context()
    """

    def __init__(
        self,
        max_tokens: int = 128000,
        summary_threshold: float = 0.8,
        summarizer: Callable[..., Any] | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.summary_threshold = summary_threshold  # 80% 时开始压缩
        self._summarizer = summarizer
        self._messages: deque[Message] = deque()
        self._system_messages: list[Message] = []
        self._summary: str = ""
        self._estimated_tokens: int = 0
        self._token_counter = _SimpleTokenizer()

    # ── 消息管理 ──────────────────────────────────────────

    def add(self, message: Message) -> None:
        """添加消息到上下文窗口。"""
        if message.role == Role.SYSTEM:
            self._system_messages.append(message)
        else:
            self._messages.append(message)
            self._estimated_tokens += self._token_counter.count(message.content)

    def add_batch(self, messages: list[Message]) -> None:
        """批量添加消息。"""
        for m in messages:
            self.add(m)

    def get_context(self, include_summary: bool = True) -> list[Message]:
        """获取当前上下文窗口的消息列表。

        返回: system messages + [summary] + conversation messages
        """
        result: list[Message] = list(self._system_messages)

        if include_summary and self._summary:
            result.append(Message(
                role=Role.SYSTEM,
                content=f"[Conversation summary from earlier]\n{self._summary}",
            ))

        result.extend(self._messages)
        return result

    def get_messages_for_llm(self, include_summary: bool = True) -> list[dict[str, str]]:
        """返回适合 LLM API 的消息格式。"""
        return [m.to_dict() for m in self.get_context(include_summary)]

    # ── Token 管理 ────────────────────────────────────────

    def token_count(self) -> int:
        """估算当前上下文的 token 数量。"""
        total = self._estimated_tokens
        total += sum(self._token_counter.count(m.content) for m in self._system_messages)
        if self._summary:
            total += self._token_counter.count(self._summary)
        return total

    def should_compress(self) -> bool:
        """是否应该触发上下文压缩。"""
        return self.token_count() > int(self.max_tokens * self.summary_threshold)

    # ── 摘要压缩 ──────────────────────────────────────────

    async def compress(self, llm: Any | None = None) -> str:
        """压缩旧消息为摘要。

        保留最近 N/3 条消息，压缩前面的。使用 LLM 生成摘要，
        如果没有 LLM，使用简单的规则提取。

        Args:
            llm: LLM 后端实例 (可选，使用构造时注入的 summarizer 或手动传入)。

        Returns:
            生成的摘要文本。
        """
        if len(self._messages) < 4:
            return self._summary  # 太少消息，不压缩

        # 保留最近 1/3 的消息
        keep_count = max(2, len(self._messages) // 3)
        to_compress = list(self._messages)[:-keep_count]

        # 生成摘要
        if llm is not None or self._summarizer is not None:
            summary_text = await self._llm_summarize(to_compress, llm or self._summarizer)
        else:
            summary_text = self._simple_summarize(to_compress)

        # 更新状态
        self._summary = self._merge_summaries(self._summary, summary_text)
        # 移除已压缩的消息
        for _ in range(len(to_compress)):
            self._messages.popleft()
        # B-21 修复：从现有消息 + 摘要整体重算 token 估算，
        # 消除多次压缩后逐步累积的计数漂移（摘要本身也占用上下文）。
        self._recalculate_tokens()

        return self._summary

    def _recalculate_tokens(self) -> None:
        """从当前消息与摘要重新计算 token 估算值。"""
        total = sum(self._token_counter.count(m.content) for m in self._messages)
        if self._summary:
            total += self._token_counter.count(self._summary)
        self._estimated_tokens = total

    def clear(self) -> None:
        """清除所有上下文 (保留 system messages)。"""
        self._messages.clear()
        self._summary = ""
        self._estimated_tokens = 0

    def reset(self) -> None:
        """完全重置 — 包括 system messages。"""
        self._system_messages.clear()
        self._messages.clear()
        self._summary = ""
        self._estimated_tokens = 0

    # ── 任务状态栈 ────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """获取上下文统计信息。"""
        return {
            "message_count": len(self._messages),
            "system_message_count": len(self._system_messages),
            "estimated_tokens": self.token_count(),
            "max_tokens": self.max_tokens,
            "has_summary": bool(self._summary),
            "summary_length": len(self._summary) if self._summary else 0,
        }

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    async def _llm_summarize(messages: list[Message], llm: Any) -> str:
        """使用 LLM 生成摘要。"""
        conversation = "\n".join(
            f"[{m.role.value}]: {m.content[:500]}" for m in messages
        )
        prompt = (
            "Please summarize the following conversation concisely, "
            "preserving key facts, decisions, and context:\n\n"
            f"{conversation}\n\nSummary:"
        )
        try:
            response = await llm.generate([{"role": "user", "content": prompt}])
            return response.text.strip()
        except Exception:
            return ContextManager._simple_summarize(messages)

    @staticmethod
    def _simple_summarize(messages: list[Message]) -> str:
        """简单规则摘要 (无 LLM 时降级)。"""
        points = []
        for m in messages:
            content = m.content[:200]
            if m.role == Role.USER:
                points.append(f"User asked: {content}")
            elif m.role == Role.ASSISTANT:
                points.append(f"Assistant responded about: {content[:100]}")
            elif m.role == Role.TOOL:
                points.append(f"Tool result: {content[:100]}")
        return "\n".join(points[-10:])  # 只保留最近 10 条

    @staticmethod
    def _merge_summaries(old_summary: str, new_summary: str) -> str:
        """合并新旧摘要。"""
        if not old_summary:
            return new_summary
        return f"{old_summary}\n\n[Later]\n{new_summary}"


class _SimpleTokenizer:
    """简单的 token 计数器。

    使用 tiktoken 如果可用，否则使用字符数/4 估算。
    """

    def __init__(self) -> None:
        self._encoder = None
        try:
            import tiktoken
            self._encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            pass

    def count(self, text: str) -> int:
        if self._encoder:
            return len(self._encoder.encode(text))
        return len(text) // 4
