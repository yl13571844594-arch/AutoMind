"""v1.11.16 工作流自动化强化：步骤条件 / 失败降级 / 整轮超时 / token 统计 / 触发器可靠性。

对应 docs/WORKFLOW_ROADMAP.md 的 v1.11.16（R-401 ~ R-405）。本文件只测**社区核心**
与可独立测的专业版触发器逻辑（用假 ctx，不启动服务端）：

* R-401 步骤级条件执行 ``if`` / ``unless``（含 foreach / parallel 子步骤、dry-run）；
* R-402 失败降级分支 ``on_failure: fallback(<步骤id>)``（真实执行 + 加载期校验）；
* R-403 工作流级硬超时 ``timeout_seconds``（真实中止，不是只写字段）；
* R-404 token 统计汇总进运行报告（llm / agent 两类步骤）；
* R-405 触发器可靠性：cron 错过补偿策略 + webhook 重放保护（专业版，假 ctx 驱动）。
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from typing import Any

import pytest

from automind.core.types import LLMResponse, PermissionTier, ToolResult
from automind.tools.base import AbstractTool, ToolRegistry
from automind.workflow.executor import (
    RUN_ABORTED,
    RUN_DRY_RUN,
    RUN_OK,
    RUN_PARTIAL,
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    run_workflow,
)
from automind.workflow.loader import WorkflowLoader

PRO = {"workflow_parallel", "workflow_triggers", "workflow_history"}


# ═══════════════════════════════════════════════════════════════
# 公共夹具
# ═══════════════════════════════════════════════════════════════


class Probe(AbstractTool):
    """按 value 定制延迟 / 失败的假工具；记录调用顺序。"""

    name = "probe"
    description = "测试用"
    parameters = {"type": "object",
                  "properties": {"value": {}, "delay": {"type": "number"}}}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail: dict[str, int] = {}

    def count(self, value: str) -> int:
        return sum(1 for c in self.calls if str(c.get("value")) == value)

    def order(self) -> list[str]:
        return [str(c.get("value")) for c in self.calls]

    async def execute(self, **kw: Any) -> ToolResult:
        self.calls.append(kw)
        delay = float(kw.get("delay") or 0.0)
        if delay:
            await asyncio.sleep(delay)
        value = str(kw.get("value"))
        if self.fail.get(value):
            self.fail[value] -= 1
            return ToolResult(tool_name=self.name, success=False, error=f"{value} 失败")
        return ToolResult(tool_name=self.name, success=True,
                          output={"value": kw.get("value")})


class FakeLLM:
    """假模型：返回带 token 用量的 LLMResponse。"""

    def __init__(self, text: str = "ok", prompt_tokens: int = 0,
                 completion_tokens: int = 0) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    async def generate(self, messages: list[dict[str, Any]], **_: Any) -> Any:
        self.calls += 1
        return LLMResponse(text=self.text, prompt_tokens=self.prompt_tokens,
                           completion_tokens=self.completion_tokens)


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def reg(probe: Probe) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(probe)
    return registry


def load(text: str, **kw: Any):
    kw.setdefault("foreach_max", 1000)
    kw.setdefault("features", set(PRO))
    kw.setdefault("min_interval", 300)
    return WorkflowLoader(**kw).try_parse(textwrap.dedent(text).lstrip())


def build(text: str, **kw: Any):
    schema, errors, _ = load(text, **kw)
    assert schema is not None, [e.format() for e in errors]
    return schema


def errors_of(text: str, **kw: Any) -> list[str]:
    schema, errors, _ = load(text, **kw)
    assert schema is None, "期望校验失败，但它通过了"
    return [e.format() for e in errors]


async def approval_ok(step: Any, prompt: str, **_: Any) -> bool:
    return True


async def run(text: str, inputs: dict[str, Any] | None = None, *, registry: Any = None,
              llm: Any = None, dry_run: bool = False, approvals: Any = approval_ok):
    schema = build(text)
    return await run_workflow(schema, inputs or {}, registry=registry, llm=llm,
                              approval=approvals, dry_run=dry_run)


# ═══════════════════════════════════════════════════════════════
# R-401：步骤级条件 if / unless —— 加载期校验
# ═══════════════════════════════════════════════════════════════


CONDITIONS_WF = """
version: 1
name: 条件执行
inputs:
  mode: {type: string, required: true}
