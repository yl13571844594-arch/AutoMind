"""人机协同 — 关键步骤暂停审批、展示进度、收集反馈。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from automind.core.types import Action, Goal, HierarchicalPlan


class ApprovalAction(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    MODIFY = "modify"
    SKIP = "skip"
    ABORT = "abort"


@dataclass
class ApprovalRequest:
    """审批请求。"""

    goal: Goal
    action: Action
    risk_level: str
    reason: str
    preview: str = ""  # 预览信息


@dataclass
class ApprovalResponse:
    """审批响应。"""

    action: ApprovalAction
    modifications: dict[str, Any] = field(default_factory=dict)
    comment: str = ""


@dataclass
class ProgressDisplay:
    """进度信息。"""

    plan: HierarchicalPlan
    current_step: str = ""
    completed: int = 0
    total: int = 0
    percent: float = 0.0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


class HumanInTheLoop:
    """人机协同接口。

    负责:
        - 在关键步骤暂停并等待人工审批
        - 展示当前计划与进度
        - 收集人工反馈
        - 允许修改计划
    """

    def __init__(
        self,
        approval_callback: Any = None,
        auto_approve_safe: bool = True,
        show_progress_interval: int = 5,
    ) -> None:
        self._approval_callback = approval_callback
        self.auto_approve_safe = auto_approve_safe
        self.show_progress_interval = show_progress_interval
        self._step_count = 0
        self._approval_history: list[tuple[ApprovalRequest, ApprovalResponse]] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """请求人工审批。

        Args:
            request: 审批请求详情。

        Returns:
            审批响应。
        """
        # 安全操作自动批准
        if self.auto_approve_safe and request.risk_level == "safe":
            response = ApprovalResponse(action=ApprovalAction.APPROVE)
            self._approval_history.append((request, response))
            return response

        if self._approval_callback:
            response = await self._approval_callback(request)
        else:
            # 默认: 通过 CLI 询问用户
            response = await self._cli_ask(request)

        self._approval_history.append((request, response))
        return response

    def get_progress_display(self, plan: HierarchicalPlan) -> ProgressDisplay:
        """生成进度展示。"""
        all_goals = [plan.root_goal] + plan.root_goal.all_children()
        total = len(all_goals)
        completed = sum(1 for g in all_goals if g.status.value == "completed")
        failed = sum(1 for g in all_goals if g.status.value == "failed")
        errors = [g.error_context for g in all_goals if g.error_context]

        return ProgressDisplay(
            plan=plan,
            completed=completed,
            total=total,
            percent=round(completed / total * 100, 1) if total else 0,
            errors=errors,
        )

    def get_approval_history(self) -> list[dict[str, Any]]:
        return [
            {
                "goal": req.goal.description,
                "action": req.action.tool_name,
                "risk": req.risk_level,
                "response": resp.action.value,
            }
            for req, resp in self._approval_history
        ]

    @staticmethod
    async def _cli_ask(request: ApprovalRequest) -> ApprovalResponse:
        """通过 CLI 询问用户。"""
        import sys

        print("\n" + "=" * 60)
        print(f"[APPROVAL REQUIRED] {request.risk_level.upper()}")
        print(f"  Goal: {request.goal.description}")
        print(f"  Action: {request.action.tool_name}")
        print(f"  Parameters: {request.action.parameters}")
        print(f"  Reason: {request.reason}")
        if request.preview:
            print(f"  Preview:\n{request.preview}")
        print("=" * 60)

        try:
            choice = input("[A]pprove / [D]eny / [S]kip / [Q]uit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ApprovalResponse(action=ApprovalAction.DENY, comment="User interrupted")

        if choice in ("a", "approve", ""):
            return ApprovalResponse(action=ApprovalAction.APPROVE)
        elif choice in ("d", "deny"):
            return ApprovalResponse(action=ApprovalAction.DENY)
        elif choice in ("s", "skip"):
            return ApprovalResponse(action=ApprovalAction.SKIP)
        elif choice in ("q", "quit", "abort"):
            return ApprovalResponse(action=ApprovalAction.ABORT)
        return ApprovalResponse(action=ApprovalAction.DENY, comment="Invalid choice")

    def should_show_progress(self) -> bool:
        self._step_count += 1
        return self._step_count % self.show_progress_interval == 0
