"""任务依赖图 — DAG 管理、拓扑排序、并行检测、约束求解 (纯 Python 实现)。"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from automind.core.types import Goal, GoalStatus


class SimpleDiGraph:
    """轻量有向图 — 纯 Python 实现，不依赖 NetworkX。"""

    def __init__(self) -> None:
        self._nodes: dict[str, Any] = {}
        self._edges: dict[str, list[str]] = defaultdict(list)  # source → [targets]
        self._reverse: dict[str, list[str]] = defaultdict(list)  # target → [sources]

    def add_node(self, node_id: str, **attrs: Any) -> None:
        self._nodes[node_id] = attrs

    def add_edge(self, u: str, v: str) -> None:
        if v not in self._edges[u]:
            self._edges[u].append(v)
        if u not in self._reverse[v]:
            self._reverse[v].append(u)

    def nodes(self) -> list[str]:
        return list(self._nodes.keys())

    def edges(self) -> list[tuple[str, str]]:
        return [(u, v) for u, targets in self._edges.items() for v in targets]

    def successors(self, node: str) -> list[str]:
        return list(self._edges.get(node, []))

    def predecessors(self, node: str) -> list[str]:
        return list(self._reverse.get(node, []))

    def in_degree(self, node: str) -> int:
        return len(self._reverse.get(node, []))

    def out_degree(self, node: str) -> int:
        return len(self._edges.get(node, []))

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()
        self._reverse.clear()

    def has_node(self, node: str) -> bool:
        return node in self._nodes

    def topological_sort(self) -> list[str]:
        """Kahn 算法拓扑排序。"""
        in_degree = {n: len(self._reverse.get(n, [])) for n in self._nodes}
        queue = deque([n for n, d in in_degree.items() if d == 0])
        result = []

        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in self._edges.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self._nodes):
            raise ValueError("Dependency graph contains a cycle!")

        return result

    def descendants(self, node: str) -> set[str]:
        """DFS 获取所有后代。"""
        visited: set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            for succ in self._edges.get(n, []):
                if succ not in visited:
                    visited.add(succ)
                    stack.append(succ)
        return visited

    def ancestors(self, node: str) -> set[str]:
        """逆 DFS 获取所有祖先。"""
        visited: set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            for pred in self._reverse.get(n, []):
                if pred not in visited:
                    visited.add(pred)
                    stack.append(pred)
        return visited

    def find_cycles(self) -> list[list[str]]:
        """检测环 (简单 DFS 着色法)。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self._nodes}
        cycles = []

        def dfs(node: str, path: list[str]) -> None:
            color[node] = GRAY
            path.append(node)
            for neighbor in self._edges.get(node, []):
                if color.get(neighbor) == GRAY:
                    # 找到环
                    idx = path.index(neighbor)
                    cycles.append(path[idx:] + [neighbor])
                elif color.get(neighbor) == WHITE:
                    dfs(neighbor, path)
            path.pop()
            color[node] = BLACK

        for n in self._nodes:
            if color.get(n) == WHITE:
                dfs(n, [])
        return cycles

    def longest_path(self) -> list[str]:
        """DAG 最长路径 (动态规划)。"""
        topo = self.topological_sort()
        dist: dict[str, int] = {n: 0 for n in self._nodes}
        parent: dict[str, str | None] = {n: None for n in self._nodes}

        for u in topo:
            for v in self._edges.get(u, []):
                if dist[u] + 1 > dist[v]:
                    dist[v] = dist[u] + 1
                    parent[v] = u

        # 回溯路径
        end = max(dist, key=lambda k: dist[k]) if dist else ""
        return self._backtrack_path(parent, end)

    def longest_path_from(self, start: str) -> list[str]:
        """从指定起始节点的最长路径。"""
        topo = self.topological_sort()
        dist: dict[str, int] = {n: 0 for n in self._nodes}
        parent: dict[str, str | None] = {n: None for n in self._nodes}

        # Only process nodes reachable from start
        reachable = self.descendants(start) | {start}
        for u in topo:
            if u not in reachable:
                continue
            for v in self._edges.get(u, []):
                if v in reachable and dist[u] + 1 > dist[v]:
                    dist[v] = dist[u] + 1
                    parent[v] = u

        end = max(reachable, key=lambda k: dist.get(k, 0))
        return self._backtrack_path(parent, end)

    @staticmethod
    def _backtrack_path(parent: dict[str, str | None], end: str) -> list[str]:
        path = []
        current = end
        while current:
            path.append(current)
            current = parent.get(current)
        return list(reversed(path))


class TaskDependencyGraph:
    """任务依赖图 — 管理目标间的依赖关系。"""

    def __init__(self) -> None:
        self.graph = SimpleDiGraph()
        self._goal_map: dict[str, Goal] = {}

    def build_from_goal_tree(self, root_goal: Goal) -> None:
        self.graph.clear()
        self._goal_map.clear()
        self._add_goal_recursive(root_goal)
        self._build_edges(root_goal)

    def topological_order(self) -> list[str]:
        try:
            return self.graph.topological_sort()
        except ValueError as e:
            raise ValueError("Dependency graph contains a cycle!") from e

    def detect_parallel_groups(self) -> list[list[str]]:
        groups: list[list[str]] = []
        remaining = set(self.graph.nodes())
        while remaining:
            ready = [n for n in remaining
                     if self.graph.in_degree(n) == 0
                     or all(pred not in remaining for pred in self.graph.predecessors(n))]
            if not ready:
                break
            groups.append(ready)
            remaining.difference_update(ready)
        return groups

    def get_execution_order(self) -> list[str]:
        return self.topological_order()

    def get_downstream(self, goal_id: str) -> list[str]:
        return list(self.graph.descendants(goal_id))

    def get_upstream(self, goal_id: str) -> list[str]:
        return list(self.graph.ancestors(goal_id))

    def critical_path(self) -> list[str]:
        try:
            return self.graph.longest_path()
        except Exception:
            return []

    def check_cycles(self) -> list[list[str]]:
        return self.graph.find_cycles()

    def get_goal(self, goal_id: str) -> Goal | None:
        return self._goal_map.get(goal_id)

    def mark_completed(self, goal_id: str) -> None:
        goal = self._goal_map.get(goal_id)
        if goal:
            goal.status = GoalStatus.COMPLETED

    def mark_failed(self, goal_id: str) -> None:
        goal = self._goal_map.get(goal_id)
        if goal:
            goal.status = GoalStatus.FAILED

    def _add_goal_recursive(self, goal: Goal) -> None:
        self.graph.add_node(goal.id, goal=goal)
        self._goal_map[goal.id] = goal
        for child in goal.children:
            self._add_goal_recursive(child)

    def _build_edges(self, goal: Goal) -> None:
        for child in goal.children:
            self.graph.add_edge(goal.id, child.id)
            self._build_edges(child)

        for i, g1 in enumerate(goal.children):
            for g2 in goal.children[i + 1:]:
                for dep in g1.resource_deps:
                    for effect in g2.expected_effects:
                        if dep == effect.name or dep in effect.arguments:
                            # B-02 修复：g1 消费的资源由 g2 产生 → g2 必须先执行。
                            # 边 u→v 表示 u 先于 v，故方向应为 g2 → g1（此前反向）。
                            self.graph.add_edge(g2.id, g1.id)
                            break

    def to_mermaid(self) -> str:
        lines = ["graph TD"]
        for u, v in self.graph.edges():
            lines.append(f"    {u}[{self._goal_map.get(u, u)}] --> {v}[{self._goal_map.get(v, v)}]")
        return "\n".join(lines)
