"""权限系统 — 分级授权、风险评分、审计日志。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automind.core.types import PermissionDecision, PermissionTier


@dataclass
class PermissionAuditEntry:
    """权限审计条目。"""

    timestamp: float
    tool_name: str
    params: dict[str, Any]
    decision: PermissionDecision
    tier: PermissionTier
    reason: str
    risk_score: int


@dataclass
class PermissionPolicy:
    """权限策略 — 定义哪些操作属于哪个等级。"""

    safe_patterns: list[str] = field(default_factory=lambda: [
        r"^ls\b", r"^dir\b", r"^cat\b", r"^type\b", r"^echo\b", r"^pwd\b",
        r"^whoami\b", r"^mkdir\b", r"^cd\b", r"^cp\b", r"^mv\b", r"^touch\b",
        r"^git\s+status\b", r"^git\s+log\b", r"^git\s+diff\b", r"^git\s+branch\b",
        r"^git\s+init\b", r"^git\s+add\b",
        r"^python\s+--version\b", r"^python\s+-V\b",
        r"^pip\s+list\b", r"^pip\s+show\b", r"^pip\s+freeze\b",
        r"^which\b", r"^where\b", r"^whereis\b",
        r"^node\s+--version\b", r"^npm\s+list\b",
        r"^dir\b", r"^find\b", r"^grep\b", r"^wc\b", r"^head\b", r"^tail\b",
    ])
    sensitive_patterns: list[str] = field(default_factory=lambda: [
        r"^pip\s+install\b", r"^npm\s+install\b", r"^git\s+commit\b",
        r"^git\s+push\b(?!\s+--force)", r"^git\s+checkout\b",
        r"^python\s+-m\s+pytest\b", r"^npm\s+test\b",
    ])
    dangerous_patterns: list[str] = field(default_factory=lambda: [
        r"rm\s+-rf\b", r"rm\s+-r\b", r"sudo\b", r"chmod\b", r"chown\b",
        r">\s*/dev/", r"mkfs\.", r"dd\s+if=",
        r"git\s+push\s+--force\b", r"git\s+reset\s+--hard\b",
        r"docker\s+rm\b", r"docker\s+system\s+prune\b",
        r"DROP\s+TABLE", r"DELETE\s+FROM",
        r":\(\)\s*\{\s*:\|:&\s*\};:",  # fork bomb
    ])
    allowed_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    require_approval_for_tier: PermissionTier = PermissionTier.SENSITIVE
    auto_approve_safe: bool = True


class PermissionEngine:
    """权限引擎 — 评估工具调用风险并做出授权决策。"""

    # 审批模式:
    #   "ask"         — 询问：除只读(safe)外，每次工具调用前都请求人工批准
    #   "auto"        — 自动：自动批准普通/低风险工具，仅高危操作需确认（默认）
    #   "approve_all" — 全批准：跳过所有工具权限审批（自主运行，慎用）
    APPROVAL_MODES = ("ask", "auto", "approve_all")

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        project_root: str | Path = ".",
        approval_mode: str = "auto",
    ) -> None:
        self.policy = policy or PermissionPolicy()
        self.project_root = str(Path(project_root).resolve())
        self.audit_log: list[PermissionAuditEntry] = []
        self.approval_mode = approval_mode if approval_mode in self.APPROVAL_MODES else "auto"

    def check(
        self,
        tool_name: str,
        tool_tier: PermissionTier,
        params: dict[str, Any] | None = None,
    ) -> tuple[PermissionDecision, str]:
        """检查工具调用是否允许。

        Args:
            tool_name: 工具名称。
            tool_tier: 工具自身的权限等级。
            params: 工具参数 (用于详细检查)。

        Returns:
            (决策, 原因) 元组。
        """
        import time
        params = params or {}

        # 重评估风险
        risk = self._assess_risk(tool_name, tool_tier, params)
        effective_tier = self._effective_tier(risk)

        # 决策 — 遵循 deny > ask > allow 的分级门控，并受 approval_mode 影响
        mode = self.approval_mode

        if mode == "approve_all":
            # 全批准：跳过所有审批（仍记录审计）
            decision = PermissionDecision.ALLOW
            reason = f"全批准模式：自动放行 (risk={risk})"
        elif mode == "ask":
            # 询问：只读(safe)直接放行，其余一律请求批准
            if effective_tier == PermissionTier.SAFE:
                decision = PermissionDecision.ALLOW
                reason = "只读/安全操作，自动放行"
            else:
                decision = PermissionDecision.ASK_USER
                reason = f"询问模式：{effective_tier.value} 操作 (risk={risk}) 需人工批准"
        else:
            # auto（默认）：普通/低风险放行，高危请求确认
            if effective_tier == PermissionTier.SAFE:
                decision = PermissionDecision.ALLOW
                reason = "安全操作，自动放行"
            elif effective_tier == PermissionTier.SENSITIVE:
                if risk < 60:
                    decision = PermissionDecision.ALLOW
                    reason = f"普通操作 (risk={risk})，自动放行"
                else:
                    decision = PermissionDecision.ASK_USER
                    reason = f"较敏感操作 (risk={risk}) 需确认"
            else:  # DANGEROUS
                decision = PermissionDecision.ASK_USER
                reason = f"高危操作 (risk={risk}) 需明确批准"

        entry = PermissionAuditEntry(
            timestamp=time.time(),
            tool_name=tool_name,
            params=params,
            decision=decision,
            tier=effective_tier,
            reason=reason,
            risk_score=risk,
        )
        self.audit_log.append(entry)
        return decision, reason

    def preflight(self, command: str) -> PermissionTier:
        """对命令字符串进行预检，返回风险等级。"""
        for pattern in self.policy.dangerous_patterns:
            if re.search(pattern, command):
                return PermissionTier.DANGEROUS
        for pattern in self.policy.sensitive_patterns:
            if re.search(pattern, command):
                return PermissionTier.SENSITIVE
        for pattern in self.policy.safe_patterns:
            if re.search(pattern, command):
                return PermissionTier.SAFE
        return PermissionTier.SENSITIVE  # 默认为敏感

    def check_path(self, path: str | Path) -> bool:
        """检查文件路径是否在允许范围内。

        B-16 修复：此前只检查 denied_paths，完全忽略 allowed_paths 白名单。
        现在：配置了白名单时路径必须落在其中之一之内，随后再过滤黑名单。
        """
        resolved = Path(path).resolve()
        # 白名单门控（仅在配置了 allowed_paths 时生效）
        if self.policy.allowed_paths:
            in_allowed = False
            for allowed in self.policy.allowed_paths:
                ap = Path(allowed).resolve()
                if resolved == ap or ap in resolved.parents:
                    in_allowed = True
                    break
            if not in_allowed:
                return False
        # 黑名单
        for denied in self.policy.denied_paths:
            dp = Path(denied).resolve()
            if resolved == dp or dp in resolved.parents:
                return False
        return True

    def _assess_risk(
        self,
        tool_name: str,
        tool_tier: PermissionTier,
        params: dict[str, Any],
    ) -> int:
        """评估操作风险分数 (0-100)。"""
        base_risk = {
            PermissionTier.SAFE: 10,
            PermissionTier.SENSITIVE: 50,
            PermissionTier.DANGEROUS: 90,
        }.get(tool_tier, 50)

        # 命令行额外审查
        command = params.get("command", params.get("cmd", ""))
        if command:
            tier = self.preflight(str(command))
            if tier == PermissionTier.DANGEROUS:
                base_risk = max(base_risk, 95)
            elif tier == PermissionTier.SENSITIVE:
                base_risk = max(base_risk, 60)

        # 路径检查
        path = params.get("path", params.get("file_path", ""))
        if path and not self.check_path(path):
            base_risk = 100

        return base_risk

    @staticmethod
    def _effective_tier(risk: int) -> PermissionTier:
        if risk >= 80:
            return PermissionTier.DANGEROUS
        if risk >= 40:
            return PermissionTier.SENSITIVE
        return PermissionTier.SAFE
