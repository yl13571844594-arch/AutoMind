"""实体记忆 — 从交互和代码中抽取结构化知识。"""

from __future__ import annotations

from typing import Any

from automind.core.types import Entity, Relation


class EntityMemory:
    """实体记忆 — 管理从对话和代码中抽取的结构化实体。

    实体类型:
        - file: 文件
        - function: 函数/方法
        - class: 类
        - module: 模块
        - concept: 抽象概念 (如 "authentication", "caching")
        - task: 任务/目标
        - error: 错误/问题
    """

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}

    def add_entity(self, entity: Entity) -> None:
        """添加实体。"""
        self._entities[entity.id] = entity

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def remove_entity(self, entity_id: str) -> None:
        self._entities.pop(entity_id, None)

    def search_entities(
        self,
        query: str | None = None,
        entity_type: str | None = None,
        limit: int = 10,
    ) -> list[Entity]:
        """搜索实体 — 按名称和类型筛选。"""
        results = []
        query_lower = query.lower() if query else ""
        for entity in self._entities.values():
            if entity_type and entity.type != entity_type:
                continue
            if query_lower and query_lower not in entity.name.lower():
                continue
            results.append(entity)
            if len(results) >= limit:
                break
        return results

    async def extract_from_text(
        self,
        text: str,
        llm: Any = None,
    ) -> list[Entity]:
        """从文本中抽取实体。

        如果提供 LLM，使用 LLM 进行结构化抽取；
        否则使用简单的正则匹配。

        Args:
            text: 文本内容。
            llm: LLM 后端 (可选)。

        Returns:
            抽取的实体列表。
        """
        if llm:
            return await self._llm_extract(text, llm)
        return self._simple_extract(text)

    def to_knowledge_graph(self, graph: Any) -> int:
        """将实体导出到知识图谱。

        Returns:
            导入的实体数量。
        """
        count = 0
        for entity in self._entities.values():
            graph.add_entity(
                entity.id,
                entity.type,
                entity.name,
                entity.properties,
            )
            count += 1
        return count

    @property
    def count(self) -> int:
        return len(self._entities)

    # ── 内部方法 ──────────────────────────────────────────

    async def _llm_extract(self, text: str, llm: Any) -> list[Entity]:
        prompt = (
            "Extract key entities from the following text. "
            "For each entity, provide: id, type (file/function/class/module/concept), "
            "name, and relevant properties. Output as JSON list.\n\n"
            f"Text:\n{text[:4000]}"
        )
        try:
            response = await llm.generate([{"role": "user", "content": prompt}])
            import json
            data = json.loads(response.text)
            entities = []
            for item in data if isinstance(data, list) else []:
                entities.append(Entity(
                    id=item.get("id", ""),
                    type=item.get("type", "concept"),
                    name=item.get("name", ""),
                    properties=item.get("properties", {}),
                ))
            for e in entities:
                # B-20 修复：同 ID 实体不再静默覆盖，而是合并属性。
                existing = self._entities.get(e.id)
                if existing is not None:
                    existing.properties.update(e.properties)
                else:
                    self._entities[e.id] = e
            return entities
        except Exception:
            return self._simple_extract(text)

    @staticmethod
    def _simple_extract(text: str) -> list[Entity]:
        """简单的正则实体抽取 (无 LLM 降级方案)。"""
        import re
        entities: list[Entity] = []
        # 文件引用
        for match in re.finditer(r'(?:[\w./\\-]+\.\w{1,6})', text):
            name = match.group()
            entities.append(Entity(
                id=f"file_{hash(name) & 0x7FFFFFFF:08x}",
                type="file",
                name=name,
            ))
        # 函数/类引用 (Python 风格)
        for match in re.finditer(r'\b([a-z_][a-z0-9_]*)\s*\(', text):
            name = match.group(1)
            if name not in ("if", "for", "while", "with", "print", "return"):
                entities.append(Entity(
                    id=f"func_{hash(name) & 0x7FFFFFFF:08x}",
                    type="function",
                    name=name,
                ))
        return entities