steps:
  - id: a
    type: tool
    tool: probe
    args: {value: "a-{{ inputs.mode }}"}
  - id: only_run
    type: tool
    if: "{{ inputs.mode }} == run"
    tool: probe
    args: {value: run}
  - id: only_skip
    type: tool
    unless: "{{ inputs.mode }} == run"
    tool: probe
    args: {value: skip}
  - id: last
    type: tool
    tool: probe
    args: {value: last}
"""


class TestConditionSchema:
    def test_if_parsed_and_serialized(self) -> None:
        schema = build(CONDITIONS_WF)
        step = schema.get_step("only_run")
        assert step is not None and step.if_condition == "{{ inputs.mode }} == run"
        assert step.as_dict()["if"] == "{{ inputs.mode }} == run"
        assert "当" in schema.summarize()

    def test_unless_parsed(self) -> None:
        schema = build(CONDITIONS_WF)
        step = schema.get_step("only_skip")
        assert step is not None and step.unless_condition == "{{ inputs.mode }} == run"
        assert step.as_dict()["unless"]

    def test_if_and_unless_conflict(self) -> None:
        text = CONDITIONS_WF.replace(
            '    unless: "{{ inputs.mode }} == run"',
            '    if: "{{ inputs.mode }} == run"\n    unless: "{{ inputs.mode }} == run"')
        assert any("不能同时写 if 与 unless" in m for m in errors_of(text))

    @pytest.mark.parametrize(("field", "value", "needle"), [
        ("if", '""', "非空字符串"),
        ("if", '"{{ inputs.mode }} =~ run"', "无法解析"),
        ("unless", '"{{ inputs.nope }} == x"', "未声明"),
    ])
    def test_bad_condition(self, field: str, value: str, needle: str) -> None:
        text = CONDITIONS_WF.replace(
            '    if: "{{ inputs.mode }} == run"', f"    {field}: {value}")
        assert any(needle in m for m in errors_of(text)), errors_of(text)

    def test_condition_may_reference_unknown_step(self) -> None:
        text = CONDITIONS_WF.replace('"{{ inputs.mode }} == run"',
                                     '"{{ steps.nope.output }} == run"', 1)
        assert any("nope" in m for m in errors_of(text))

    def test_condition_allowed_on_children(self) -> None:
        text = """
            version: 1
            name: 子步骤条件
            inputs:
              x: {type: string, required: true}
            steps:
              - id: fan
                type: parallel
                steps:
                  - id: a
                    type: tool
                    tool: probe
                    if: "{{ inputs.x }} == 1"
                  - id: b
                    type: tool
                    tool: probe
                    unless: "{{ inputs.x }} == 1"
        """
        schema = build(text)
        fan = schema.get_step("fan")
        assert fan is not None
        assert fan.substeps[0].if_condition and fan.substeps[1].unless_condition

    def test_branch_cannot_have_if(self) -> None:
        """branch 叠 if 会让两条分支段都被顺序执行 —— 加载期必须拒绝。"""
        text = """
            version: 1
            name: 分支条件
            inputs:
              x: {type: string, required: true}
            steps:
              - id: decide
                type: branch
                if: "{{ inputs.x }} == 1"
                condition: "{{ inputs.x }} == 1"
                then: good
                else: bad
              - id: good
                type: tool
                tool: probe
              - id: bad
                type: tool
                tool: probe
        """
        assert any("branch 步骤不支持 if / unless" in m for m in errors_of(text))


# ═══════════════════════════════════════════════════════════════
# R-401：步骤级条件 —— 真实执行
# ═══════════════════════════════════════════════════════════════


class TestConditionExecution:
    def test_if_true_runs_and_unless_skips(self, reg: ToolRegistry, probe: Probe) -> None:
        result = asyncio.run(run(CONDITIONS_WF, {"mode": "run"}, registry=reg))
        assert result.status == RUN_OK, result.error
        states = {s.id: s.status for s in result.steps}
        assert states == {"a": STATUS_OK, "only_run": STATUS_OK,
                          "only_skip": STATUS_SKIPPED, "last": STATUS_OK}
        assert probe.order() == ["a-run", "run", "last"]
        skipped = next(s for s in result.steps if s.id == "only_skip")
        assert skipped.detail["condition"]["should_run"] is False
        assert skipped.detail["condition"]["matched"] is True

    def test_if_false_skips(self, reg: ToolRegistry, probe: Probe) -> None:
        result = asyncio.run(run(CONDITIONS_WF, {"mode": "skip"}, registry=reg))
        assert result.status == RUN_OK, result.error
        states = {s.id: s.status for s in result.steps}
        assert states["only_run"] == STATUS_SKIPPED
        assert states["only_skip"] == STATUS_OK
        assert probe.order() == ["a-skip", "skip", "last"]

    def test_skipped_output_is_none_and_references_fail_clearly(
            self, reg: ToolRegistry, probe: Probe) -> None:
        text = """
            version: 1
            name: 引用被跳过步骤
            inputs:
              run: {type: string, required: true}
            steps:
              - id: a
                type: tool
                if: "{{ inputs.run }} == yes"
                tool: probe
              - id: b
                type: tool
                tool: probe
                args: {value: "{{ steps.a.output.value }}"}
        """
        result = asyncio.run(run(text, {"run": "no"}, registry=reg))
        assert result.status == RUN_ABORTED
        b = next(s for s in result.steps if s.id == "b")
        assert b.status in (STATUS_FAILED, STATUS_ABORTED)
        assert "模板渲染失败" in b.error
        assert probe.count("") == 0

    def test_unresolvable_condition_fails_step(self, reg: ToolRegistry) -> None:
        text = """
            version: 1
            name: 条件无法判定
            inputs:
              n: {type: string, required: true}
            steps:
              - id: a
                type: tool
                if: "{{ inputs.n }} > 5"
                tool: probe
        """
        result = asyncio.run(run(text, {"n": "abc"}, registry=reg))
        a = result.steps[0]
        assert a.status in (STATUS_FAILED, STATUS_ABORTED)
        assert "无法判定" in a.error

    def test_exists_condition(self, reg: ToolRegistry, probe: Probe) -> None:
        text = """
            version: 1
            name: exists
            steps:
              - id: a
                type: tool
                tool: probe
                args: {value: first}
              - id: has_value
                type: tool
                if: "{{ steps.a.output.value }} exists"
                tool: probe
                args: {value: second}
        """
        result = asyncio.run(run(text, registry=reg))
        assert result.status == RUN_OK and probe.order() == ["first", "second"]

    def test_parallel_group_tolerates_condition_skipped_child(
            self, reg: ToolRegistry, probe: Probe) -> None:
        text = """
            version: 1
            name: 并行组条件
            inputs:
              env: {type: string, required: true}
            steps:
              - id: fan
                type: parallel
                max_concurrency: 2
                steps:
                  - id: always
                    type: tool
                    tool: probe
                    args: {value: always}
                  - id: only_prod
                    type: tool
                    if: "{{ inputs.env }} == prod"
                    tool: probe
                    args: {value: prod}
              - id: after
                type: tool
                tool: probe
                args: {value: after}
        """
        result = asyncio.run(run(text, {"env": "dev"}, registry=reg))
        assert result.status == RUN_OK, result.error
        fan = next(s for s in result.steps if s.id == "fan")
        assert fan.status == STATUS_OK
        children = {c["id"]: c["status"] for c in fan.detail["children"]}
        assert children == {"always": STATUS_OK, "only_prod": STATUS_SKIPPED}
        assert fan.detail["skipped_children"] == ["only_prod"]
        assert probe.order() == ["always", "after"]

    def test_foreach_child_condition_skips_without_failing_item(
            self, reg: ToolRegistry, probe: Probe) -> None:
        text = """
            version: 1
            name: foreach 条件
            steps:
              - id: loop
                type: foreach
                items: [1, 2, 3]
                steps:
                  - id: work
                    type: tool
                    if: "{{ loop.item }} > 1"
                    tool: probe
                    args: {value: "item-{{ loop.item }}"}
                  - id: tail
                    type: tool
                    tool: probe
                    args: {value: "tail-{{ loop.item }}"}
        """
        result = asyncio.run(run(text, registry=reg))
        assert result.status == RUN_OK, result.error
        assert probe.order() == ["tail-1", "item-2", "tail-2", "item-3", "tail-3"]
        loop = next(s for s in result.steps if s.id == "loop")
        assert loop.status == STATUS_OK and loop.detail["failed_items"] == 0

    def test_dry_run_previews_condition_without_skipping(self, reg: ToolRegistry) -> None:
        result = asyncio.run(run(CONDITIONS_WF, {"mode": "run"}, registry=reg,
                                 dry_run=True))
        assert result.status == RUN_DRY_RUN
        step = next(s for s in result.steps if s.id == "only_run")
        assert step.status == STATUS_OK
        assert "condition_if" in step.detail and step.detail["condition_if"]["field"] == "if"
        other = next(s for s in result.steps if s.id == "only_skip")
        assert other.detail["condition_unless"]["field"] == "unless"

    def test_dry_run_reports_bad_condition_template(self, reg: ToolRegistry) -> None:
        # a 步骤还没有输出，试运行时条件里引用它 → 渲染报错必须暴露
        text = """
            version: 1
            name: dry 条件模板错
            steps:
              - id: a
                type: tool
                if: "{{ steps.a.output.value }} == x"
                tool: probe
        """
        result = asyncio.run(run(text, registry=reg, dry_run=True))
        step = result.steps[0]
        assert step.status == STATUS_FAILED and "试运行渲染失败" in step.error


# ═══════════════════════════════════════════════════════════════
# R-402：失败降级 fallback —— 加载期校验
# ═══════════════════════════════════════════════════════════════


FALLBACK_WF = """
version: 1
name: 失败降级
steps:
  - id: primary
    type: tool
    tool: probe
    args: {value: primary}
    on_failure: fallback(recover)
  - id: dependent
    type: tool
    tool: probe
    args: {value: dependent}
  - id: recover
    type: tool
    tool: probe
    args: {value: recover}
  - id: after
    type: tool
    tool: probe
    args: {value: after}
