"""v1.10.0 引擎新能力（社区核心那一半）：多级 / 按角色审批、凭据引用与脱敏。

对应 docs/WORKFLOW_ROADMAP.md 的 R-303、R-305。企业版的存储、身份校验、发布审批、
运行审计与并发配额见 pro/tests/test_workflow_governance.py。
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool, ToolRegistry
from automind.workflow.executor import (
    REDACTED,
    RUN_ABORTED,
    RUN_DRY_RUN,
    RUN_OK,
    STATUS_ABORTED,
    run_workflow,
)
from automind.workflow.loader import WorkflowLoader

ENT = {"workflow_governance", "workflow_parallel", "workflow_triggers"}
SECRET = "s3cr3t-Value-42"


def load(text: str, **kw: Any):
    kw.setdefault("features", set(ENT))
    kw.setdefault("foreach_max", 20)
    kw.setdefault("min_interval", 60)
    return WorkflowLoader(**kw).try_parse(textwrap.dedent(text).lstrip())


def build(text: str, **kw: Any):
    schema, errors, _ = load(text, **kw)
    assert schema is not None, [e.format() for e in errors]
    return schema


def errors_of(text: str, **kw: Any) -> list[str]:
    schema, errors, _ = load(text, **kw)
    assert schema is None, "期望校验失败，但它通过了"
    return [e.format() for e in errors]


class Echo(AbstractTool):
    name = "echo"
    description = "测试用：回显参数；token 以 bad 开头时失败并在错误里带上它"
    parameters = {"type": "object", "properties": {"token": {}, "url": {}}}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kw: Any) -> ToolResult:
        self.calls.append(kw)
        if str(kw.get("token", "")).startswith("bad"):
            return ToolResult(tool_name=self.name, success=False,
                              error=f"认证失败：token={kw.get('token')}")
        return ToolResult(tool_name=self.name, success=True,
                          output={"echo": kw, "note": f"used {kw.get('token')}"})


@pytest.fixture
def echo() -> Echo:
    return Echo()


@pytest.fixture
def reg(echo: Echo) -> ToolRegistry:
    r = ToolRegistry()
    r.register(echo)
    return r


# ═══════════════════════════════════════════════════════════════
# 多级 / 按角色审批（R-303）
# ═══════════════════════════════════════════════════════════════


APPROVAL = """
version: 1
name: 付款
steps:
  - id: approve
    type: human
    prompt: 付款 100 元
    approvers: [role:finance, role:manager]
    mode: all
  - id: pay
    type: tool
    tool: echo
    args: {url: pay}
"""


class TestApproverSchema:
    def test_valid(self) -> None:
        step = build(APPROVAL).steps[0]
        assert step.approvers == ["role:finance", "role:manager"]
        assert step.approval_mode == "all"
        assert step.as_dict()["approvers"] == ["role:finance", "role:manager"]
        assert "role:finance → role:manager" in build(APPROVAL).summarize()

    def test_community_blocked(self) -> None:
        assert any("approvers / mode" in m and "需要企业版" in m
                   for m in errors_of(APPROVAL, features=set()))

    @pytest.mark.parametrize(("old", "new", "needle"), [
        ("[role:finance, role:manager]", "[finance]", "写法不对"),
        ("[role:finance, role:manager]", "[role:finance, role:finance]", "审批人重复"),
        ("[role:finance, role:manager]", "[]", "非空列表"),
        ("mode: all", "mode: majority", "mode 只能是 all"),
        ("[role:finance, role:manager]", "[group:x]", "写法不对"),
    ])
    def test_bad(self, old: str, new: str, needle: str) -> None:
        assert any(needle in m for m in errors_of(APPROVAL.replace(old, new)))

    def test_mode_without_approvers_warns(self) -> None:
        text = APPROVAL.replace("    approvers: [role:finance, role:manager]\n", "")
        schema, _, warnings = load(text)
        assert schema is not None and any("mode 只在指定了审批人时有意义" in w.message
                                          for w in warnings)


def scripted_approval(answers: list[dict[str, Any]]):
    calls: list[dict[str, Any]] = []

    async def _approval(step: Any, prompt: str, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return answers[len(calls) - 1]

    _approval.calls = calls                                # type: ignore[attr-defined]
    return _approval


class TestMultiLevelApproval:
    async def test_all_levels_in_order(self, reg, echo) -> None:
        approval = scripted_approval([
            {"approved": True, "approver": {"user": "fin", "role": "finance"}},
            {"approved": True, "approver": {"user": "boss", "role": "manager"}}])
        run = await run_workflow(build(APPROVAL), {}, registry=reg, approval=approval)
        assert run.status == RUN_OK and len(echo.calls) == 1
        assert [c["required"] for c in approval.calls] == [["role:finance"], ["role:manager"]]
        assert [c["level"] for c in approval.calls] == [1, 2]
        approvals = run.steps[0].detail["approvals"]
        assert [a["by"]["user"] for a in approvals] == ["fin", "boss"]
        assert all(a["approved"] and a["at"] for a in approvals)

    async def test_rejection_at_level_two_stops(self, reg, echo) -> None:
        approval = scripted_approval([
            {"approved": True, "approver": {"user": "fin", "role": "finance"}},
            {"approved": False, "comment": "预算不够", "approver": {"user": "boss"}}])
        run = await run_workflow(build(APPROVAL), {}, registry=reg, approval=approval)
        step = run.steps[0]
        assert step.status == STATUS_ABORTED and run.status == RUN_ABORTED
        assert "第 2/2 级" in step.error and "role:manager" in step.error
        assert "预算不够" in step.error and echo.calls == []

    async def test_any_mode_asks_once(self, reg, echo) -> None:
        approval = scripted_approval([{"approved": True, "approver": {"user": "boss"}}])
        run = await run_workflow(build(APPROVAL.replace("mode: all", "mode: any")), {},
                                 registry=reg, approval=approval)
        assert run.status == RUN_OK and len(approval.calls) == 1
        assert approval.calls[0]["required"] == ["role:finance", "role:manager"]

    async def test_plain_human_step_keeps_old_signature(self, reg) -> None:
        """没写 approvers 的 human 步骤：审批回调仍按 (step, prompt) 调用（向后兼容）。"""
        seen: list[tuple] = []

        async def old_style(step: Any, prompt: str) -> bool:
            seen.append((step.id, prompt))
            return True

        text = APPROVAL.replace("    approvers: [role:finance, role:manager]\n", "") \
            .replace("    mode: all\n", "")
        run = await run_workflow(build(text, features=set()), {}, registry=reg,
                                 approval=old_style)
        assert run.status == RUN_OK and seen == [("approve", "付款 100 元")]
        assert len(run.steps[0].detail["approvals"]) == 1

    async def test_dry_run_previews_approvers(self, reg) -> None:
        run = await run_workflow(build(APPROVAL), {}, registry=reg, dry_run=True)
        assert run.status == RUN_DRY_RUN
        assert run.steps[0].detail["approvers"] == ["role:finance", "role:manager"]


# ═══════════════════════════════════════════════════════════════
# 凭据（R-305）
# ═══════════════════════════════════════════════════════════════


SECRETS = """
version: 1
name: 调接口
steps:
  - id: call
    type: tool
    tool: echo
    args:
      token: "{{ secrets.API_TOKEN }}"
      url: "https://x/?k={{ secrets.API_TOKEN }}"
  - id: after
    type: tool
    tool: echo
    args: {url: "{{ steps.call.output.note }}"}
