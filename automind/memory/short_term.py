"""短期记忆 — 会话上下文窗口。"""

from __future__ import annotations

from collections import deque

from automind.core.types import MemoryChunk, Message, Role


class ShortTermMemory:
    """短期记忆 — 当前会话的对话缓冲。

    特性:
        - Token 限制的滑动窗口
        - 自动旧消息截断
        - 支持摘要压缩
        - 消息去重
    """

    def __init__(self, max_tokens: int = 128000) -> None:
        self.max_tokens = max_tokens
        self._messages: deque[Message] = deque()
        self._system_messages: list[Message] = []
        self._summary: str = ""
        self._token_count: int = 0
        self._version: int = 0

    def add(self, message: Message) -> None:
        """添加消息。"""
        if message.role == Role.SYSTEM:
            self._system_messages.append(message)
            return

        self._messages.append(message)
        self._token_count += self._estimate_tokens(message.content)
        self._version += 1
        self._trim_if_needed()

    def add_batch(self, messages: list[Message]) -> None:
        for m in messages:
            self.add(m)

    def get_all(self) -> list[Message]:
        """获取所有消息 (system + summary + conversation)。"""
        result: list[Message] = list(self._system_messages)
        if self._summary:
            result.append(Message(
                role=Role.SYSTEM,
                content=f"[Session summary]\n{self._summary}",
            ))
        result.extend(self._messages)
        return result

    def get_recent(self, n: int = 10) -> list[Message]:
        """获取最近的 n 条消息。"""
        return list(self._messages)[-n:]

    def get_for_llm(self) -> list[dict[str, str]]:
        """返回适合 LLM API 的消息格式。"""
        return [m.to_dict() for m in self.get_all()]

    def set_summary(self, summary: str) -> None:
        """设置会话摘要。"""
        self._summary = summary

    def should_compress(self, threshold: float = 0.8) -> bool:
        """是否应该触发压缩。"""
        return self._token_count > int(self.max_tokens * threshold) and len(self._messages) > 4

    def compress(self, keep_recent: int = 10) -> list[Message]:
        """压缩旧消息 — 保留最近 N 条，返回被压缩的消息 (用于生成摘要)。"""
        if len(self._messages) <= keep_recent:
            return []

        split_at = len(self._messages) - keep_recent
        to_compress = []
        for _ in range(split_at):
            msg = self._messages.popleft()
            to_compress.append(msg)
            self._token_count -= self._estimate_tokens(msg.content)

        self._version += 1
        return to_compress

    def search(self, query: str, k: int = 5) -> list[MemoryChunk]:
        """简单的关键词搜索最近消息。"""
        results = []
        query_lower = query.lower()
        for msg in reversed(self._messages):
            if query_lower in msg.content.lower():
                results.append(MemoryChunk(
                    source="short_term",
                    content=msg.content,
                    relevance_score=0.5,
                    metadata={"role": msg.role.value},
                ))
            if len(results) >= k:
                break
        return results

    def token_count(self) -> int:
        return self._token_count + sum(
            self._estimate_tokens(m.content) for m in self._system_messages
        )

    def clear(self) -> None:
        self._messages.clear()
        self._token_count = 0
        self._version += 1

    def reset(self) -> None:
        """完全重置，包括 system messages。"""
        self._system_messages.clear()
        self.clear()
        self._summary = ""

    @property
    def version(self) -> int:
        return self._version

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算文本 token 数量。"""
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text) // 4)

    def _trim_if_needed(self) -> None:
        """如果超出限制，移除最旧的非系统消息。"""
        while self._token_count > self.max_tokens and len(self._messages) > 1:
            old = self._messages.popleft()
            self._token_count -= self._estimate_tokens(old.content)