"""


class TestFallbackSchema:
    def test_parse_and_serialize(self) -> None:
        schema = build(FALLBACK_WF)
        step = schema.get_step("primary")
        assert step is not None
        assert step.on_failure.kind == "fallback"
        assert step.on_failure.fallback == "recover"
        assert step.on_failure.max_attempts == 1
        assert step.as_dict()["on_failure"]["fallback"] == "recover"
        assert "fallback(recover)" in schema.summarize()

    @pytest.mark.parametrize(("old", "new", "needle"), [
        ("fallback(recover)", "fallback(nowhere)", "指向不存在的步骤"),
        ("fallback(recover)", "fallback(primary)", "不能指向自己"),
        ("fallback(recover)", "fallback()", "缺少目标步骤 id"),
        ("fallback(recover)", "fallback(reco", "缺少目标步骤 id"),
        ("fallback(recover)", "fallback(a.b)", "不是合法步骤 id"),
    ])
    def test_bad_fallback(self, old: str, new: str, needle: str) -> None:
        assert old in FALLBACK_WF
        msgs = errors_of(FALLBACK_WF.replace(old, new))
        assert any(needle in m for m in msgs), msgs

    def test_backward_fallback_rejected(self) -> None:
        text = """
            version: 1
            name: 向后 fallback
            steps:
              - id: recover
                type: tool
                tool: probe
              - id: primary
                type: tool
                tool: probe
                on_failure: fallback(recover)
        """
        assert any("不支持循环" in m for m in errors_of(text))

    def test_fallback_not_allowed_on_children(self) -> None:
        text = """
            version: 1
            name: 子步骤 fallback
            steps:
              - id: loop
                type: foreach
                items: [1]
                steps:
                  - id: child
                    type: tool
                    tool: probe
                    on_failure: fallback(after)
              - id: after
                type: tool
                tool: probe
        """
        assert any("只能写在顶层步骤上" in m for m in errors_of(text))

    def test_fallback_and_retry_are_exclusive(self) -> None:
        # fallback 写成 retry 的变体不应被误认
        msgs = errors_of(FALLBACK_WF.replace("fallback(recover)",
                                            "retry(fallback(recover))"))
        assert any("无法识别的 on_failure" in m for m in msgs), msgs


# ═══════════════════════════════════════════════════════════════
# R-402：失败降级 —— 真实执行
# ═══════════════════════════════════════════════════════════════


class TestFallbackExecution:
    def test_failure_jumps_to_fallback_and_skips_between(
            self, reg: ToolRegistry, probe: Probe) -> None:
        probe.fail["primary"] = 1
        result = asyncio.run(run(FALLBACK_WF, registry=reg))
        assert result.status == RUN_PARTIAL, result.status
        states = {s.id: s.status for s in result.steps}
        assert states["primary"] == STATUS_FAILED
        assert states["dependent"] == STATUS_SKIPPED
        assert states["recover"] == STATUS_OK
        assert states["after"] == STATUS_OK
        assert probe.order() == ["primary", "recover", "after"]
        skipped = next(s for s in result.steps if s.id == "dependent")
        assert "fallback" in skipped.error
        primary = next(s for s in result.steps if s.id == "primary")
        assert primary.detail["fallback"] == "recover"
        assert result.outputs["recover"] == {"value": "recover"}

    def test_success_does_not_take_fallback(self, reg: ToolRegistry, probe: Probe) -> None:
        result = asyncio.run(run(FALLBACK_WF, registry=reg))
        assert result.status == RUN_OK
        states = {s.id: s.status for s in result.steps}
        assert states["dependent"] == STATUS_OK and states["recover"] == STATUS_OK
        assert "fallback" not in next(s for s in result.steps
                                      if s.id == "primary").detail

    def test_fallback_after_step_timeout(self, reg: ToolRegistry, probe: Probe) -> None:
        text = FALLBACK_WF.replace(
            "    args: {value: primary}\n",
            "    args: {value: primary, delay: 1.0}\n    timeout: 0.05\n")
        result = asyncio.run(run(text, registry=reg))
        primary = next(s for s in result.steps if s.id == "primary")
        assert primary.status == STATUS_FAILED and "步骤超时" in primary.error
        assert probe.order() == ["primary", "recover", "after"]
        assert result.status == RUN_PARTIAL

    def test_failing_fallback_step_aborts(self, reg: ToolRegistry, probe: Probe) -> None:
        probe.fail["primary"] = 1
        probe.fail["recover"] = 1
        result = asyncio.run(run(FALLBACK_WF, registry=reg))
        assert result.status == RUN_ABORTED
        states = {s.id: s.status for s in result.steps}
        assert states["primary"] == STATUS_FAILED and states["recover"] == STATUS_ABORTED
        assert states["after"] == STATUS_SKIPPED


# ═══════════════════════════════════════════════════════════════
# R-403：工作流级硬超时 timeout_seconds
# ═══════════════════════════════════════════════════════════════


TIMEOUT_WF = """
version: 1
name: 整轮超时
timeout_seconds: 0.15
steps:
  - id: slow
    type: tool
    tool: probe
    args: {value: slow, delay: 1.0}
  - id: after
    type: tool
    tool: probe
    args: {value: after}
