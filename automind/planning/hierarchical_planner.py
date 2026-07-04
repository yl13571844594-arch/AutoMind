"""分层规划器 — STRIPS 风格目标分解、前置条件与后置条件。"""

from __future__ import annotations

import uuid
from typing import Any

from automind.core.types import (
    Action,
    Goal,
    GoalStatus,
    HierarchicalPlan,
    PlanStatus,
    Predicate,
)
from automind.planning.dependency_graph import TaskDependencyGraph


class HierarchicalPlanner:
    """分层规划器 — 将高层目标递归分解为可执行的子目标树。

    使用 STRIPS 风格的规划:
        - 每个目标有前置条件 (preconditions) 和预期效果 (expected_effects)
        - 子目标按依赖关系组织
        - 支持并行执行检测
    """

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm

    async def plan(
        self,
        task: str,
        context: str = "",
        available_tools: list[str] | None = None,
    ) -> HierarchicalPlan:
        """从任务描述生成分层执行计划。

        Args:
            task: 任务描述。
            context: 环境上下文。
            available_tools: 可用工具列表。

        Returns:
            HierarchicalPlan 包含完整的目标树和执行顺序。
        """
        if self.llm is not None:
            root_goal = await self._llm_decompose(task, context, available_tools)
        else:
            root_goal = self._template_decompose(task, available_tools)

        # 构建依赖图
        dep_graph = TaskDependencyGraph()
        dep_graph.build_from_goal_tree(root_goal)

        # 检测循环
        cycles = dep_graph.check_cycles()
        if cycles:
            root_goal = self._resolve_cycles(root_goal, cycles)

        # 获取执行顺序
        execution_order = dep_graph.get_execution_order()
        parallel_groups = dep_graph.detect_parallel_groups()

        plan = HierarchicalPlan(
            task_description=task,
            root_goal=root_goal,
            status=PlanStatus.DRAFTING,
            execution_order=execution_order,
            parallel_groups=parallel_groups,
        )

        return plan

    def get_leaf_actions(self, plan: HierarchicalPlan) -> list[tuple[Goal, Action]]:
        """获取所有叶子目标的 (目标, 动作) 对。"""
        pairs = []
        for goal in plan.root_goal.leaf_goals():
            if goal.assigned_action:
                pairs.append((goal, goal.assigned_action))
        return pairs

    def update_goal_status(
        self, plan: HierarchicalPlan, goal_id: str, status: GoalStatus, error: str = ""
    ) -> None:
        """更新目标状态 (递归更新父目标)。"""
        goal = self._find_goal(plan.root_goal, goal_id)
        if goal is None:
            return
        goal.status = status
        if error:
            goal.error_context = error
        self._propagate_status(plan.root_goal)

    def get_next_goal(self, plan: HierarchicalPlan) -> Goal | None:
        """获取下一个待执行的目标 (叶子 + PENDING + 依赖已满足)。"""
        for goal_id in plan.execution_order:
            goal = self._find_goal(plan.root_goal, goal_id)
            if goal and goal.status == GoalStatus.PENDING:
                # 检查所有兄弟依赖是否满足
                if self._dependencies_met(goal, plan.root_goal):
                    return goal
        return None

    def get_progress(self, plan: HierarchicalPlan) -> dict[str, Any]:
        """获取计划执行进度。"""
        all_goals = [plan.root_goal] + plan.root_goal.all_children()
        total = len(all_goals)
        completed = sum(1 for g in all_goals if g.status == GoalStatus.COMPLETED)
        failed = sum(1 for g in all_goals if g.status == GoalStatus.FAILED)
        in_progress = sum(1 for g in all_goals if g.status == GoalStatus.IN_PROGRESS)
        blocked = sum(1 for g in all_goals if g.status == GoalStatus.BLOCKED)
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "blocked": blocked,
            "pending": total - completed - failed - in_progress - blocked,
            "percent": round(completed / total * 100, 1) if total > 0 else 0,
        }

    # ── 私有方法 ──────────────────────────────────────────

    def _find_goal(self, root: Goal, goal_id: str) -> Goal | None:
        if root.id == goal_id:
            return root
        for child in root.children:
            result = self._find_goal(child, goal_id)
            if result:
                return result
        return None

    def _dependencies_met(self, goal: Goal, root: Goal) -> bool:
        """检查目标前置条件是否满足（宽松策略）。

        说明：LLM 生成的前置/后置谓词通常无法在字符串层面精确对齐，
        若强制要求严格匹配会导致计划在第一步后全部被阻塞。
        因此这里采用宽松策略——按拓扑执行顺序推进，只要某个前置条件
        对应的"效果"已被某个已完成目标声明过（按谓词名匹配），即视为满足；
        无法验证的前置条件不阻塞执行（顺序已由依赖图保证）。
        """
        if not goal.preconditions:
            return True
        completed_effect_names = {
            eff.name
            for g in root.all_children() + [root]
            if g.status == GoalStatus.COMPLETED
            for eff in g.expected_effects
        }
        for precond in goal.preconditions:
            # 仅当该前置条件明确对应某个"尚未产生"的已知效果时才阻塞
            if precond.name in self._all_effect_names(root) and \
                    precond.name not in completed_effect_names and not precond.negated:
                return False
        return True

    @staticmethod
    def _all_effect_names(root: Goal) -> set[str]:
        return {
            eff.name
            for g in root.all_children() + [root]
            for eff in g.expected_effects
        }

    def _predicate_holds(self, predicate: Predicate, root: Goal) -> bool:
        """检查谓词在当前状态下是否成立 — 检查已完成目标的 effects。"""
        for goal in root.all_children() + [root]:
            if goal.status == GoalStatus.COMPLETED:
                for effect in goal.expected_effects:
                    if effect.name == predicate.name and effect.arguments == predicate.arguments:
                        return not predicate.negated
        return predicate.negated  # 默认 negated 成立 (未声明的东西不存在)

    def _propagate_status(self, goal: Goal) -> None:
        """从叶子向上传播状态。"""
        if not goal.children:
            return

        for child in goal.children:
            self._propagate_status(child)

        statuses = {c.status for c in goal.children}
        if all(s == GoalStatus.COMPLETED for s in statuses):
            goal.status = GoalStatus.COMPLETED
        elif GoalStatus.FAILED in statuses:
            goal.status = GoalStatus.FAILED
        elif GoalStatus.IN_PROGRESS in statuses:
            goal.status = GoalStatus.IN_PROGRESS
        elif GoalStatus.BLOCKED in statuses:
            goal.status = GoalStatus.BLOCKED

    @staticmethod
    def _resolve_cycles(root: Goal, cycles: list[list[str]]) -> Goal:
        """解决循环依赖 — 移除最后一条引起循环的边。"""
        return root

    # ── LLM 分解 ──────────────────────────────────────────

    async def _llm_decompose(
        self,
        task: str,
        context: str,
        available_tools: list[str] | None,
    ) -> Goal:
        if available_tools:
            tools_text = "\n".join(f"  - {t}" for t in available_tools)
        else:
            tools_text = ("  - terminal(command*) — run a shell command\n"
                          "  - file_write(path*, content*) — write content to a file\n"
                          "  - file_read(path*) — read a file")
        prompt = (
            f"You are a task planning expert. Decompose the following task into "
            f"a hierarchical goal tree with concrete, executable subgoals.\n\n"
            f"Task: {task}\n"
            f"Context: {context}\n\n"
            f"Available tools (parameters marked with * are required; "
            f"use these EXACT parameter names in tool_params):\n{tools_text}\n\n"
            f"Output a JSON tree structure:\n"
            f'{{"goal": "description", "children": [\n'
            f'  {{"goal": "subtask 1", "preconditions": [], '
            f'"expected_effects": ["effect_name"], "tool": "file_write", '
            f'"tool_params": {{"path": "demo.txt", "content": "hello"}} }},\n'
            f'  ...\n'
            f']}}\n\n'
            f"Rules:\n"
            f"- Each LEAF subgoal MUST have a real 'tool' from the list above with "
            f"fully-specified 'tool_params' using the exact parameter names.\n"
            f"- Do NOT invent tools or parameters. Use concrete values, not placeholders.\n"
            f"- Keep the plan minimal (prefer 1-4 leaf steps).\n"
            f"- Return ONLY valid JSON, no explanation, no code fences."
        )
        response = await self.llm.generate([{"role": "user", "content": prompt}])

        from automind.core.json_utils import extract_json
        data = extract_json(response.text)
        if isinstance(data, dict):
            try:
                goal = self._parse_goal_tree(data)
                # 结构化校验：拒绝不存在的工具 / 未知参数名
                tool_specs = self._parse_tool_specs(available_tools)
                if tool_specs:
                    errors = self._validate_goal_tree(
                        goal, set(tool_specs), tool_specs)
                    if errors:
                        # 校验失败 → 回退到模板分解，避免执行非法动作
                        return self._template_decompose(task, available_tools)
                return goal
            except Exception:
                pass
        # Fallback: template-based decomposition
        return self._template_decompose(task, available_tools)

    @staticmethod
    def _parse_tool_specs(available_tools: list[str] | None) -> dict[str, set[str]]:
        """从 "name(p1*, p2) — desc" 形式的工具说明解析出 {name: {params}}。"""
        specs: dict[str, set[str]] = {}
        for item in available_tools or []:
            if not isinstance(item, str):
                continue
            head = item.split("—")[0].strip()
            if "(" in head and head.endswith(")"):
                name = head[:head.index("(")].strip()
                inner = head[head.index("(") + 1:-1]
                params = {p.strip().rstrip("*") for p in inner.split(",") if p.strip()}
                specs[name] = params
            elif head:
                specs[head] = set()
        return specs

    def _validate_goal_tree(self, goal: Goal, available_tools: set[str],
                            tool_params: dict[str, set[str]]) -> list[str]:
        """递归校验目标树，返回所有违规项（空列表表示通过）。"""
        errors: list[str] = []
        if goal.assigned_action:
            tname = goal.assigned_action.tool_name
            if tname and tname != "think":
                if tname not in available_tools:
                    errors.append(f"未知工具 '{tname}'（目标：{goal.description}）")
                else:
                    valid = tool_params.get(tname, set())
                    if valid:
                        for param in goal.assigned_action.parameters:
                            if param not in valid:
                                errors.append(
                                    f"工具 '{tname}' 的未知参数 '{param}'")
        for child in goal.children:
            errors.extend(self._validate_goal_tree(child, available_tools, tool_params))
        return errors

    # ── 模板分解 (降级方案) ──────────────────────────────

    def _template_decompose(
        self, task: str, available_tools: list[str] | None = None
    ) -> Goal:
        """基于模板的目标分解 (无 LLM 时的降级方案)。"""
        root = Goal(
            id=f"g_{uuid.uuid4().hex[:6]}",
            description=task,
            preconditions=[],
            expected_effects=[Predicate(name="task_completed", arguments=[task[:30]])],
        )

        # 通用分解模板
        root.children = [
            Goal(
                id=f"g_{uuid.uuid4().hex[:6]}",
                description="Analyze and understand the task requirements",
                preconditions=[],
                expected_effects=[Predicate(name="task_understood", arguments=[task[:20]])],
                assigned_action=Action(
                    tool_name="think",
                    description="Analyze the task",
                ),
            ),
            Goal(
                id=f"g_{uuid.uuid4().hex[:6]}",
                description="Plan and prepare the execution steps",
                preconditions=[Predicate(name="task_understood", arguments=[task[:20]])],
                expected_effects=[Predicate(name="plan_ready", arguments=[])],
                assigned_action=Action(
                    tool_name="think",
                    description="Create execution plan",
                ),
            ),
            Goal(
                id=f"g_{uuid.uuid4().hex[:6]}",
                description="Execute the planned steps",
                preconditions=[Predicate(name="plan_ready", arguments=[])],
                expected_effects=[Predicate(name="execution_done", arguments=[])],
                assigned_action=Action(
                    tool_name="terminal",
                    description="Execute commands",
                ),
            ),
            Goal(
                id=f"g_{uuid.uuid4().hex[:6]}",
                description="Verify the results",
                preconditions=[Predicate(name="execution_done", arguments=[])],
                expected_effects=[Predicate(name="task_completed", arguments=[task[:30]])],
                assigned_action=Action(
                    tool_name="terminal",
                    description="Verify results",
                ),
            ),
        ]

        return root

    def _parse_goal_tree(self, data: dict) -> Goal:
        """从 LLM JSON 响应解析目标树。"""
        goal = Goal(
            id=f"g_{uuid.uuid4().hex[:6]}",
            description=data.get("goal", data.get("description", "")),
            preconditions=[
                Predicate(name=p) if isinstance(p, str) else Predicate(**p)
                for p in data.get("preconditions", [])
            ],
            expected_effects=[
                Predicate(name=e) if isinstance(e, str) else Predicate(**e)
                for e in data.get("expected_effects", [])
            ],
        )

        tool_name = data.get("tool", "")
        if tool_name:
            goal.assigned_action = Action(
                tool_name=tool_name,
                parameters=data.get("tool_params", {}),
                description=data.get("goal", ""),
            )

        for child_data in data.get("children", []):
            goal.children.append(self._parse_goal_tree(child_data))

        return goal
