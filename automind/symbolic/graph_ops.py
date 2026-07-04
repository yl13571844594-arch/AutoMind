"""图操作 — 传递闭包、影响分析、依赖排序 (纯 Python + 可选 NetworkX)。"""

from __future__ import annotations

from automind.planning.dependency_graph import SimpleDiGraph


class GraphOps:
    """图操作工具集。

    使用 SimpleDiGraph 作为底层实现，提供:
        - 传递闭包
        - 影响分析
        - 拓扑排名
        - 循环检测
        - 连通分量
    """

    @staticmethod
    def transitive_closure(graph: SimpleDiGraph) -> SimpleDiGraph:
        """计算传递闭包。"""
        closure = SimpleDiGraph()
        for node in graph.nodes():
            closure.add_node(node, **graph._nodes.get(node, {}))
        for u in graph.nodes():
            descendants = graph.descendants(u)
            for v in descendants:
                closure.add_edge(u, v)
        return closure

    @staticmethod
    def impact_analysis(graph: SimpleDiGraph, node: str) -> dict[str, list[str]]:
        """分析修改某个节点的影响范围。"""
        successors = graph.successors(node)
        all_descendants = list(graph.descendants(node))
        critical = graph.longest_path_from(node)
        return {
            "direct_dependents": successors,
            "all_dependents": all_descendants,
            "critical_path": critical,
        }

    @staticmethod
    def topological_rank(graph: SimpleDiGraph) -> dict[str, int]:
        """计算拓扑排名。"""
        ranks: dict[str, int] = {}
        try:
            for node in graph.topological_sort():
                preds = graph.predecessors(node)
                ranks[node] = 0 if not preds else 1 + max(ranks.get(p, 0) for p in preds)
        except ValueError:
            pass
        return ranks

    @staticmethod
    def find_cycles(graph: SimpleDiGraph) -> list[list[str]]:
        """检测图中的所有环。"""
        return graph.find_cycles()

    @staticmethod
    def most_dependent_upon(graph: SimpleDiGraph, top_n: int = 10) -> list[tuple[str, int]]:
        """找出被最多节点依赖的节点 (最高入度)。"""
        degrees = [(node, graph.in_degree(node)) for node in graph.nodes()]
        degrees.sort(key=lambda x: -x[1])
        return degrees[:top_n]

    @staticmethod
    def most_depends_on(graph: SimpleDiGraph, top_n: int = 10) -> list[tuple[str, int]]:
        """找出依赖最多其他节点的节点 (最高出度)。"""
        degrees = [(node, graph.out_degree(node)) for node in graph.nodes()]
        degrees.sort(key=lambda x: -x[1])
        return degrees[:top_n]