"""


class TestWorkflowTimeout:
    def test_parsed_and_serialized(self) -> None:
        schema = build(TIMEOUT_WF)
        assert schema.timeout_seconds == pytest.approx(0.15)
        assert schema.as_dict()["timeout_seconds"] == pytest.approx(0.15)
        assert "整轮超时" in schema.summarize()

    @pytest.mark.parametrize("value", ["-1", "abc", "true"])
    def test_bad_timeout(self, value: str) -> None:
        text = TIMEOUT_WF.replace("timeout_seconds: 0.15", f"timeout_seconds: {value}")
        assert errors_of(text)

    def test_run_is_really_aborted(self, reg: ToolRegistry, probe: Probe) -> None:
        started = time.perf_counter()
        result = asyncio.run(run(TIMEOUT_WF, registry=reg))
        elapsed = time.perf_counter() - started
        assert elapsed < 0.9, f"整轮超时没有生效，耗时 {elapsed:.2f}s"
        assert result.status == RUN_ABORTED
        assert "工作流超时" in result.error and "0.15" in result.error
        states = {s.id: s.status for s in result.steps}
        assert states["slow"] in (STATUS_ABORTED, STATUS_FAILED, "cancelled")
        assert states["after"] == STATUS_SKIPPED
        assert probe.order() == ["slow"], "超时后不应继续执行后续步骤"

    def test_zero_means_unlimited(self, reg: ToolRegistry, probe: Probe) -> None:
        text = TIMEOUT_WF.replace("timeout_seconds: 0.15", "timeout_seconds: 0")
        result = asyncio.run(run(text, registry=reg))
        assert result.status == RUN_OK and probe.order() == ["slow", "after"]


# ═══════════════════════════════════════════════════════════════
# R-404：token 统计汇总进报告
# ═══════════════════════════════════════════════════════════════


TOKENS_WF = """
version: 1
name: token 统计
steps:
  - id: ask
    type: llm
    prompt: "hi"
  - id: again
    type: llm
    prompt: "again"
