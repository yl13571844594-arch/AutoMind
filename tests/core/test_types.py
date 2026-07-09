"""核心类型测试 — Goal 树操作、Predicate 转换、枚举完整性。"""

from automind.core.types import (
    Goal,
    InteractionMode,
    Predicate,
    Role,
)


class TestGoalTree:
    def test_all_children_recursive(self, sample_goal):
        ids = {g.id for g in sample_goal.all_children()}
        assert ids == {"a", "b", "c"}

    def test_leaf_goals(self, sample_goal):
        leaves = {g.id for g in sample_goal.leaf_goals()}
        assert leaves == {"a", "c"}

    def test_single_node_is_its_own_leaf(self):
        g = Goal(id="x", description="solo")
        assert [n.id for n in g.leaf_goals()] == ["x"]


class TestPredicate:
    def test_to_datalog_positive(self):
        p = Predicate(name="file_exists", arguments=["a.txt"])
        assert p.to_datalog() == "file_exists(a.txt)"

    def test_to_datalog_negated(self):
        p = Predicate(name="file_exists", arguments=["a.txt"], negated=True)
        assert p.to_datalog() == "not file_exists(a.txt)"

    def test_str_repr(self):
        p = Predicate(name="ready", arguments=["x", "y"])
        assert str(p) == "ready(x, y)"


class TestEnums:
    def test_interaction_modes(self):
        vals = {m.value for m in InteractionMode}
        assert {"chat", "work", "coding", "multi", "loop"} <= vals

    def test_roles(self):
        assert Role.USER.value == "user"
        assert Role.ASSISTANT.value == "assistant"
