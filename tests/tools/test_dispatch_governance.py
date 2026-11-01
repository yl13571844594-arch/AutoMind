"""分派级治理闸门（v1.11.19）回归测试。

盯住"审批旁路"这个安全缺口：ReAct / Plan 在执行器里逐次审批，但
workflow / skill / direct 三条通道不经过执行器 —— 高危工具在 ``ask`` 模式下
也能被静默执行。本文件验证：

1. 装了闸门 + 无人值守来源时，有效 DANGEROUS 被拒且**工具真的没执行**；
2. 非高危放行；``approval_mode="approve_all"`` 时放行；
3. react/plan 来源**不二次审批**（执行器已批准），但执行记录照常落盘；
4. 裸 Registry（没装闸门）行为零变化、也不写账本；
5. 审批模式读的是**实时值**（中途切换立即生效）。

全部用例用临时目录隔离账本，不污染仓库 .automind。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from automind.core import governance
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool, ToolRegistry
from automind.tools.permissions import PermissionEngine

# ═══════════════════════════════════════════════════════════
# 夹具与桩工具
# ═══════════════════════════════════════════════════════════


def _writable_dir(prefix: str = "automind-gov-") -> Path:
    """建一个默认权限的可写临时目录（与 tests/core/conftest 同一理由：

    pytest 的 tmp_path 用 ``mkdir(0o700)``，在 Windows 沙箱里会变成只读，
    账本写入会"假失败"；这里用 ``os.mkdir`` 的默认权限，验证的是代码而不是环境。
    """
    base = Path(tempfile.gettempdir())
    for _ in range(100):
        candidate = base / f"{prefix}{os.urandom(4).hex()}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError:                       # 系统临时目录不可写 → 退回工作区
            candidate = Path(__file__).resolve().parents[2] / f".test_tmp_{os.urandom(3).hex()}"
            candidate.mkdir(parents=True)
        return candidate
    raise RuntimeError("无法创建可写的临时目录")


@pytest.fixture(autouse=True)
def _isolated_ledger() -> Iterator[Path]:
    root = _writable_dir("automind-gov-ledger-")
    governance.reset_for_tests(root / "governance", enabled=True)
    yield root
    governance.reset_for_tests(root / "governance-teardown", enabled=True)
    shutil.rmtree(root, ignore_errors=True)


class _ProbeTool(AbstractTool):
    """记调用次数的探针工具；等级由构造参数决定。"""

    def __init__(self, name: str, tier: PermissionTier) -> None:
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.permission_tier = tier
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return ToolResult(tool_name=self.name, success=True, output={"ran": True})


class _BoomTool(AbstractTool):
    name = "boom_probe"
    description = "执行即抛异常"
    parameters = {"type": "object", "properties": {}}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        raise RuntimeError("模拟工具内部崩溃")


def _governed_registry(engine: PermissionEngine) -> tuple[ToolRegistry, _ProbeTool, _ProbeTool]:
    """装好闸门的注册表：danger_probe（DANGEROUS）+ safe_probe（SAFE）。"""
    registry = ToolRegistry()
    danger = _ProbeTool("danger_probe", PermissionTier.DANGEROUS)
    safe = _ProbeTool("safe_probe", PermissionTier.SAFE)
    registry.register(danger)
    registry.register(safe)
    registry.set_governor(
        governance.make_unattended_governor(
            engine, lambda: engine.approval_mode))
    return registry, danger, safe


def _engine(mode: str = "auto") -> PermissionEngine:
    return PermissionEngine(approval_mode=mode)


def _decisions(*, source: str = "") -> list[dict]:
    return governance.tail(200, kind="decision", source=source)


# ═══════════════════════════════════════════════════════════
# 1. direct 通道：高危拒绝、非高危放行
# ═══════════════════════════════════════════════════════════


class TestDirectChannel:
    async def test_dangerous_is_denied_and_not_executed(self):
        registry, danger, _safe = _governed_registry(_engine("auto"))
        result = await registry.dispatch("danger_probe", arg=1)

        assert result.success is False
        assert danger.calls == 0, "被治理拦截的工具绝不能被执行"
        assert "运行底座治理拦截" in (result.error or "")
        assert result.metadata.get("governance_denied") is True
        assert result.metadata.get("governance_source") == "direct"

        decisions = _decisions()
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "deny_unattended"
        assert decisions[0]["source"] == "direct"
        assert decisions[0]["tier"] == "dangerous"
        assert decisions[0]["risk"] >= 80
        assert governance.tail(10, kind="execution") == [], "被拒的调用没有执行记录"

    async def test_safe_tool_is_allowed_and_recorded(self):
        registry, _danger, safe = _governed_registry(_engine("auto"))
        result = await registry.dispatch("safe_probe", arg=2)

        assert result.success is True
        assert safe.calls == 1
        decisions = _decisions()
        assert [d["decision"] for d in decisions] == ["allow_unattended"]
        executions = governance.tail(10, kind="execution")
        assert len(executions) == 1
        assert executions[0]["tool"] == "safe_probe"
        assert executions[0]["success"] is True
        assert executions[0]["source"] == "direct"

    async def test_sensitive_tool_is_allowed(self):
        """SENSITIVE 有效等级放行（只有 DANGEROUS 需要人工批准）。"""
        registry = ToolRegistry()
        sens = _ProbeTool("sens_probe", PermissionTier.SENSITIVE)
        registry.register(sens)
        engine = _engine("auto")
        registry.set_governor(
            governance.make_unattended_governor(engine, lambda: engine.approval_mode))

        result = await registry.dispatch("sens_probe")
        assert result.success is True
        assert sens.calls == 1
        decisions = _decisions()
        assert [d["decision"] for d in decisions] == ["allow_unattended"]
        assert decisions[0]["tier"] == "sensitive"
        assert 40 <= decisions[0]["risk"] < 80

    async def test_approve_all_allows_dangerous(self):
        engine = _engine("approve_all")
        registry, danger, _safe = _governed_registry(engine)
        result = await registry.dispatch("danger_probe")

        assert result.success is True
        assert danger.calls == 1
        assert [d["decision"] for d in _decisions()] == ["allow_unattended"]

    async def test_ask_mode_still_denies_unattended_dangerous(self):
        """ask/auto 模式下高危本该弹窗问人；无人值守通道问不到人 = 拒绝。"""
        for mode in ("ask", "auto"):
            registry, danger, _safe = _governed_registry(_engine(mode))
            result = await registry.dispatch("danger_probe")
            assert result.success is False, f"{mode} 模式不应放行无人值守高危操作"
            assert danger.calls == 0

    async def test_risk_escalation_by_params(self):
        """工具声明 SAFE，但命令本身危险 → 有效等级被抬到 DANGEROUS → 拒绝。"""
        registry = ToolRegistry()
        tool = _ProbeTool("shell_probe", PermissionTier.SAFE)
        registry.register(tool)
        engine = _engine("auto")
        registry.set_governor(
            governance.make_unattended_governor(engine, lambda: engine.approval_mode))

        result = await registry.dispatch("shell_probe", command="rm -rf /tmp/x")
        assert result.success is False
        assert tool.calls == 0
        assert _decisions()[0]["risk"] >= 80

        # 同一条命令在 approve_all 下放行
        engine.approval_mode = "approve_all"
        assert (await registry.dispatch("shell_probe", command="rm -rf /tmp/x")).success

    async def test_bare_registry_behaviour_unchanged(self):
        """没装闸门的裸 Registry（单测 / 库调用）完全保持既有行为，且不写账本。"""
        registry = ToolRegistry()
        danger = _ProbeTool("danger_probe", PermissionTier.DANGEROUS)
        registry.register(danger)
        result = await registry.dispatch("danger_probe")

        assert result.success is True
        assert danger.calls == 1
        assert governance.tail(10) == [], "没有安装闸门就不该写治理账本"
        assert governance.summary()["total"] == 0


# ═══════════════════════════════════════════════════════════
# 2. workflow / skill 通道同样受管；react / plan 不复审
# ═══════════════════════════════════════════════════════════


class TestChannels:
    async def test_workflow_source_is_governed(self):
        registry, danger, _safe = _governed_registry(_engine("auto"))
        token = governance.bind_source("workflow")
        try:
            result = await registry.dispatch("danger_probe")
        finally:
            governance.reset_source(token)

        assert result.success is False
        assert danger.calls == 0
        assert result.metadata.get("governance_source") == "workflow"
        assert _decisions()[0]["source"] == "workflow"

    async def test_skill_source_is_governed(self):
        registry, danger, _safe = _governed_registry(_engine("auto"))
        token = governance.bind_source("skill")
        try:
            result = await registry.dispatch("danger_probe")
        finally:
            governance.reset_source(token)

        assert result.success is False
        assert danger.calls == 0
        assert _decisions()[0]["source"] == "skill"

    async def test_react_source_skips_gate_but_records_execution(self):
        """ReAct 已在执行器里审批过：dispatch 不再二次审批，但要留执行记录。"""
        registry, danger, _safe = _governed_registry(_engine("ask"))
        token = governance.bind_source("react")
        try:
            result = await registry.dispatch("danger_probe")
        finally:
            governance.reset_source(token)

        assert result.success is True, "react 来源不能被执行器审批过的调用再次拦截"
        assert danger.calls == 1
        assert _decisions() == [], "是否批准由 ReAct 的 _gate 记账，dispatch 不重复记"
        executions = governance.tail(10, kind="execution")
        assert len(executions) == 1
        assert executions[0]["source"] == "react"
        assert executions[0]["tier"] == "dangerous"

    async def test_plan_source_skips_gate(self):
        registry, danger, _safe = _governed_registry(_engine("ask"))
        token = governance.bind_source("plan")
        try:
            result = await registry.dispatch("danger_probe")
        finally:
            governance.reset_source(token)
        assert result.success is True and danger.calls == 1
        assert governance.tail(10, kind="execution")[0]["source"] == "plan"


# ═══════════════════════════════════════════════════════════
# 3. 实时审批模式 / 失败执行记账 / 公开权限包装
# ═══════════════════════════════════════════════════════════


class TestLiveModeAndFailures:
    async def test_approval_mode_is_read_live(self):
        engine = _engine("auto")
        registry, danger, _safe = _governed_registry(engine)
        assert (await registry.dispatch("danger_probe")).success is False
        assert danger.calls == 0

        engine.approval_mode = "approve_all"        # 不重装闸门，直接改
        assert (await registry.dispatch("danger_probe")).success is True
        assert danger.calls == 1

        engine.approval_mode = "auto"
        assert (await registry.dispatch("danger_probe")).success is False
        assert danger.calls == 1, "切回 auto 后必须重新拦截"

    async def test_failed_execution_is_recorded(self):
        registry = ToolRegistry()
        boom = _BoomTool()
        registry.register(boom)
        engine = _engine("auto")
        registry.set_governor(
            governance.make_unattended_governor(engine, lambda: engine.approval_mode))

        result = await registry.dispatch("boom_probe")
        assert result.success is False
        assert boom.calls == 1
        executions = governance.tail(10, kind="execution")
        assert len(executions) == 1
        assert executions[0]["success"] is False
        assert "模拟工具内部崩溃" in executions[0]["error"]

    async def test_validation_failure_does_not_reach_governor(self):
        """参数校验失败在闸门之前就返回：不该消耗一次治理决策。"""
        # 声明了 required 参数，构造一次"缺参数"的调用
        reg = ToolRegistry()
        tool = _ProbeTool("typed_probe", PermissionTier.DANGEROUS)
        tool.parameters = {"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": ["path"]}
        reg.register(tool)
        engine = _engine("auto")
        reg.set_governor(
            governance.make_unattended_governor(engine, lambda: engine.approval_mode))
        result = await reg.dispatch("typed_probe")          # 缺 path
        assert result.success is False
        assert tool.calls == 0
        assert _decisions() == [], "没执行也没审批的调用不该产生治理决策"


class TestPermissionPublicWrappers:
    def test_assess_risk_matches_existing_check_semantics(self):
        engine = PermissionEngine(approval_mode="auto")
        risk_safe = engine.assess_risk("file_read", PermissionTier.SAFE, {})
        assert risk_safe == engine._assess_risk("file_read", PermissionTier.SAFE, {})
        assert risk_safe == 10

        risky = engine.assess_risk("terminal", PermissionTier.SENSITIVE,
                                   {"command": "rm -rf /"})
        assert risky >= 80, "危险命令必须把风险抬到 DANGEROUS 区间"

    def test_effective_tier_thresholds(self):
        engine = PermissionEngine()
        assert engine.effective_tier(10) is PermissionTier.SAFE
        assert engine.effective_tier(50) is PermissionTier.SENSITIVE
        assert engine.effective_tier(95) is PermissionTier.DANGEROUS

    def test_public_wrappers_do_not_append_audit_entries(self):
        """公开包装只读不写：不能因为治理侧调用而污染权限审计日志。"""
        engine = PermissionEngine()
        before = len(engine.audit_log)
        engine.assess_risk("file_read", PermissionTier.SAFE, {})
        engine.effective_tier(10)
        assert len(engine.audit_log) == before