"""


class TestTokenStats:
    def test_llm_tokens_aggregated(self, reg: ToolRegistry) -> None:
        llm = FakeLLM("好的", prompt_tokens=100, completion_tokens=20)
        result = asyncio.run(run(TOKENS_WF, registry=reg, llm=llm))
        assert result.status == RUN_OK
        assert result.tokens == {"prompt": 200, "completion": 40, "total": 240}
        assert result.as_dict()["tokens"]["total"] == 240
        assert result.steps[0].tokens == {"prompt": 100, "completion": 20, "total": 120}
        assert result.steps[1].as_dict()["tokens"]["prompt"] == 100
        assert "240 tokens" in result.summary_line()

    def test_agent_tokens_aggregated(self, reg: ToolRegistry) -> None:
        from automind.workflow.executor import WorkflowExecutor

        text = """
            version: 1
            name: agent token
            steps:
              - id: think
                type: agent
                goal: "分析一下"
                allowed_tools: [probe]
        """

        async def agent_runner(step: Any, goal: str) -> dict[str, Any]:
            return {"answer": "完成", "stop_reason": "no_more_tools",
                    "actions_ok": 1, "actions_failed": 0, "iterations": 2,
                    "tokens": 321}

        executor = WorkflowExecutor(build(text), registry=reg, agent_runner=agent_runner)
        result = asyncio.run(executor.run())
        assert result.status == RUN_OK, result.error
        assert result.tokens["total"] == 321
        assert result.steps[0].tokens["total"] == 321

    def test_llm_without_usage_records_zero(self, reg: ToolRegistry) -> None:
        class NoUsageLLM:
            async def generate(self, messages: list[dict[str, Any]], **_: Any) -> Any:
                return "纯文本响应"

        result = asyncio.run(run(TOKENS_WF, registry=reg, llm=NoUsageLLM()))
        assert result.status == RUN_OK
        assert result.tokens["total"] == 0

    def test_run_summary_for_history(self, reg: ToolRegistry) -> None:
        """运行记录模块从报告里读 tokens —— 这条链路必须真的走通。"""
        from automind.workflow.runs import summarize

        llm = FakeLLM("x", prompt_tokens=7, completion_tokens=3)
        result = asyncio.run(run(TOKENS_WF, registry=reg, llm=llm))
        row = summarize({"run_id": "r", "workflow": "token 统计",
                         "report": result.as_dict()})
        assert row["tokens"] == 20
        assert row["duration_ms"] is not None


# ═══════════════════════════════════════════════════════════════
# R-405a：触发器 schema（cron 错过补偿 / webhook 重放保护）
# ═══════════════════════════════════════════════════════════════


RELIABLE_TRIGGERS = """
version: 1
name: 可靠触发
triggers:
  - id: daily
    cron: "0 9 * * 1-5"
    catch_up: skip
    max_delay_seconds: 3600
  - id: hook
    webhook: true
    dedupe_window: 300
