"""SimpleDiGraph / TaskDependencyGraph 单元测试 — DAG 算法。"""

from automind.planning.dependency_graph import SimpleDiGraph


class TestSimpleDiGraph:
    def test_topological_sort_linear(self):
        g = SimpleDiGraph()
        for n in ("a", "b", "c"):
            g.add_node(n)
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        order = g.topological_sort()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_cycle_detection(self):
        g = SimpleDiGraph()
        for n in ("a", "b", "c"):
            g.add_node(n)
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", "a")
        assert len(g.find_cycles()) >= 1

    def test_no_cycle_on_dag(self):
        g = SimpleDiGraph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        assert g.find_cycles() == []

    def test_descendants(self):
        g = SimpleDiGraph()
        for n in ("a", "b", "c"):
            g.add_node(n)
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        assert g.descendants("a") == {"b", "c"}

    def test_ancestors(self):
        g = SimpleDiGraph()
        for n in ("a", "b", "c"):
            g.add_node(n)
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        assert g.ancestors("c") == {"a", "b"}

    def test_in_out_degree(self):
        g = SimpleDiGraph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        assert g.out_degree("a") == 1
        assert g.in_degree("b") == 1
        assert g.in_degree("a") == 0
