"""记忆管理器 — 统一协调所有记忆子系统。"""

from __future__ import annotations

from typing import Any

from automind.core.types import MemoryChunk, Message
from automind.memory.entity_memory import EntityMemory
from automind.memory.knowledge_graph import KnowledgeGraph
from automind.memory.long_term import LongTermMemory
from automind.memory.project_memory import ProjectMemory
from automind.memory.short_term import ShortTermMemory


class MemoryManager:
    """记忆管理器 — 统一管理短期、长期、项目、图谱和实体记忆。

    使用示例::

        mm = MemoryManager(max_tokens=128000)
        mm.short_term.add(message)
        relevant = await mm.retrieve_relevant("how to fix this bug")
    """

    def __init__(
        self,
        max_tokens: int = 128000,
        persist_dir: str = ".automind/chroma",
        project_root: str = ".",
    ) -> None:
        self.short_term = ShortTermMemory(max_tokens=max_tokens)
        self.long_term = LongTermMemory(persist_dir=persist_dir)
        self.project = ProjectMemory(project_root)
        self.knowledge_graph = KnowledgeGraph()
        self.entity_memory = EntityMemory()

    async def store_interaction(
        self,
        user_message: Message,
        assistant_message: Message,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """存储一次完整的交互到所有记忆子系统。"""
        # 短期记忆
        self.short_term.add(user_message)
        self.short_term.add(assistant_message)

        # 长期记忆 (异步)
        import uuid
        await self.long_term.add(
            documents=[
                f"User: {user_message.content}",
                f"Assistant: {assistant_message.content}",
            ],
            metadatas=[
                {"type": "user_message", **(metadata or {})},
                {"type": "assistant_message", **(metadata or {})},
            ],
            ids=[uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]],
        )

        # 实体抽取 (从用户消息)
        entities = await self.entity_memory.extract_from_text(user_message.content)
        for entity in entities:
            self.knowledge_graph.add_entity(
                entity.id, entity.type, entity.name, entity.properties
            )

    async def retrieve_relevant(self, query: str, k: int = 5) -> list[MemoryChunk]:
        """从所有记忆系统检索相关信息。"""
        chunks: list[MemoryChunk] = []

        # 短期记忆搜索
        chunks.extend(self.short_term.search(query, k))

        # 长期记忆 RAG 检索
        long_results = await self.long_term.search(query, k)
        for r in long_results:
            chunks.append(MemoryChunk(
                source="long_term",
                content=r["document"],
                relevance_score=r.get("score", 0.0),
                metadata=r.get("metadata", {}),
            ))

        # 项目记忆 (风格指南)
        style = self.project.get_convention_prompt()
        if style:
            chunks.append(MemoryChunk(
                source="project",
                content=style,
                relevance_score=0.3,
            ))

        # 排序并去重
        chunks.sort(key=lambda c: c.relevance_score, reverse=True)
        seen = set()
        unique = []
        for c in chunks:
            key = c.content[:100]
            if key not in seen:
                seen.add(key)
                unique.append(c)

        return unique[:k]

    async def compress_context(self, llm: Any = None) -> str:
        """压缩短期记忆上下文。

        Returns:
            生成的摘要文本。
        """
        if not self.short_term.should_compress():
            return ""

        compressed = self.short_term.compress(keep_recent=10)
        if not compressed:
            return ""

        # 使用 LLM 生成摘要
        if llm:
            conversation = "\n".join(
                f"[{m.role.value}]: {m.content[:300]}" for m in compressed
            )
            try:
                response = await llm.generate([{
                    "role": "user",
                    "content": f"Summarize this conversation concisely:\n\n{conversation}",
                }])
                summary = response.text.strip()
            except Exception:
                summary = _simple_summarize(compressed)
        else:
            summary = _simple_summarize(compressed)

        self.short_term.set_summary(summary)

        # 同时存入长期记忆
        await self.long_term.add(
            documents=[summary],
            metadatas=[{"type": "session_summary"}],
        )

        return summary

    def get_stats(self) -> dict[str, Any]:
        """获取记忆系统统计信息。"""
        return {
            "short_term": {
                "messages": len(self.short_term._messages),
                "tokens": self.short_term.token_count(),
            },
            "long_term": {"documents": self.long_term.count()},
            "knowledge_graph": {
                "entities": self.knowledge_graph.entity_count,
                "relations": self.knowledge_graph.relation_count,
            },
            "entities": self.entity_memory.count,
        }

    def close(self) -> None:
        """释放记忆系统资源（ChromaDB 连接、短期窗口）。幂等。"""
        try:
            self.long_term.close()
        except Exception:
            pass
        try:
            self.short_term._messages.clear()
        except Exception:
            pass


def _simple_summarize(messages: list[Message]) -> str:
    """简单摘要生成 (无 LLM 降级方案)。"""
    points = []
    for m in messages[-15:]:
        text = m.content[:200]
        if m.role.value == "user":
            points.append(f"- User: {text}")
        else:
            points.append(f"- {m.role.value}: {text[:120]}")
    return "\n".join(points)