steps:
  - id: s
    type: tool
    tool: probe
"""


class TestTriggerSchema:
    def test_parsed_and_serialized(self) -> None:
        schema = build(RELIABLE_TRIGGERS)
        daily = schema.get_trigger("daily")
        hook = schema.get_trigger("hook")
        assert daily is not None and daily.catch_up == "skip"
        assert daily.max_delay_seconds == 3600
        assert hook is not None and hook.dedupe_window == 300
        data = schema.as_dict()["triggers"]
        assert data[0]["catch_up"] == "skip" and data[0]["max_delay_seconds"] == 3600
        assert data[1]["dedupe_window"] == 300

    def test_defaults_keep_old_behavior(self) -> None:
        text = """
            version: 1
            name: 默认
            triggers:
              - cron: "0 9 * * *"
            steps:
              - {id: s, type: tool, tool: probe}
        """
        trigger = build(text).triggers[0]
        assert trigger.catch_up == "once"          # once = 与此前隐式行为一致
        assert trigger.max_delay_seconds == 0      # 0 = 不限制迟到
        assert trigger.dedupe_window == 0          # 0 = 关闭去重（向后兼容）

    @pytest.mark.parametrize(("old", "new", "needle"), [
        ("catch_up: skip", "catch_up: never", "catch_up 只能是"),
        ("catch_up: skip", "catch_up: skip\n    dedupe_window: 60", "只对 webhook"),
        ("dedupe_window: 300", "dedupe_window: 999999", "过大"),
        ("dedupe_window: 300", "dedupe_window: -1", "不能小于 0"),
        ("max_delay_seconds: 3600", "max_delay_seconds: -5", "不能小于 0"),
    ])
    def test_bad_trigger_fields(self, old: str, new: str, needle: str) -> None:
        assert old in RELIABLE_TRIGGERS
        msgs = errors_of(RELIABLE_TRIGGERS.replace(old, new))
        assert any(needle in m for m in msgs), msgs

    def test_community_still_gated(self) -> None:
        schema, errors, _ = load(RELIABLE_TRIGGERS, features=set())
        assert schema is None
        assert any("triggers" in e.message and "专业版" in e.message for e in errors)


# ═══════════════════════════════════════════════════════════════
# R-405b：专业版触发器可靠性（假 ctx 驱动，不启动服务端）
# ═══════════════════════════════════════════════════════════════


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def _trigger_setup(tmp_path: Any, text: str, monkeypatch: Any) -> dict[str, Any]:
    wp = pytest.importorskip("automind_pro.workflow_pro")
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    from automind.core import quota

    limits = {"workflow_triggers": 30, "workflow_min_interval": 300,
              "workflow_history": 50, "workflow_history_days": None,
              "workflow_foreach_max": 1000, "workflows": 20}
    monkeypatch.setattr(quota, "workflow_limit", lambda _key: limits.get(_key))
    monkeypatch.setattr(quota, "check_workflow_triggers", lambda _active: (True, ""))
    schema = build(text)
    catalog = {"wf.yaml": {"schema": schema, "runnable": True}}
    starts: list[dict[str, Any]] = []
    running: dict[str, str] = {}

    async def start_workflow(**kw: Any) -> dict[str, Any]:
        starts.append(kw)
        return {"ok": True, "run_id": f"run{len(starts)}"}

    app = FastAPI()
    ctx = {
        "app": app,
        "workflow_catalog": lambda: catalog,
        "workflow_running": lambda file, _key="": running.get(file, ""),
        "start_workflow": start_workflow,
        "data_dir": lambda: tmp_path,
    }
    clock = _Clock()
    trig = wp.WorkflowTriggersFeature(tick=1.0, resync=10**9, now=clock)
    trig._state = wp.TriggerStateStore(tmp_path / "triggers.json")
    trig.attach(ctx)
    entries = trig.sync()
    return {"wp": wp, "trig": trig, "entries": entries, "clock": clock,
            "starts": starts, "running": running, "app": app}


def _entry_of(env: dict[str, Any], trigger_id: str) -> Any:
    key = env["wp"].trigger_key("wf.yaml", trigger_id)
    return env["entries"][key]


CRON_WF = """
version: 1
name: 定时
triggers:
  - id: daily
    cron: "0 9 * * *"
{extra}
steps:
  - id: s
    type: tool
    tool: probe
