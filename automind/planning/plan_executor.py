"""Plan-and-Execute 执行器 — 按计划逐步执行，支持重新规划和回溯。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.core.types import (
    Goal,
    GoalStatus,
    HierarchicalPlan,
    PlanStatus,
    ToolResult,
)
from automind.planning.hierarchical_planner import HierarchicalPlanner
from automind.planning.nonmonotonic import NonMonotonicReasoner
from automind.tools.base import ToolRegistry
from automind.tools.permissions import PermissionEngine


@dataclass
class StepResult:
    """单个步骤的执行结果。"""

    goal_id: str
    goal_description: str
    success: bool
    tool_result: ToolResult | None = None
    error: str = ""
    retries: int = 0


@dataclass
class ExecutionReport:
    """完整执行报告。"""

    plan: HierarchicalPlan
    steps: list[StepResult] = field(default_factory=list)
    backtracks: int = 0
    errors_corrected: int = 0
    total_steps: int = 0
    completed_steps: int = 0
    duration_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.completed_steps / max(self.total_steps, 1)


class PlanExecutor:
    """Plan-and-Execute 执行器。

    执行流程:
        1. 从 HierarchicalPlan 获取下一个待执行的叶子目标
        2. 执行目标的 assigned_action
        3. 验证后置条件
        4. 如果失败 → 自我纠正 或 回溯
        5. 继续下一个目标，直到全部完成

    特性:
        - 自动回溯 (非单调推理)
        - 自我纠错 (自动重试)
        - 人机协同 (关键步骤暂停审批)
        - 检查点保存
    """

    def __init__(
        self,
        llm: Any,
        tool_registry: ToolRegistry,
        permission_engine: PermissionEngine | None = None,
        max_retries: int = 3,
        auto_retry: bool = True,
        parallel: bool = True,
        use_cache: bool = True,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        self.permissions = permission_engine or PermissionEngine()
        self.hierarchical_planner = HierarchicalPlanner(llm)
        self.nonmonotonic = NonMonotonicReasoner()
        self.max_retries = max_retries
        self.auto_retry = auto_retry
        # 并行执行（§2.4）：互不依赖的就绪目标用 asyncio.gather 并发
        self.parallel = parallel
        # 子任务缓存：同一次计划执行内，相同的 SAFE 级只读调用结果复用
        self.use_cache = use_cache
        self._subtask_cache: dict[str, ToolResult] = {}
        self.cache_hits = 0

    async def execute(
        self,
        plan: HierarchicalPlan,
        on_step_start: Any = None,
        on_step_end: Any = None,
        on_backtrack: Any = None,
        on_approval_needed: Any = None,
    ) -> ExecutionReport:
        """执行分层计划。

        Args:
            plan: 待执行的计划。
            on_step_start: 步骤开始回调 (goal) → None。
            on_step_end: 步骤结束回调 (StepResult) → None。
            on_backtrack: 回溯回调 (goal_id, reason) → None。
            on_approval_needed: 审批回调 (goal, action) → bool (允许/拒绝)。

        Returns:
            执行报告。
        """
        import time
        start_time = time.perf_counter()

        import asyncio as _asyncio

        plan.status = PlanStatus.EXECUTING
        report = ExecutionReport(plan=plan)
        # 每次计划执行使用独立的子任务缓存
        self._subtask_cache = {}
        self.cache_hits = 0
        # B-01 修复：记录每个目标已尝试的自我纠错次数，纠错后重新执行且有上限，
        # 既保证"修正后的动作被真正执行"，又避免反复纠错导致的死循环。
        correction_attempts: dict[str, int] = {}
        aborted = False

        while not aborted:
            # 收集当前全部就绪目标（PENDING + 依赖满足）
            ready = self._ready_goals(plan)
            if not ready:
                break

            # §2.4 并行执行：多个互不依赖的就绪目标并发跑；否则退化为串行单个
            batch = ready if (self.parallel and len(ready) > 1) else ready[:1]

            for g in batch:
                report.total_steps += 1
                if on_step_start:
                    await on_step_start(g)

            if len(batch) > 1:
                step_results = await _asyncio.gather(*[
                    self._execute_goal(g, on_approval_needed) for g in batch
                ])
            else:
                step_results = [await self._execute_goal(batch[0], on_approval_needed)]

            # 按序处理批内结果（状态更新与失败处理保持确定性）
            for goal, step_result in zip(batch, step_results):
                if on_step_end:
                    await on_step_end(step_result)
                report.steps.append(step_result)

                if step_result.success:
                    report.completed_steps += 1
                    self.hierarchical_planner.update_goal_status(
                        plan, goal.id, GoalStatus.COMPLETED
                    )
                    continue

                # 失败 → 尝试修正或回溯
                corrected = await self._handle_failure(
                    plan, goal, step_result, report, on_backtrack
                )
                attempts = correction_attempts.get(goal.id, 0)
                if corrected and attempts < self.max_retries:
                    report.errors_corrected += 1
                    correction_attempts[goal.id] = attempts + 1
                    # B-01：修正后的目标重置为 PENDING，下一轮重新执行
                    self.hierarchical_planner.update_goal_status(
                        plan, goal.id, GoalStatus.PENDING
                    )
                else:
                    # 无法修正，或纠错次数已达上限
                    self.hierarchical_planner.update_goal_status(
                        plan, goal.id, GoalStatus.FAILED, step_result.error
                    )
                    aborted = True
                    break

        # 更新计划状态
        progress = self.hierarchical_planner.get_progress(plan)
        if progress["failed"] == 0:
            plan.status = PlanStatus.COMPLETED
        elif progress["completed"] > 0:
            plan.status = PlanStatus.FAILED
        else:
            plan.status = PlanStatus.FAILED

        report.duration_ms = (time.perf_counter() - start_time) * 1000
        return report

    def _ready_goals(self, plan: HierarchicalPlan) -> list[Goal]:
        """收集当前所有就绪的叶子目标（PENDING 且依赖已满足），按执行顺序返回。"""
        ready: list[Goal] = []
        for goal_id in plan.execution_order:
            goal = self.hierarchical_planner._find_goal(plan.root_goal, goal_id)
            if goal and goal.status == GoalStatus.PENDING and \
                    self.hierarchical_planner._dependencies_met(goal, plan.root_goal):
                ready.append(goal)
        return ready

    @staticmethod
    def _cache_key(tool_name: str, params: dict[str, Any]) -> str:
        import json as _json
        try:
            return tool_name + "::" + _json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return tool_name + "::" + repr(sorted(params.items()))

    async def _execute_goal(
        self,
        goal: Goal,
        on_approval_needed: Any = None,
    ) -> StepResult:
        """执行单个目标。"""
        goal.status = GoalStatus.IN_PROGRESS

        if goal.assigned_action is None:
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=True,
                error="No action assigned — skipping",
            )

        action = goal.assigned_action

        # "think" 步骤是纯分析步骤，无需工具执行，直接标记完成
        if action.tool_name == "think":
            goal.status = GoalStatus.COMPLETED
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=True,
            )

        # 权限检查
        tool = self._get_tool(action.tool_name)
        if tool is not None:
            decision, reason = self.permissions.check(
                action.tool_name, tool.permission_tier, action.parameters
            )
            if decision.value == "deny":
                return StepResult(
                    goal_id=goal.id,
                    goal_description=goal.description,
                    success=False,
                    error=f"Permission denied: {reason}",
                )
            if decision.value == "ask_user" and on_approval_needed:
                approved = await on_approval_needed(goal, action)
                if not approved:
                    return StepResult(
                        goal_id=goal.id,
                        goal_description=goal.description,
                        success=False,
                        error="User denied the action",
                    )

        # 子任务缓存：SAFE 级只读工具（file_read/web_fetch 等）同参调用直接复用结果，
        # 避免并行/重试场景下的重复 IO；写类工具绝不缓存。
        from automind.core.types import PermissionTier as _PT
        cacheable = (
            self.use_cache and tool is not None
            and tool.permission_tier == _PT.SAFE
        )
        cache_key = self._cache_key(action.tool_name, action.parameters) if cacheable else ""
        if cacheable and cache_key in self._subtask_cache:
            self.cache_hits += 1
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=True,
                tool_result=self._subtask_cache[cache_key],
                retries=0,
            )

        # 执行
        for attempt in range(self.max_retries):
            try:
                result = await self.tool_registry.dispatch(
                    action.tool_name, **action.parameters
                )

                if result.success:
                    if cacheable:
                        self._subtask_cache[cache_key] = result
                    return StepResult(
                        goal_id=goal.id,
                        goal_description=goal.description,
                        success=True,
                        tool_result=result,
                        retries=attempt,
                    )

                # 失败 → 如果 auto_retry，继续尝试
                if not self.auto_retry:
                    break

            except Exception as e:
                if attempt == self.max_retries - 1:
                    return StepResult(
                        goal_id=goal.id,
                        goal_description=goal.description,
                        success=False,
                        error=str(e),
                        retries=attempt + 1,
                    )

        return StepResult(
            goal_id=goal.id,
            goal_description=goal.description,
            success=False,
            error=f"Failed after {self.max_retries} attempts",
            retries=self.max_retries,
        )

    async def _handle_failure(
        self,
        plan: HierarchicalPlan,
        failed_goal: Goal,
        step_result: StepResult,
        report: ExecutionReport,
        on_backtrack: Any = None,
    ) -> bool:
        """处理执行失败 — 尝试自我纠错或回溯。"""
        # 1. 尝试自我纠错
        if self.llm and step_result.error:
            fix = await self._self_correct(failed_goal, step_result)
            if fix:
                return True  # 纠正成功

        # 2. 回溯
        report.backtracks += 1
        self.nonmonotonic.backtrack_plan(
            plan, failed_goal.id, step_result.error
        )

        if on_backtrack:
            await on_backtrack(failed_goal.id, step_result.error)

        return False  # 无法自动恢复

    async def _self_correct(
        self, failed_goal: Goal, step_result: StepResult
    ) -> bool:
        """使用 LLM 分析错误并提出修正方案。"""
        prompt = (
            f"A goal execution failed. Analyze the error and suggest a fix.\n\n"
            f"Goal: {failed_goal.description}\n"
            f"Action: {failed_goal.assigned_action.tool_name if failed_goal.assigned_action else 'none'}\n"
            f"Parameters: {failed_goal.assigned_action.parameters if failed_goal.assigned_action else {}}\n"
            f"Error: {step_result.error}\n\n"
            f"Provide a corrected set of parameters (as JSON) or a different approach. "
            f"If the action itself needs to change, suggest a different tool.\n"
            f'Return: {{"tool": "tool_name", "params": {{}}}}'
        )
        try:
            response = await self.llm.generate([{"role": "user", "content": prompt}])
            from automind.core.json_utils import extract_json
            fix = extract_json(response.text)
            if not isinstance(fix, dict):
                return False
            if fix.get("tool") and failed_goal.assigned_action:
                failed_goal.assigned_action.tool_name = fix["tool"]
            if fix.get("params") and failed_goal.assigned_action:
                failed_goal.assigned_action.parameters = fix["params"]
            return True
        except Exception:
            return False

    def _get_tool(self, name: str) -> Any:
        """安全获取工具。"""
        try:
            return self.tool_registry.get(name)
        except Exception:
            return None