"""


class TestSecretSchema:
    def test_valid_and_names(self) -> None:
        assert build(SECRETS).secret_names() == ["API_TOKEN"]

    def test_community_blocked(self) -> None:
        assert any("凭据引用" in m and "需要企业版" in m for m in errors_of(SECRETS, features=set()))

    @pytest.mark.parametrize("field", [
        "  - id: ask\n    type: llm\n    prompt: \"key {{ secrets.API_TOKEN }}\"\n",
        "  - id: ask\n    type: human\n    prompt: \"key {{ secrets.API_TOKEN }}\"\n",
        "  - id: ask\n    type: agent\n    goal: \"key {{ secrets.API_TOKEN }}\"\n"
        "    allowed_tools: [echo]\n",
    ])
    def test_only_tool_args(self, field: str) -> None:
        text = SECRETS + field
        assert any("只能用在 tool 步骤的 args 里" in m for m in errors_of(text))

    @pytest.mark.parametrize("ref", ["secrets.api_token", "secrets.A.B", "secrets"])
    def test_bad_reference(self, ref: str) -> None:
        text = SECRETS.replace("secrets.API_TOKEN }}\"\n      url", f"{ref} }}}}\"\n      url")
        assert any("凭据引用写法" in m for m in errors_of(text))


class TestSecretRedaction:
    async def test_tool_gets_value_report_does_not(self, reg, echo) -> None:
        events: list[tuple[str, dict]] = []
        snaps: list[dict] = []
        run = await run_workflow(build(SECRETS), {}, registry=reg,
                                 secrets={"API_TOKEN": SECRET},
                                 on_event=lambda e, p: events.append((e, p)),
                                 on_checkpoint=snaps.append)
        assert run.status == RUN_OK
        assert echo.calls[0]["token"] == SECRET                 # 工具拿到的是真值
        assert echo.calls[1]["url"] == f"used {SECRET}"         # 下游步骤照常可用
        blob = run.to_json(include_outputs=True) + repr(events) + repr(snaps)
        assert SECRET not in blob, "凭据明文出现在报告 / 事件 / 快照里"
        assert REDACTED in run.steps[0].as_dict()["detail"]["args"]["token"]

    async def test_error_messages_are_redacted(self, reg, echo, caplog) -> None:
        run = await run_workflow(build(SECRETS), {}, registry=reg,
                                 secrets={"API_TOKEN": "bad-" + SECRET})
        step = run.steps[0]
        assert step.status == STATUS_ABORTED
        assert SECRET not in step.error and REDACTED in step.error
        assert SECRET not in caplog.text

    async def test_missing_secret_fails_clearly(self, reg, echo) -> None:
        run = await run_workflow(build(SECRETS), {}, registry=reg, secrets={})
        assert run.steps[0].status == STATUS_ABORTED
        assert "凭据 API_TOKEN 未配置" in run.steps[0].error and echo.calls == []

    async def test_dry_run_never_needs_values(self, reg, echo) -> None:
        run = await run_workflow(build(SECRETS), {}, registry=reg, dry_run=True)
        assert run.status == RUN_DRY_RUN
        assert run.steps[0].detail["args"]["token"] == "⟨凭据：API_TOKEN⟩"

    async def test_short_values_are_not_used_for_redaction(self, reg) -> None:
        run = await run_workflow(build(SECRETS), {}, registry=reg, secrets={"API_TOKEN": "ab"})
        assert run.status == RUN_OK                             # 太短的值不当成脱敏目标