"""


class TestTriggerCatchUp:
    def test_late_trigger_catches_up_once_by_default(self, wx_tmp, monkeypatch) -> None:
        env = _trigger_setup(wx_tmp, CRON_WF.format(extra=""), monkeypatch)
        entry = _entry_of(env, "daily")
        env["clock"].t = entry.next_run + 700          # 服务停机一夜后开机
        fired = asyncio.run(env["trig"].tick_once())
        assert fired == [entry.key]
        assert len(env["starts"]) == 1
        state = env["trig"].state.get(entry.key)
        assert state["last_catch_up"] is True and state["missed_runs"] == 1
        assert env["trig"]._entries[entry.key].as_dict()["last_catch_up"] is True

    def test_on_time_trigger_is_not_missed(self, wx_tmp, monkeypatch) -> None:
        env = _trigger_setup(wx_tmp, CRON_WF.format(extra=""), monkeypatch)
        entry = _entry_of(env, "daily")
        env["clock"].t = entry.next_run + 5            # tick 间隔内的正常迟到
        assert asyncio.run(env["trig"].tick_once()) == [entry.key]
        state = env["trig"].state.get(entry.key)
        assert state["last_catch_up"] is False and int(state.get("missed_runs") or 0) == 0

    def test_catch_up_skip_policy(self, wx_tmp, monkeypatch) -> None:
        env = _trigger_setup(
            wx_tmp, CRON_WF.format(extra="    catch_up: skip\n"), monkeypatch)
        entry = _entry_of(env, "daily")
        env["clock"].t = entry.next_run + 700
        assert asyncio.run(env["trig"].tick_once()) == []
        assert env["starts"] == []
        state = env["trig"].state.get(entry.key)
        assert state["last_status"] == "catch_up_skipped"
        assert int(state["missed_runs"]) == 1
        assert entry.next_run > env["clock"].t            # 下一次仍按"现在之后"算

    def test_max_delay_guard(self, wx_tmp, monkeypatch) -> None:
        env = _trigger_setup(
            wx_tmp, CRON_WF.format(extra="    max_delay_seconds: 300\n"), monkeypatch)
        entry = _entry_of(env, "daily")
        env["clock"].t = entry.next_run + 700
        assert asyncio.run(env["trig"].tick_once()) == []   # 迟到 700s > 300s → 不补
        assert env["starts"] == []
        entry = env["trig"]._entries[entry.key]             # 跳过时已推进到下一次
        env["clock"].t = entry.next_run + 5
        assert asyncio.run(env["trig"].tick_once()) == [entry.key]
        assert len(env["starts"]) == 1


WEBHOOK_ONLY_WF = """
version: 1
name: hook
triggers:
  - id: hook
    webhook: true
{extra}
steps:
  - id: s
    type: tool
    tool: probe
