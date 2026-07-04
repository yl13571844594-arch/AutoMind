"""知识图谱 — 纯 Python 实体-关系图，支持路径查询与子图提取。"""

from __future__ import annotations

from typing import Any

from automind.planning.dependency_graph import SimpleDiGraph


class KnowledgeGraph:
    """基于 SimpleDiGraph 的知识图谱。

    节点: 实体 (文件、函数、类、模块、概念)
    边: 关系 (imports, calls, defines, inherits, depends_on)

    特性:
        - 有向图
        - 实体属性自由存储
        - 路径查询、子图提取
        - 转换为 Datalog 事实
    """

    def __init__(self) -> None:
        self.graph = SimpleDiGraph()

    # ── 实体管理 ──────────────────────────────────────────

    def add_entity(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        properties: dict[str, Any] | None = None,
    ) -> str:
        """添加实体节点。"""
        self.graph.add_node(entity_id, type=entity_type, name=name, **(properties or {}))
        return entity_id

    def remove_entity(self, entity_id: str) -> None:
        """移除实体及其所有关联关系。"""
        if self.graph.has_node(entity_id):
            for succ in self.graph.successors(entity_id):
                self.graph._reverse[succ] = [n for n in self.graph._reverse.get(succ, []) if n != entity_id]
            for pred in self.graph.predecessors(entity_id):
                self.graph._edges[pred] = [n for n in self.graph._edges.get(pred, []) if n != entity_id]
            self.graph._nodes.pop(entity_id, None)
            self.graph._edges.pop(entity_id, None)
            self.graph._reverse.pop(entity_id, None)

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        """获取实体属性。"""
        return dict(self.graph._nodes.get(entity_id, {}))

    def find_entities(self, entity_type: str | None = None, **filters: Any) -> list[str]:
        """按类型和属性筛选实体。"""
        result = []
        for node, attrs in self.graph._nodes.items():
            if entity_type and attrs.get("type") != entity_type:
                continue
            if all(attrs.get(k) == v for k, v in filters.items()):
                result.append(node)
        return result

    # ── 关系管理 ──────────────────────────────────────────

    def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """添加有向关系。"""
        self.graph.add_edge(source_id, target_id)
        # Store relation type in edge metadata (simple approach: store on source node)
        edge_key = f"rel_{source_id}_{target_id}"
        self.graph._nodes.setdefault(source_id, {})[edge_key] = {
            "type": relation_type, **(properties or {})
        }

    def _stored_relation_type(self, source_id: str, target_id: str) -> str:
        """B-09 修复：从源节点存储的边元数据读取真实关系类型（此前恒为 related）。"""
        node_data = self.graph._nodes.get(source_id) or {}
        edge = node_data.get(f"rel_{source_id}_{target_id}")
        if isinstance(edge, dict):
            return edge.get("type", "related")
        return "related"

    def get_relations(self, entity_id: str, direction: str = "both") -> list[dict[str, Any]]:
        """获取实体的关系。"""
        result = []
        if direction in ("out", "both"):
            for target in self.graph.successors(entity_id):
                result.append({
                    "source": entity_id, "target": target,
                    "type": self._stored_relation_type(entity_id, target),
                })
        if direction in ("in", "both"):
            for source in self.graph.predecessors(entity_id):
                result.append({
                    "source": source, "target": entity_id,
                    "type": self._stored_relation_type(source, entity_id),
                })
        return result

    # ── 查询 ──────────────────────────────────────────────

    def find_path(self, source_id: str, target_id: str) -> list[str] | None:
        """BFS 查找两个实体间的最短路径。"""
        if source_id not in self.graph._nodes or target_id not in self.graph._nodes:
            return None
        from collections import deque
        queue = deque([[source_id]])
        visited = {source_id}
        while queue:
            path = queue.popleft()
            node = path[-1]
            if node == target_id:
                return path
            for neighbor in self.graph.successors(node) + self.graph.predecessors(node):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(path + [neighbor])
        return None

    def get_neighbors(
        self, entity_id: str, relation_types: list[str] | None = None, depth: int = 1
    ) -> list[str]:
        """获取邻居实体 (支持多跳)。"""
        if depth <= 0:
            return []
        neighbors: set[str] = set()
        current = {entity_id}
        for _ in range(depth):
            next_level: set[str] = set()
            for node in current:
                for target in self.graph.successors(node):
                    next_level.add(target)
                for source in self.graph.predecessors(node):
                    next_level.add(source)
            neighbors.update(next_level)
            current = next_level
        return list(neighbors)

    # ── Datalog 转换 ──────────────────────────────────────

    def to_datalog_facts(self) -> list[str]:
        """将图谱转换为 Datalog 事实列表。"""
        facts = []
        for node, attrs in self.graph._nodes.items():
            facts.append(f"entity({node}, {attrs.get('type', 'unknown')}, {attrs.get('name', node)})")
        for u, v in self.graph.edges():
            facts.append(f"related({u}, {v})")
        return facts

    # ── 统计 ──────────────────────────────────────────────

    @property
    def entity_count(self) -> int:
        return len(self.graph._nodes)

    @property
    def relation_count(self) -> int:
        return sum(len(v) for v in self.graph._edges.values())

    def summary(self) -> str:
        """生成图谱摘要。"""
        type_counts: dict[str, int] = {}
        for attrs in self.graph._nodes.values():
            t = attrs.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        return (
            f"KnowledgeGraph: {self.entity_count} entities, {self.relation_count} relations\n"
            f"  Entity types: {type_counts}"
        )
