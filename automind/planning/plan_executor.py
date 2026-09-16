"""Plan-and-Execute 执行器 — 按计划逐步执行，支持重新规划和回溯。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.core.logging import get_logger
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

logger = get_logger("automind.planning.plan_executor")


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
        goal_timeout: float = 900.0,
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
        # v1.7.0：单目标执行上限（秒）。0/负数 = 不限（恢复旧行为）。
        # 异常屏障只兜得住"抛出来"，兜不住"挂住不回"—— 这是并行批次
        # 停滞的主因（网络读、子进程、审批等待都可能无限期挂起）。
        try:
            self.goal_timeout = max(0.0, float(goal_timeout or 0))
        except (TypeError, ValueError):
            self.goal_timeout = 900.0
        #: 超时熔断计数（供报告说明"有几个步骤是被超时掐掉的"）
        self.timed_out = 0

    async def execute(
        self,
        plan: HierarchicalPlan,
        on_step_start: Any = None,
        on_step_end: Any = None,
        on_backtrack: Any = None,
        on_approval_needed: Any = None,
        deadline: float | None = None,
    ) -> ExecutionReport:
        """执行分层计划。

        Args:
            plan: 待执行的计划。
            on_step_start: 步骤开始回调 (goal) → None。
            on_step_end: 步骤结束回调 (StepResult) → None。
            on_backtrack: 回溯回调 (goal_id, reason) → None。
            on_approval_needed: 审批回调 (goal, action) → bool (允许/拒绝)。
            deadline: 整轮任务的绝对截止时刻（``time.monotonic()`` 基准）。
                None = 不限。到点不再开启新批次，剩余目标标为超时，
                已完成的步骤全部保留在报告里。

        Returns:
            执行报告。
        """
        import time
        start_time = time.perf_counter()

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

            # v1.7.0 整轮任务预算：到点**不再开启新批次**。
            # 判据放在批次之间而不是"硬掐断"：掐断会丢掉已做完的工作，
            # 而这里能把成果完整地交出去（已完成步骤照常入账）。
            if deadline is not None and time.monotonic() >= deadline:
                for g in ready:
                    self.hierarchical_planner.update_goal_status(
                        plan, g.id, GoalStatus.FAILED,
                        "整轮任务总时长预算已到点，该步骤未开始")
                    report.steps.append(StepResult(
                        goal_id=g.id, goal_description=g.description,
                        success=False,
                        error=("整轮任务的总时长预算已到点，本步骤未开始执行"
                               "（已完成的步骤成果全部保留）。"
                               "如确需跑完整个计划，请调大 "
                               "execution.max_task_seconds 或拆小任务。")))
                logger.warning("plan_task_budget_exhausted",
                               remaining=len(ready),
                               completed=report.completed_steps)
                break

            # §2.4 并行执行：多个互不依赖的就绪目标并发跑；否则退化为串行单个
            batch = ready if (self.parallel and len(ready) > 1) else ready[:1]

            for g in batch:
                report.total_steps += 1
                if on_step_start:
                    await on_step_start(g)

            if len(batch) > 1:
                # §2.4 并行执行 + v1.6.4 单点异常防护 + v1.7.0 挂起防护：
                # `asyncio.gather` 默认"任一任务抛异常就整体失败"——工具侧偶发异常
                # （超时竞态、第三方库抛错、审批通道断开）会让**同一批里已经跑完
                # 的目标一起丢失**，整单任务被判失败，用户看到的是"什么都没做"。
                # 这里给每个目标单独包一层：异常就地转成该目标的失败结果，
                # 其余目标的成果照常保留、照常进入报告与后续步骤。
                #
                # v1.7.0 补的另一半：**hang 不是异常**。一个目标卡在网络读 /
                # 子进程 / 审批上时既不返回也不抛错，gather 会一直等下去，
                # 整批（连同整个计划）就此停滞，而用户看到的只是"界面没反应"。
                # 因此每个目标再带一个执行上限（goal_timeout），超时就地转成
                # 该目标的失败结果；批级再加一道外层兜底，确保 gather 一定返回。
                step_results = await self._gather_batch(batch, on_approval_needed)
            else:
                # 单目标路径同样要带上限 —— "批里只有一个目标"并不意味着它不会挂，
                # 而挂住的单目标会让整个计划停在这一步（v1.7.0 之前没有任何兜底）。
                step_results = [await self._run_goal_with_timeout(
                    batch[0], on_approval_needed)]

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
                    # v1.6.4：**不要在此 break** —— 同批里已经跑完的其它目标
                    # 必须照常入账（否则报告与界面上它们凭空消失，用户看到的
                    # 是"整批都没做"，而实际上其中几个真的成了）。
                    # 终止语义不变：本轮结束后 while 条件让计划停止推进。

            if aborted:
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

    async def _gather_batch(
        self,
        batch: list[Goal],
        on_approval_needed: Any = None,
    ) -> list[StepResult]:
        """并发跑一批目标，**保证一定返回**。

        三层保护，缺一层就还有"整批不回"的路径：

        1. 异常屏障（``_execute_goal_guarded``）：抛出来的错就地转成该目标的失败。
        2. 单目标超时（``goal_timeout``）：挂住的（不抛错也不返回的）目标被掐掉，
           转成明确的失败结果并说明"是超时，不是没做"。
        3. 批级兜底超时：单目标超时理论上够用，但若某个工具屏蔽了取消
           （``asyncio.shield`` / 同步阻塞调用跑在事件循环里），
           ``wait_for`` 自己也会被卡住。批级用 ``asyncio.wait`` 到点就走，
           把还没完成的目标标为超时 —— **永远不让计划执行停滞**。
        """
        import asyncio as _asyncio

        tasks = [_asyncio.ensure_future(self._run_goal_with_timeout(g, on_approval_needed))
                 for g in batch]
        if self.goal_timeout <= 0:
            return list(await _asyncio.gather(*tasks))

        # 批级上限 = 单目标上限 + 一点余量（单目标内部已经各留了自己的预算）
        budget = self.goal_timeout + 5.0
        done, pending = await _asyncio.wait(tasks, timeout=budget)
        for t in pending:
            t.cancel()
        results: list[StepResult] = []
        for goal, task in zip(batch, tasks):
            if task in done:
                try:
                    results.append(task.result())
                except Exception as e:      # pragma: no cover - 屏障已有兜底
                    results.append(self._timeout_result(
                        goal, f"{type(e).__name__}: {e}"))
            else:
                results.append(self._timeout_result(
                    goal, f"整批并行执行超过 {budget:.0f} 秒仍未返回"))
        if pending:
            # 让被取消的任务有机会真正结束，避免 "Task was destroyed but it is
            # pending" 噪声盖住真正的问题
            await _asyncio.gather(*pending, return_exceptions=True)
        return results

    async def _run_goal_with_timeout(
        self,
        goal: Goal,
        on_approval_needed: Any = None,
    ) -> StepResult:
        """单目标执行 + 超时熔断（超时转成该目标的失败结果，不拖累同批）。"""
        import asyncio as _asyncio

        if self.goal_timeout <= 0:
            return await self._execute_goal_guarded(goal, on_approval_needed)
        try:
            return await _asyncio.wait_for(
                self._execute_goal_guarded(goal, on_approval_needed),
                timeout=self.goal_timeout)
        except TimeoutError:
            self.timed_out += 1
            logger.error("goal_execution_timeout", goal=goal.id,
                         timeout_s=self.goal_timeout,
                         tool=(goal.assigned_action.tool_name
                               if goal.assigned_action else ""))
            return self._timeout_result(
                goal, f"该步骤执行超过 {self.goal_timeout:.0f} 秒仍未返回，已中止")

    def _timeout_result(self, goal: Goal, detail: str) -> StepResult:
        """超时统一出口 —— 措辞必须让人分清"超时中止"与"做了但失败"。"""
        return StepResult(
            goal_id=goal.id,
            goal_description=goal.description,
            success=False,
            error=(f"{detail}（同批其它步骤不受影响）。"
                   "超时说明这一步卡住了：请检查该步骤的工具/命令是否在等外部响应"
                   "（网络、子进程、审批），必要时改用更小的参数范围重试。"),
        )

    async def _execute_goal_guarded(
        self,
        goal: Goal,
        on_approval_needed: Any = None,
    ) -> StepResult:
        """``_execute_goal`` 的异常屏障 —— 一个目标炸掉不影响同批其它目标。

        并行的价值建立在"单点故障不连坐"之上：没有这层屏障时，批内任一
        ``_execute_goal`` 抛出未捕获异常（工具实现缺陷、超时竞态、审批通道
        断开）会被 ``asyncio.gather`` 升级为整批失败，**已并行的其它目标
        成果一起丢失**，任务直接判败。现在异常就地转成该目标的
        ``StepResult(success=False)``，走与普通失败**完全相同**的后续流程
        （自我纠错 → 回溯 → 报告），其余目标不受影响。
        """
        try:
            return await self._execute_goal(goal, on_approval_needed)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            logger.error("goal_execution_crashed", goal=goal.id,
                         tool=(goal.assigned_action.tool_name
                               if goal.assigned_action else ""),
                         error=reason)
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=False,
                error=f"该步骤执行时抛出未捕获异常（同批其它步骤不受影响）：{reason}",
            )

    def _ready_goals(self, plan: HierarchicalPlan) -> list[Goal]:
        """收集当前所有就绪的叶子目标（PENDING 且依赖已满足），按执行顺序返回。"""
        ready: list[Goal] = []
        for goal_id in plan.execution_order:
            goal = self.hierarchical_planner._find_goal(plan.root_goal, goal_id)
            if goal and goal.status == GoalStatus.PENDING and \
                    self.hierarchical_planner._dependencies_met(goal, plan.root_goal):
                ready.append(goal)
        return ready

    #: 缓存键里用于"只看参数、不看目标"的哨兵（仅供跨目标复用测试使用）
    CACHE_ANY_GOAL = "*"

    @classmethod
    def _cache_key(cls, tool_name: str, params: dict[str, Any],
                   goal_id: str = "") -> str:
        """子任务缓存键 = 目标身份 + 工具名 + 参数。

        **目标身份必须进键**：缓存的本意是"同一个步骤的重试/重复调用别重复读"，
        而不是"不同步骤只要调了同一个工具就算做完"。此前键里只有工具名与参数，
        于是计划里两个语义完全不同的步骤 —— 例如"读配置 A"与"基于配置 A 生成
        报告"，模型恰好都规划成 ``file_read(同一路径)`` —— 第二个会被**直接
        判成功**：它一步都没执行，报告里却记成"已完成"。
        （这个 bug 是在给"总时长预算"写测试时撞出来的：6 个步骤只真跑了 1 次。）
        """
        import json as _json
        try:
            blob = _json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            blob = repr(sorted(params.items()))
        return f"{goal_id}::{tool_name}::{blob}"

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
        #
        # v1.7.0：这道门**必须对所有动作生效**。此前整段包在 `if tool is not None:`
        # 里 —— 工具名写错 / 未注册（`_get_tool` 返回 None）时权限检查连同审批
        # 一起被跳过，动作直接进 dispatch。虽然 dispatch 随后会因找不到工具而失败
        # （不构成执行逃逸），但"没找到工具"恰恰是最需要人工确认的形态之一
        # （可能是模型臆造了一个不存在的高危动作），而且它让审批模式在这条路径上
        # 表现为"配了 ask 也不弹窗"。未注册的工统一按最高风险等级送审。
        from automind.core.types import PermissionTier as _PTier
        tool = self._get_tool(action.tool_name)
        if tool is None:
            logger.warning("unknown_tool_requires_approval", tool=action.tool_name,
                           goal=goal.id)
        check_tier = (tool.permission_tier if tool is not None
                      else _PTier.DANGEROUS)
        decision, reason = self.permissions.check(
            action.tool_name, check_tier, action.parameters
        )
        if decision.value == "deny":
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=False,
                error=f"Permission denied: {reason}",
            )
        if decision.value == "allow" and tool is None:
            # 「全批准」模式下不会走审批，但"批准一个不存在的工具"毫无意义 ——
            # 直接如实失败，别让一个幻影动作被计成"已完成步骤"。
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=False,
                error=(f"工具「{action.tool_name}」不存在或未注册，无法执行该步骤。"),
            )
        if decision.value == "ask_user":
            # 安全修复（v1.4.5）：原条件是 `ask_user and on_approval_needed` ——
            # 没接审批回调时整个判断被**整体跳过**，需要人工确认的操作直接执行了。
            # 现在没有回调就等于问不到人，按拒绝处理。
            # （要无人值守跑，应把审批模式设为「自动」/「全批准」——那样
            #   permissions.check() 不会返回 ask_user，根本走不到这里。）
            if on_approval_needed is None:
                return StepResult(
                    goal_id=goal.id,
                    goal_description=goal.description,
                    success=False,
                    error=(f"{reason}；当前没有可用的审批通道，已按拒绝处理"
                           "（如需无人值守运行，请将审批模式设为「自动」或「全批准」）"),
                )
            try:
                from automind.state.human_loop import ApprovalOutcome
                outcome = ApprovalOutcome.normalize(
                    await on_approval_needed(goal, action))
                approved = outcome.approved
                if approved and outcome.modified:
                    # 「修改后批准」：用用户改过的参数覆盖本步骤的动作参数
                    action.parameters = dict(outcome.arguments or {})
                    logger.info("approval_modified", goal=goal.id,
                                tool=action.tool_name,
                                keys=sorted(action.parameters))
                elif not approved and outcome.comment:
                    reason = outcome.comment
            except Exception as e:
                approved = False   # 审批通道异常一律视为未批准
                reason = f"审批通道异常（{type(e).__name__}）"
            if not approved:
                return StepResult(
                    goal_id=goal.id,
                    goal_description=goal.description,
                    success=False,
                    error=f"User denied the action: {reason}",
                )

        if tool is None:
            # 走到这里说明审批通过了（否则上面已经返回）—— 但工具**没有实现**，
            # 无法执行。如实失败，别把它计成"已完成步骤"。
            return StepResult(
                goal_id=goal.id,
                goal_description=goal.description,
                success=False,
                error=(f"工具「{action.tool_name}」不存在或未注册，无法执行该步骤"
                       "（该动作已按最高风险等级送审，但工具本身不可用）。"),
            )

        # 子任务缓存：SAFE 级只读工具（file_read/web_fetch 等）同参调用直接复用结果，
        # 避免**同一个目标**在重试/纠错重跑时的重复 IO；写类工具绝不缓存。
        # 键里带目标身份，跨目标不复用（见 _cache_key 的说明）。
        cacheable = self.use_cache and tool.permission_tier == _PTier.SAFE
        cache_key = (self._cache_key(action.tool_name, action.parameters, goal.id)
                     if cacheable else "")
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
        #: 最后一次的真实失败原因 —— 不要用一句笼统的
        #: "Failed after N attempts" 把工具报的错盖掉：模型/用户拿不到原因
        #: 就只能盲目重试，而这正是"失败被静默化"的另一种形态。
        last_error = ""
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

                last_error = str(result.error or "").strip() or \
                    f"工具 {action.tool_name} 返回失败（无错误信息）"
                # 失败 → 如果 auto_retry，继续尝试
                if not self.auto_retry:
                    break

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt == self.max_retries - 1:
                    return StepResult(
                        goal_id=goal.id,
                        goal_description=goal.description,
                        success=False,
                        error=last_error,
                        retries=attempt + 1,
                    )

        return StepResult(
            goal_id=goal.id,
            goal_description=goal.description,
            success=False,
            error=(f"重试 {self.max_retries} 次后仍失败：{last_error}"
                   if last_error else f"Failed after {self.max_retries} attempts"),
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