"""


def _webhook_client(env: dict[str, Any], wp: Any) -> tuple[Any, str]:
    from fastapi.testclient import TestClient

    token = "test-token-0123456789"
    entry = _entry_of(env, "hook")
    env["trig"]._record(entry, token_hash=wp._hash_token(token))
    env["trig"].sync()
    return TestClient(env["app"]), token


class TestWebhookReplay:
    def test_same_body_within_window_is_deduplicated(self, wx_tmp, monkeypatch) -> None:
        env = _trigger_setup(
            wx_tmp, WEBHOOK_ONLY_WF.format(extra="    dedupe_window: 300\n"), monkeypatch)
        wp = env["wp"]
        client, token = _webhook_client(env, wp)
        headers = {"X-AutoMind-Hook-Token": token}
        first = client.post("/api/workflow/hooks/" + _entry_of(env, "hook").key,
                            json={"order": "A1"}, headers=headers)
        assert first.status_code == 200 and first.json()["ok"]
        run_id = first.json()["run_id"]
        second = client.post("/api/workflow/hooks/" + _entry_of(env, "hook").key,
                             json={"order": "A1"}, headers=headers)
        assert second.status_code == 200
        assert second.json()["duplicate"] is True and second.json()["run_id"] == run_id
        assert len(env["starts"]) == 1, "重复请求不应再触发运行"
        # 不同的 Idempotency-Key 视为不同请求
        third = client.post("/api/workflow/hooks/" + _entry_of(env, "hook").key,
                            json={"order": "A1"},
                            headers={**headers, "Idempotency-Key": "req-2"})
        assert third.status_code == 200 and not third.json().get("duplicate")
        assert len(env["starts"]) == 2

    def test_dedupe_disabled_by_default(self, wx_tmp, monkeypatch) -> None:
        env = _trigger_setup(wx_tmp, WEBHOOK_ONLY_WF.format(extra=""), monkeypatch)
        wp = env["wp"]
        client, token = _webhook_client(env, wp)
        headers = {"X-AutoMind-Hook-Token": token}
        for _ in range(2):
            r = client.post("/api/workflow/hooks/" + _entry_of(env, "hook").key,
                            json={"order": "B1"}, headers=headers)
            assert r.status_code == 200 and not r.json().get("duplicate")
        assert len(env["starts"]) == 2

    def test_dedupe_helpers(self) -> None:
        wp = pytest.importorskip("automind_pro.workflow_pro")
        assert wp._dedupe_key(b"x", "") == wp._dedupe_key(b"x", "")
        assert wp._dedupe_key(b"x", "") != wp._dedupe_key(b"y", "")
        assert wp._dedupe_key(b"x", "abc") == "h:abc"
        recent = [["a", 100.0, "r1"], ["b", 500.0, "r2"], ["bad"], ["c", 700.0]]
        kept = wp._prune_recent_keys(recent, now=800.0, window=350)
        assert [k[0] for k in kept] == ["b", "c"]
        assert wp._prune_recent_keys([], now=0, window=1) == []

    def test_message_includes_duration_tokens_and_slowest_step(self) -> None:
        wp = pytest.importorskip("automind_pro.workflow_pro")
        title, text = wp._message(
            {"workflow": "日报", "run_id": "r1", "trigger": "cron"},
            outcome="failed", error="步骤 push 失败", extra="",
            summary={"total": 3, "succeeded": 2, "failed": 1, "skipped": 0,
                     "duration_ms": 1500, "tokens": 42,
                     "slowest_step": {"id": "push", "duration_ms": 1200}})
        assert "日报" in title and "失败" in title
        assert "耗时：1.50s" in text and "Token：42" in text
        assert "最慢步骤：push（1.20s）" in text

