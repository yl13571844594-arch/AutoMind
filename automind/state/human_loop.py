"""人机协同 — 关键步骤暂停审批、展示进度、收集反馈。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from automind.core.types import Action, Goal, HierarchicalPlan


def _coerce_like(old: Any, raw: str) -> Any:
    """把用户输入的字符串按原值的类型解释。

    终端里输入的一切都是字符串；若原参数是 `timeout=30`（int），直接存 `"60"`
    会让工具拿到字符串类型而行为异常。这里按原值类型做一次最小转换，
    转不动就原样保留字符串（宁可保留原文，也不猜错类型）。
    """
    if isinstance(old, bool):
        return raw.strip().lower() in ("1", "true", "yes", "y", "on", "是", "真")
    if isinstance(old, int) and not isinstance(old, bool):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(old, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    if isinstance(old, (list, dict)):
        import json
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


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
class ApprovalOutcome:
    """审批回调的归一化结果。

    审批回调（Web 注入的 `agent.approval_callback` / 执行器的 `approval_cb`）
    历史上只返回 `bool`，因此 `ApprovalAction.MODIFY`「改参数后批准」虽然在枚举
    里定义了、`ApprovalResponse.modifications` 字段也留了，却**在整条执行链路上
    无从表达** —— 前端只有批准/拒绝两个按钮，CLI 只有 A/D/S/Q。

    现在回调可以返回两种形式，`normalize()` 统一成本类型：
      · `bool`  —— 老形式，批准 / 拒绝；
      · `dict`  —— `{"approved": bool, "arguments": {...}, "comment": str}`，
        `arguments` 非空即表示用户改过参数，执行器应改用这份参数。
    """

    approved: bool
    #: 非 None 表示用户修改过工具参数，执行时应以此为准
    arguments: dict[str, Any] | None = None
    comment: str = ""

    @property
    def modified(self) -> bool:
        return self.arguments is not None

    def __bool__(self) -> bool:
        """让"未批准"在布尔上下文里为假。

        这是安全兜底：dataclass 实例默认恒为真，若某处仍写着
        `if not approved:`（历史代码、第三方扩展、未来新增的调用点），
        一个"拒绝"的 outcome 会被当成"批准"放行 —— 审批控制彻底失效。
        绑定 __bool__ 后，即便调用方没改成读 `.approved`，也仍然 fail-closed。
        """
        return self.approved

    @classmethod
    def normalize(cls, result: Any) -> ApprovalOutcome:
        """把回调的返回值归一化；无法识别的一律按未批准处理（fail-closed）。"""
        if isinstance(result, cls):
            return result
        if isinstance(result, dict):
            args = result.get("arguments")
            return cls(
                approved=bool(result.get("approved")),
                arguments=dict(args) if isinstance(args, dict) and args else None,
                comment=str(result.get("comment") or ""),
            )
        return cls(approved=bool(result))


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

        print("\n" + "=" * 60)
        print(f"[APPROVAL REQUIRED] {request.risk_level.upper()}")
        print(f"  Goal: {request.goal.description}")
        print(f"  Action: {request.action.tool_name}")
        print(f"  Parameters: {request.action.parameters}")
        print(f"  Reason: {request.reason}")
        if request.preview:
            print(f"  Preview:\n{request.preview}")
        print("=" * 60)

        # 非交互环境（服务端 / 桌面 GUI / CI）根本没有人在终端前面。
        # 此时不能去 input()，更不能"没人回答就放行"——直接拒绝并说明原因。
        if not (sys.stdin and sys.stdin.isatty()):
            return ApprovalResponse(
                action=ApprovalAction.DENY,
                comment="无交互终端，无法获得人工批准（按拒绝处理）")

        try:
            choice = input(
                "[A]pprove 批准 / [M]odify 改参数后批准 / "
                "[D]eny 拒绝 / [S]kip 跳过 / [Q]uit 中止: ").strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            return ApprovalResponse(action=ApprovalAction.DENY, comment="User interrupted")

        # 直接回车**不再等于批准**：安全提示的默认值只能是"不批准"，
        # 否则一路回车就把所有敏感操作放行了。
        if choice in ("a", "approve"):
            return ApprovalResponse(action=ApprovalAction.APPROVE)
        elif choice in ("m", "modify"):
            return HumanInTheLoop._cli_modify(request)
        elif choice in ("d", "deny"):
            return ApprovalResponse(action=ApprovalAction.DENY)
        elif choice in ("s", "skip"):
            return ApprovalResponse(action=ApprovalAction.SKIP)
        elif choice in ("q", "quit", "abort"):
            return ApprovalResponse(action=ApprovalAction.ABORT)
        return ApprovalResponse(action=ApprovalAction.DENY, comment="Invalid choice")

    @staticmethod
    def _cli_modify(request: ApprovalRequest) -> ApprovalResponse:
        """逐个参数询问新值，回车保留原值；全程无输入则按拒绝处理。

        比让用户手敲一整串 JSON 友好得多 —— 需要改的往往只是其中一个参数
        （典型场景：把 `rm -rf /tmp/x` 改成更小的范围、把写入路径挪个位置）。
        """
        params = dict(getattr(request.action, "parameters", {}) or {})
        if not params:
            print("  该操作没有可修改的参数，按拒绝处理。")
            return ApprovalResponse(action=ApprovalAction.DENY,
                                    comment="无参数可修改")

        print("\n  逐项修改（直接回车＝保留原值，输入 !cancel 放弃修改）：")
        new_params: dict[str, Any] = {}
        for key, old in params.items():
            shown = str(old)
            if len(shown) > 200:
                shown = shown[:200] + "…"
            try:
                raw = input(f"    {key} = {shown}\n    新值> ").strip()
            except (EOFError, KeyboardInterrupt, OSError):
                return ApprovalResponse(action=ApprovalAction.DENY,
                                        comment="User interrupted")
            if raw == "!cancel":
                return ApprovalResponse(action=ApprovalAction.DENY,
                                        comment="用户放弃修改")
            new_params[key] = _coerce_like(old, raw) if raw else old

        changed = {k: v for k, v in new_params.items() if v != params.get(k)}
        if not changed:
            print("  没有任何改动，按原参数批准。")
            return ApprovalResponse(action=ApprovalAction.APPROVE)
        print(f"  已修改 {len(changed)} 项：{', '.join(changed)}")
        return ApprovalResponse(action=ApprovalAction.MODIFY,
                                modifications=new_params,
                                comment=f"用户修改了 {len(changed)} 个参数")

    def should_show_progress(self) -> bool:
        self._step_count += 1
        return self._step_count % self.show_progress_interval == 0
