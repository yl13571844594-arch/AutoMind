"""符号一致性检查 —— 决定 Agent 敢不敢执行下一步、以及执行完算不算数。

这层判断错了不会抛异常：要么放行了前置条件没满足的步骤（后面莫名其妙失败），
要么把已经达成的效果判成"未验证"（无谓地回溯重来）。两种都只能靠断言钉住。
"""

from __future__ import annotations

from automind.core.types import (
    Goal,
    GoalStatus,
    HierarchicalPlan,
    Predicate,
    ToolResult,
)
from automind.reflection.consistency_checker import ConsistencyChecker


def pred(name: str, *args: str, negated: bool = False) -> Predicate:
    return Predicate(name=name, arguments=list(args), negated=negated)


def goal(gid: str, desc="g", pre=None, eff=None, deps=None,
         status=GoalStatus.PENDING) -> Goal:
    g = Goal(id=gid, description=desc, status=status)
    g.preconditions = pre or []
    g.expected_effects = eff or []
    if deps:
        g.resource_deps = deps
    return g


class TestPreconditions:
    def test_satisfied_by_completed_effect(self):
        done = goal("g1", eff=[pred("file_exists", "/tmp/a")])
        target = goal("g2", pre=[pred("file_exists", "/tmp/a")])

        r = ConsistencyChecker().check_goal_preconditions(target, [done])
        assert r.passed
        assert str(pred("file_exists", "/tmp/a")) in r.satisfied_conditions
        assert not r.violations

    def test_unsatisfied_precondition_blocks(self):
        target = goal("g2", pre=[pred("file_exists", "/tmp/missing")])

        r = ConsistencyChecker().check_goal_preconditions(target, [])
        assert r.passed is False
        assert r.unsatisfied_conditions and r.violations

    def test_negated_precondition_holds_when_no_conflicting_effect(self):
        """¬file_exists(x) 在没人创建过 x 时应当成立。"""
        target = goal("g2", pre=[pred("file_exists", "/tmp/x", negated=True)])

        r = ConsistencyChecker().check_goal_preconditions(target, [])
        assert r.passed, "负前置条件在无冲突时应通过"

    def test_negated_precondition_violated_by_conflicting_effect(self):
        """已经有人创建了 x，却要求 ¬file_exists(x) —— 必须拦住。"""
        creator = goal("g1", eff=[pred("file_exists", "/tmp/x")])
        target = goal("g2", pre=[pred("file_exists", "/tmp/x", negated=True)])

        r = ConsistencyChecker().check_goal_preconditions(target, [creator])
        assert r.passed is False
        assert any("conflicting" in v for v in r.violations)

    def test_no_preconditions_always_passes(self):
        assert ConsistencyChecker().check_goal_preconditions(goal("g"), []).passed


class TestPostconditions:
    def test_failed_tool_fails_the_check(self):
        g = goal("g1", eff=[pred("file_exists", "/tmp/a")])
        res = ToolResult(tool_name="t", success=False, error="磁盘满了")

        r = ConsistencyChecker().check_goal_postconditions(g, res)
        assert r.passed is False
        assert any("磁盘满了" in v for v in r.violations)

    def test_missing_result_fails_the_check(self):
        """没有结果不能当成"没问题"——那会把没执行的步骤标成完成。"""
        g = goal("g1", eff=[pred("file_exists", "/tmp/a")])
        r = ConsistencyChecker().check_goal_postconditions(g, None)
        assert r.passed is False

    def test_success_without_expected_effects_passes(self):
        g = goal("g1")
        r = ConsistencyChecker().check_goal_postconditions(
            g, ToolResult(tool_name="t", success=True, output="ok"))
        assert r.passed


class TestResourceConflicts:
    def test_detects_two_goals_claiming_the_same_resource(self):
        a = goal("g1", deps=["db"])
        b = goal("g2", deps=["db"])
        conflicts = ConsistencyChecker().check_resource_conflicts([a, b])
        assert len(conflicts) == 1
        assert "db" in conflicts[0] and "g1" in conflicts[0] and "g2" in conflicts[0]

    def test_disjoint_resources_do_not_conflict(self):
        a = goal("g1", deps=["db"])
        b = goal("g2", deps=["cache"])
        assert ConsistencyChecker().check_resource_conflicts([a, b]) == []

    def test_same_goal_listing_a_resource_twice_is_not_a_conflict(self):
        """自己和自己不冲突 —— 否则会凭空冒出"资源冲突"把并行执行掐掉。"""
        a = goal("g1", deps=["db", "db"])
        assert ConsistencyChecker().check_resource_conflicts([a]) == []

    def test_empty_input(self):
        assert ConsistencyChecker().check_resource_conflicts([]) == []


class TestPlanConsistency:
    def _plan(self, root: Goal) -> HierarchicalPlan:
        return HierarchicalPlan(task_description="t", root_goal=root)

    def test_chain_with_satisfied_preconditions_passes(self):
        child_a = goal("a", eff=[pred("ready")], status=GoalStatus.COMPLETED)
        child_b = goal("b", pre=[pred("ready")])
        root = goal("root")
        root.children = [child_a, child_b]

        r = ConsistencyChecker().check_plan_consistency(self._plan(root))
        assert r.passed, r.violations

    def test_unsatisfied_chain_is_reported(self):
        child_b = goal("b", pre=[pred("ready")])   # 没有任何目标产出 ready
        root = goal("root")
        root.children = [child_b]

        r = ConsistencyChecker().check_plan_consistency(self._plan(root))
        assert r.passed is False
        assert r.violations

    def test_uses_enum_status_not_string(self):
        """B-18 回归：用枚举比较，不是字面量 'completed'。

        若实现退回字符串比较，已完成目标会被漏掉，
        其 effect 不再计入，下游目标的前置条件全部判为不满足。
        """
        producer = goal("a", eff=[pred("ready")], status=GoalStatus.COMPLETED)
        consumer = goal("b", pre=[pred("ready")])
        root = goal("root")
        root.children = [producer, consumer]

        r = ConsistencyChecker().check_plan_consistency(self._plan(root))
        assert r.passed, f"已完成目标的 effect 没被算上：{r.violations}"
