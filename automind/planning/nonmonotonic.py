"""非单调推理 — 信念修正、冲突检测、回溯。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.core.types import Goal, GoalStatus, HierarchicalPlan, Predicate


@dataclass
class Belief:
    """一条信念。"""

    id: str
    content: str
    source: str = ""  # 来源: "observation", "deduction", "assumption"
    confidence: float = 1.0
    supporting_evidence: list[str] = field(default_factory=list)


@dataclass
class Conflict:
    """冲突记录。"""

    description: str
    beliefs_involved: list[str]
    detected_at_goal: str
    resolution: str = ""


class NonMonotonicReasoner:
    """非单调推理引擎。

    核心机制:
        1. 信念管理 — 添加/撤回信念，维护依赖关系
        2. 一致性检查 — 检测逻辑冲突
        3. 冲突消解 — 撤回不可靠的信念
        4. 计划回溯 — 修改上游目标并重新规划

    信念依赖图:
        belief_a ──→ belief_c (a 支持 c)
        belief_b ──→ belief_c (b 也支持 c)
        如果撤回 a, c 仍然被 b 支持 (保留)
        如果 a 和 b 都被撤回, c 也自动撤回
    """

    def __init__(self) -> None:
        self.beliefs: dict[str, Belief] = {}
        self.justifications: dict[str, set[str]] = {}  # belief_id → 被它支持的信念
        self.dependents: dict[str, set[str]] = {}  # belief_id → 支持它的信念
        self.conflicts: list[Conflict] = []

    # ── 信念管理 ──────────────────────────────────────────

    def assert_belief(
        self,
        belief_id: str,
        content: str,
        justification_ids: list[str] | None = None,
        source: str = "observation",
        confidence: float = 1.0,
    ) -> Belief:
        """断言一个新信念。"""
        belief = Belief(
            id=belief_id,
            content=content,
            source=source,
            confidence=confidence,
            supporting_evidence=justification_ids or [],
        )
        self.beliefs[belief_id] = belief

        # 更新依赖关系
        for jid in justification_ids or []:
            if jid not in self.justifications:
                self.justifications[jid] = set()
            self.justifications[jid].add(belief_id)

            if belief_id not in self.dependents:
                self.dependents[belief_id] = set()
            self.dependents[belief_id].add(jid)

        return belief

    def retract_belief(self, belief_id: str) -> list[str]:
        """撤回一个信念及所有依赖它的信念。

        Returns:
            被撤回的信念 ID 列表。
        """
        if belief_id not in self.beliefs:
            return []

        # 找到所有被此信念支持的信念 (递归)
        to_retract = set()
        self._collect_dependents(belief_id, to_retract)
        to_retract.add(belief_id)

        # 对每个被支持的信念，检查是否还有其他支持
        actually_retracted = []
        for bid in sorted(to_retract):
            if bid == belief_id:
                # 直接撤回目标
                self.beliefs.pop(bid, None)
                actually_retracted.append(bid)
            else:
                # 检查是否还有其他支持
                remaining_support = self.dependents.get(bid, set()) - to_retract
                if not remaining_support:
                    self.beliefs.pop(bid, None)
                    actually_retracted.append(bid)

        return actually_retracted

    # ── 一致性检查 ────────────────────────────────────────

    def check_consistency(self) -> list[Conflict]:
        """检查信念集的一致性。

        Returns:
            检测到的冲突列表。
        """
        self.conflicts = []

        # 检测矛盾的信念对
        belief_list = list(self.beliefs.values())
        for i, b1 in enumerate(belief_list):
            for b2 in belief_list[i + 1:]:
                if self._are_contradictory(b1, b2):
                    self.conflicts.append(Conflict(
                        description=f"Contradiction: '{b1.content}' vs '{b2.content}'",
                        beliefs_involved=[b1.id, b2.id],
                        detected_at_goal="",
                    ))

        return self.conflicts

    def resolve_conflicts(self) -> list[str]:
        """消解冲突 — 撤回置信度低的信念。

        Returns:
            被撤回的信念 ID 列表。
        """
        retracted = []
        for conflict in self.conflicts:
            # 找到冲突中置信度最低的信念
            worst = min(
                conflict.beliefs_involved,
                key=lambda bid: self.beliefs.get(bid, Belief(id=bid, content="")).confidence,
            )
            retracted.extend(self.retract_belief(worst))
        return retracted

    # ── 计划回溯 ──────────────────────────────────────────

    def backtrack_plan(
        self,
        plan: HierarchicalPlan,
        failed_goal_id: str,
        error_context: str = "",
    ) -> HierarchicalPlan:
        """从失败的目标回溯，修改上游计划。

        策略:
            1. 找到失败目标的上游目标
            2. 分析失败原因
            3. 将受影响的目标重置为 PENDING
            4. 记录修订历史

        Args:
            plan: 当前计划。
            failed_goal_id: 失败的目标 ID。
            error_context: 错误描述。

        Returns:
            修订后的计划。
        """
        # 找到失败目标
        failed_goal = self._find_goal(plan.root_goal, failed_goal_id)
        if failed_goal is None:
            return plan

        # 标记失败
        failed_goal.status = GoalStatus.FAILED
        failed_goal.error_context = error_context

        # 找到上游目标链
        upstream = self._get_upstream_goals(plan.root_goal, failed_goal_id)

        # 重置上游目标中与失败相关的目标
        for goal in upstream:
            if goal.status in (GoalStatus.COMPLETED, GoalStatus.IN_PROGRESS):
                goal.status = GoalStatus.REVERTED

        # 重置失败目标及其下游
        self._reset_downstream(failed_goal)

        # 记录修订
        plan.revision_history.append(
            f"Backtrack from {failed_goal_id}: {error_context}"
        )
        plan.updated_at = plan.updated_at  # 更新时间戳

        return plan

    def suggested_fix(self, failed_goal: Goal) -> str:
        """分析失败目标并建议修正方案。"""
        suggestions = []
        if failed_goal.error_context:
            suggestions.append(f"Error: {failed_goal.error_context}")
        if failed_goal.assigned_action:
            suggestions.append(
                f"Check action: {failed_goal.assigned_action.tool_name} "
                f"with params {failed_goal.assigned_action.parameters}"
            )
        if failed_goal.preconditions:
            suggestions.append(
                f"Verify preconditions: {[str(p) for p in failed_goal.preconditions]}"
            )
        return "\n".join(suggestions) if suggestions else "No specific suggestion available."

    # ── 内部方法 ──────────────────────────────────────────

    def _collect_dependents(self, belief_id: str, result: set[str]) -> None:
        """递归收集所有依赖此信念的信念。"""
        for supported in self.justifications.get(belief_id, set()):
            if supported not in result:
                result.add(supported)
                self._collect_dependents(supported, result)

    @staticmethod
    def _are_contradictory(b1: Belief, b2: Belief) -> bool:
        """简单的矛盾检测。"""
        c1, c2 = b1.content.lower(), b2.content.lower()
        # 检查否定词
        neg_pairs = [
            ("success", "fail"), ("true", "false"), ("yes", "no"),
            ("exist", "not exist"), ("completed", "pending"),
        ]
        for pos, neg in neg_pairs:
            if (pos in c1 and neg in c2) or (neg in c1 and pos in c2):
                # 检查是否关于同一主体
                return True
        return False

    def _find_goal(self, root: Goal, goal_id: str) -> Goal | None:
        if root.id == goal_id:
            return root
        for child in root.children:
            result = self._find_goal(child, goal_id)
            if result:
                return result
        return None

    def _get_upstream_goals(self, root: Goal, goal_id: str) -> list[Goal]:
        """获取目标的所有上游目标 (从根到目标本身)。"""
        path = []
        if self._collect_path(root, goal_id, path):
            return path
        return []

    def _collect_path(self, current: Goal, target_id: str, path: list[Goal]) -> bool:
        path.append(current)
        if current.id == target_id:
            return True
        for child in current.children:
            if self._collect_path(child, target_id, path):
                return True
        path.pop()
        return False

    def _reset_downstream(self, goal: Goal) -> None:
        """重置目标及其所有下游目标。"""
        goal.status = GoalStatus.PENDING
        goal.retry_count = 0
        for child in goal.children:
            self._reset_downstream(child)
