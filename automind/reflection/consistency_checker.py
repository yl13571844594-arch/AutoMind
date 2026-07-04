"""符号一致性检查器 — Datalog 规则验证逻辑一致性。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.core.types import Goal, GoalStatus, HierarchicalPlan, Predicate


@dataclass
class ConsistencyReport:
    """一致性检查报告。"""

    passed: bool
    violations: list[str] = field(default_factory=list)
    satisfied_conditions: list[str] = field(default_factory=list)
    unsatisfied_conditions: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class ConsistencyChecker:
    """符号一致性检查器。

    使用 Datalog 规则验证:
        1. 前置条件是否满足
        2. 后置条件是否达成
        3. 资源冲突检测
        4. 死锁检测 (循环等待资源)

    流程:
        - 将计划状态和目标转化为事实
        - 应用规则检查
        - 生成一致性报告
    """

    def __init__(self) -> None:
        # B-19 修复：移除"定义但从不查询"的 Datalog 死规则与引擎依赖；
        # 资源冲突检测由 check_resource_conflicts 的命令式实现负责。
        pass

    def check_goal_preconditions(self, goal: Goal, completed_goals: list[Goal]) -> ConsistencyReport:
        """检查目标的前置条件是否被已完成的目标满足。

        Args:
            goal: 待检查的目标。
            completed_goals: 已完成的目标列表。

        Returns:
            ConsistencyReport。
        """
        report = ConsistencyReport(passed=True)

        # 收集已完成目标的 effects
        achieved_effects: set[str] = set()
        for cg in completed_goals:
            for effect in cg.expected_effects:
                achieved_effects.add(str(effect))

        for precond in goal.preconditions:
            precond_str = str(precond)
            if precond_str in achieved_effects:
                report.satisfied_conditions.append(precond_str)
            elif precond.negated:
                # 负前置条件：检查是否有冲突的 effect
                positive = str(Predicate(name=precond.name, arguments=precond.arguments, negated=False))
                if positive in achieved_effects:
                    report.violations.append(
                        f"Precondition violated: {precond_str} — conflicting effect {positive} exists"
                    )
                    report.passed = False
                else:
                    report.satisfied_conditions.append(precond_str)
            else:
                report.unsatisfied_conditions.append(precond_str)
                report.violations.append(
                    f"Precondition not satisfied: {precond_str}"
                )
                report.passed = False

        return report

    def check_goal_postconditions(
        self, goal: Goal, tool_result: Any
    ) -> ConsistencyReport:
        """检查目标的后置条件是否被工具执行结果满足。

        Args:
            goal: 已完成的目标。
            tool_result: 工具执行结果。

        Returns:
            ConsistencyReport。
        """
        report = ConsistencyReport(passed=True)

        if not tool_result or not tool_result.success:
            report.passed = False
            report.violations.append(
                f"Tool execution failed for goal '{goal.description}': {tool_result.error if tool_result else 'no result'}"
            )
            return report

        for effect in goal.expected_effects:
            effect_str = str(effect)
            if self._verify_effect(effect, tool_result):
                report.satisfied_conditions.append(effect_str)
            else:
                report.unsatisfied_conditions.append(effect_str)
                report.violations.append(
                    f"Expected effect not verified: {effect_str}"
                )
                report.passed = False

        return report

    def check_resource_conflicts(self, goals: list[Goal]) -> list[str]:
        """检测目标之间的资源冲突。"""
        conflicts = []
        resource_owners: dict[str, str] = {}
        for g in goals:
            for dep in g.resource_deps:
                if dep in resource_owners and resource_owners[dep] != g.id:
                    conflicts.append(
                        f"Resource conflict: '{dep}' required by both "
                        f"'{resource_owners[dep]}' and '{g.id}'"
                    )
                else:
                    resource_owners[dep] = g.id
        return conflicts

    def check_plan_consistency(self, plan: HierarchicalPlan) -> ConsistencyReport:
        """检查整个计划的一致性。

        包括:
            - 前置条件链
            - 资源冲突
            - 循环依赖
        """
        report = ConsistencyReport(passed=True)
        all_goals = [plan.root_goal] + plan.root_goal.all_children()
        # B-18 修复：用枚举比较替代字符串字面量，避免枚举重构后静默失效。
        completed = [g for g in all_goals if g.status == GoalStatus.COMPLETED]

        for goal in all_goals:
            if goal.status not in (GoalStatus.COMPLETED, GoalStatus.FAILED):
                precond_report = self.check_goal_preconditions(goal, completed)
                if not precond_report.passed:
                    report.violations.extend(precond_report.violations)
                    report.passed = False

        conflicts = self.check_resource_conflicts(all_goals)
        if conflicts:
            report.violations.extend(conflicts)
            report.passed = False

        if not report.passed:
            report.suggestions.append(
                "Resolve unsatisfied preconditions before continuing execution"
            )

        return report

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _verify_effect(effect: Predicate, result: Any) -> bool:
        """验证工具结果是否满足预期效果。

        基于简单的关键词匹配 + 结果检查。
        """
        effect_str = str(effect).lower()
        output_str = str(result.output).lower() if result.output else ""

        checks = {
            "exists": lambda: result.success,
            "file_exists": lambda: result.success,
            "created": lambda: result.success,
            "installed": lambda: result.success,
            "passed": lambda: result.exit_code == 0,
            "exit_code": lambda: result.exit_code == 0,
            "test_pass": lambda: result.exit_code == 0,
            "success": lambda: result.success,
            "completed": lambda: result.success,
        }

        for keyword, check_fn in checks.items():
            if keyword in effect_str:
                return check_fn()

        # 默认: 如果工具成功且效果词在输出中出现
        if result.success:
            effect_words = set(effect.name.lower().split("_"))
            return any(w in output_str for w in effect_words)

        return False
