"""v1.8.0 引擎新能力：agent / foreach 步骤、版本拦截、retry 修正、断点续跑、按步快照。

对应 docs/WORKFLOW_ROADMAP.md 的 R-104、R-106、R-107、R-110 与设计规则 D1 / D6。
全部离线：agent 步骤要么注入假 runner（测执行器语义），要么用脚本化的假模型
驱动真正的 ReActExecutor（测 agent_step 的工具白名单与预算）。
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from automind.core.exceptions import BudgetExhaustedError
from automind.core.types import LLMResponse, PermissionTier, ToolCall, ToolResult
from automind.tools.base import AbstractTool, ToolRegistry
from automind.workflow.agent_step import (
    RestrictedToolRegistry,
    StepBudgetLLM,
    make_agent_runner,
)
from automind.workflow.executor import (
    RUN_ABORTED,
    RUN_DRY_RUN,
    RUN_OK,
    RUN_PARTIAL,
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_OK,
    WorkflowExecutor,
    run_workflow,
)
from automind.workflow.loader import WorkflowLoader


def load(text: str, **kw: Any):
    kw.setdefault("foreach_max", 20)
    kw.setdefault("features", set())
    return WorkflowLoader(**kw).try_parse(textwrap.dedent(text).lstrip())


def build(text: str, **kw: Any):
    schema, errors, _ = load(text, **kw)
    assert schema is not None, [e.format() for e in errors]
    return schema


def errors_of(text: str, **kw: Any) -> list[str]:
    schema, errors, _ = load(text, **kw)
    assert schema is None, "期望校验失败，但它通过了"
    return [e.format() for e in errors]


# ═══════════════════════════════════════════════════════════════
# 校验：agent
# ═══════════════════════════════════════════════════════════════


class TestAgentSchema:
    def test_defaults_and_safe_only_warning(self) -> None:
        schema, errors, warnings = load("""
            version: 1
            name: a
            steps:
              - id: think
                type: agent
                goal: 总结一下
            """)
        assert schema is not None and not errors
        step = schema.steps[0]
        assert (step.max_steps, step.token_budget, step.mode) == (20, 0, "react")
        assert step.allowed_tools is None
        assert any("只读" in w.message for w in warnings), "未声明 allowed_tools 必须提醒"

    def test_explicit_fields(self) -> None:
        step = build("""
            version: 1
            name: a
            inputs:
              path: {type: string, default: x.csv}
            steps:
              - id: analyze
                type: agent
                goal: "阅读 {{ inputs.path }}"
                allowed_tools: [file_read, file_write, file_read]
                max_steps: 15
                token_budget: 80000
                on_failure: retry(1)
            """).steps[0]
        assert step.allowed_tools == ["file_read", "file_write"]     # 去重但保序
        assert (step.max_steps, step.token_budget) == (15, 80000)
        assert step.as_dict()["allowed_tools"] == ["file_read", "file_write"]

    @pytest.mark.parametrize("field, value, needle", [
        ("max_steps", "0", "不能小于 1"),
        ("max_steps", "500", "上限 200"),
        ("token_budget", "-1", "不能小于 0"),
        ("mode", "plan", "mode 不支持"),
        ("allowed_tools", "file_read", "工具名列表"),
    ])
    def test_bad_fields(self, field: str, value: str, needle: str) -> None:
        errs = errors_of(f"""
            version: 1
            name: a
            steps:
              - id: x
                type: agent
                goal: g
                {field}: {value}
            """)
        assert any(needle in e for e in errs), errs

    def test_goal_template_checked(self) -> None:
        errs = errors_of("""
            version: 1
            name: a
            steps:
              - id: x
                type: agent
                goal: "{{ inputs.nope }}"
            """)
        assert any("未声明的入参" in e for e in errs)


# ═══════════════════════════════════════════════════════════════
# 校验：foreach
# ═══════════════════════════════════════════════════════════════


FOREACH = """
version: 1
name: 逐项
steps:
  - id: list
    type: tool
    tool: recorder
    args: {value: x}
  - id: each
    type: foreach
    items: "{{ steps.list.output.rows }}"
    steps:
      - id: send
        type: tool
        tool: recorder
        args: {value: "第{{ loop.index }}项：{{ loop.item.name }}"}
      - id: note
        type: tool
        tool: recorder
        args: {value: "{{ steps.send.output }}"}
  - id: done
    type: tool
    tool: recorder
    args: {value: "{{ steps.each.output }}"}
"""


class TestForeachSchema:
    def test_valid(self) -> None:
        schema = build(FOREACH)
        each = schema.steps[1]
        assert each.type == "foreach" and [c.id for c in each.substeps] == ["send", "note"]
        assert all(c.parent == "each" for c in each.substeps)
        assert "子步骤=send(tool)、note(tool)" in schema.summarize()

    def test_literal_list_over_limit_reports_line(self) -> None:
        schema, errors, _ = load("""
            version: 1
            name: a
            steps:
              - id: each
                type: foreach
                items: [1, 2, 3]
                steps:
                  - id: s
                    type: tool
                    tool: recorder
            """, foreach_max=2)
        assert schema is None
        issue = next(e for e in errors if "超过" in e.message)
        assert issue.position.line == 6 and "上限 1000" in issue.message

    def test_loop_outside_foreach_rejected(self) -> None:
        errs = errors_of("""
            version: 1
            name: a
            steps:
              - id: s
                type: tool
                tool: recorder
                args: {value: "{{ loop.item }}"}
            """)
        assert any("loop 只能在 foreach" in e for e in errs)

    def test_bad_loop_field(self) -> None:
        errs = errors_of(FOREACH.replace("loop.index", "loop.idx"))
        assert any("loop 只暴露 item" in e for e in errs)

    @pytest.mark.parametrize("child_type", ["branch", "foreach"])
    def test_disallowed_child_types(self, child_type: str) -> None:
        errs = errors_of(FOREACH.replace("      - id: note\n        type: tool",
                                         f"      - id: note\n        type: {child_type}"))
        assert any("foreach 的子步骤不支持" in e for e in errs)

    def test_substep_not_visible_outside(self) -> None:
        errs = errors_of(FOREACH.replace('args: {value: "{{ steps.each.output }}"}',
                                         'args: {value: "{{ steps.send.output }}"}'))
        assert any("是 foreach 'each' 的子步骤" in e for e in errs)

    def test_own_output_not_visible_inside(self) -> None:
        errs = errors_of(FOREACH.replace('"{{ steps.send.output }}"',
                                         '"{{ steps.each.output }}"'))
        assert any("不能在 foreach 'each' 的子步骤里引用它自己的输出" in e for e in errs)

    def test_ids_share_one_namespace(self) -> None:
        errs = errors_of(FOREACH.replace("- id: note", "- id: list"))
        assert any("步骤 id 重复：'list'" in e for e in errs)

    def test_plain_string_items_rejected(self) -> None:
        errs = errors_of(FOREACH.replace('"{{ steps.list.output.rows }}"', "rows"))
        assert any("应当是模板" in e for e in errs)


# ═══════════════════════════════════════════════════════════════
# 版本拦截（R-110）
# ═══════════════════════════════════════════════════════════════


class TestEditionGates:
    PARALLEL = """
        version: 1
        name: a
        steps:
          - id: p
            type: parallel
            steps: []
        """

    def test_parallel_needs_pro_at_load_time(self) -> None:
        schema, errors, _ = load(self.PARALLEL)
        assert schema is None
        assert "需要专业版" in errors[0].message and errors[0].position.line == 4

    def test_licensed_parallel_loads(self) -> None:
        """v1.9.0 起 parallel 已实现：授权了 workflow_parallel 就能加载（不再报"尚未实现"）。"""
        text = self.PARALLEL.replace("steps: []", """steps:
              - {id: x, type: tool, tool: recorder}
              - {id: y, type: tool, tool: recorder}""")
        schema, errors, _ = load(text, features={"workflow_parallel"})
        assert schema is not None, [e.message for e in errors]
        assert [c.id for c in schema.steps[0].substeps] == ["x", "y"]

    def test_triggers_top_level(self) -> None:
        schema, errors, _ = load("""
            version: 1
            name: a
            triggers:
              - cron: "0 9 * * 1-5"
            steps:
              - id: s
                type: tool
                tool: recorder
            """)
        assert schema is None
        assert len(errors) == 1 and "顶层字段 'triggers' 需要专业版" in errors[0].message

    def test_gate_follows_the_feature_not_the_edition_name(self) -> None:
        """拦不拦看的是"特性有没有授权"，提示语说清需要哪个版本。"""
        from automind.workflow.loader import build_document
        from automind.workflow.schema import collect_issues
        doc, positions, _ = build_document(textwrap.dedent(self.PARALLEL))
        _, errors, _ = collect_issues(doc, positions=positions, features=set(),
                                      edition="community", edition_label="社区版")
        assert "需要专业版" in errors[0].message and "当前为社区版" in errors[0].message

    def test_planned_keys_are_not_advertised_as_commercial_features(self) -> None:
        """已规划的键不进 COMMERCIAL_FEATURES：那张表每一项都必须有真实现，
        否则企业版许可证的 feature_flags() 会出现"未授权"（pro/tests 断言全部为真）。"""
        from automind.core.edition import COMMERCIAL_FEATURES, PLANNED_FEATURES
        from automind.workflow.schema import RESERVED_STEP_TYPES, RESERVED_TOP_LEVEL

        assert not set(PLANNED_FEATURES) & set(COMMERCIAL_FEATURES)
        for feature, _since in [*RESERVED_STEP_TYPES.values(), *RESERVED_TOP_LEVEL.values()]:
            assert feature in PLANNED_FEATURES or feature in COMMERCIAL_FEATURES

    def test_loader_defaults_follow_current_edition(self) -> None:
        limits = WorkflowLoader().limits()
        assert limits["foreach_max"] == 20            # 社区版
        assert "workflow_parallel" not in (limits["features"] or set())


# ═══════════════════════════════════════════════════════════════
# 执行：agent 步骤（注入假 runner）
# ═══════════════════════════════════════════════════════════════


AGENT_FLOW = """
version: 1
name: 分析
steps:
  - id: analyze
    type: agent
    goal: 找出错误率最高的接口
    allowed_tools: [recorder]
    on_failure: continue
  - id: send
    type: tool
    tool: recorder
    args: {value: "{{ steps.analyze.output.answer }}"}
"""


def fake_runner(result: dict[str, Any]):
    calls: list[tuple[str, str]] = []

    async def _runner(step: Any, goal: str) -> dict[str, Any]:
        calls.append((step.id, goal))
        return dict(result)

    _runner.calls = calls                                 # type: ignore[attr-defined]
    return _runner


class TestAgentExecution:
    async def test_ok_output_is_d6_shape(self, registry, recorder) -> None:
        runner = fake_runner({"answer": "/api/pay", "artifacts": ["report.md"],
                              "actions_ok": 3, "actions_failed": 0,
                              "stop_reason": "no_more_tools"})
        run = await run_workflow(build(AGENT_FLOW), {}, registry=registry,
                                 agent_runner=runner)
        assert run.status == RUN_OK
        assert runner.calls == [("analyze", "找出错误率最高的接口")]
        assert recorder.calls[0]["value"] == "/api/pay"
        assert set(run.outputs["analyze"]) == {"answer", "artifacts", "actions_ok",
                                               "actions_failed", "stop_reason"}
        assert run.steps[0].detail["artifacts"] == ["report.md"]

    async def test_not_finished_is_failure_with_partial_output(self, registry, recorder) -> None:
        runner = fake_runner({"answer": "做到一半", "stop_reason": "max_iterations",
                              "actions_ok": 2, "denied_tools": ["terminal"]})
        run = await run_workflow(build(AGENT_FLOW), {}, registry=registry,
                                 agent_runner=runner)
        step = run.steps[0]
        assert step.status == STATUS_FAILED and "max_steps" in step.error
        assert step.detail["denied_tools"] == ["terminal"], "越权调用必须进报告"
        assert run.status == RUN_PARTIAL
        assert recorder.calls[0]["value"] == "做到一半"   # 部分结果仍可被后续步骤读到

    async def test_no_runner_is_explicit(self, registry) -> None:
        run = await run_workflow(build(AGENT_FLOW), {}, registry=registry)
        assert run.steps[0].status == STATUS_FAILED
        assert "没有注入 agent_runner" in run.steps[0].error

    async def test_retry_applies_to_agent(self, registry) -> None:
        outcomes = iter([{"stop_reason": "no_progress"}, {"stop_reason": "no_more_tools",
                                                          "answer": "ok"}])

        async def runner(step: Any, goal: str) -> dict[str, Any]:
            return next(outcomes)

        schema = build(AGENT_FLOW.replace("on_failure: continue", "on_failure: retry(1)"))
        run = await run_workflow(schema, {}, registry=registry, agent_runner=runner)
        assert run.steps[0].status == STATUS_OK and run.steps[0].retries == 1

    async def test_dry_run_does_not_call_runner(self, registry) -> None:
        runner = fake_runner({"stop_reason": "no_more_tools"})
        run = await run_workflow(build(AGENT_FLOW), {}, registry=registry,
                                 agent_runner=runner, dry_run=True)
        assert run.status == RUN_DRY_RUN and runner.calls == []
        assert run.steps[0].detail["allowed_tools"] == ["recorder"]


class _FlakyLLM:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def generate(self, messages: Any, **_: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("upstream reset")
        return SimpleNamespace(text="好了")


class TestLlmRetryFix:
    """v1.8.0 修正：llm 步骤写 retry(n) 此前根本不重试，失败后还按 continue 放行。"""

    FLOW = """
        version: 1
        name: a
        steps:
          - id: gen
            type: llm
            prompt: hi
            on_failure: retry(2)
          - id: after
            type: tool
            tool: recorder
        """

    async def test_retries_then_succeeds(self, registry) -> None:
        llm = _FlakyLLM(failures=2)
        run = await run_workflow(build(self.FLOW), {}, registry=registry, llm=llm)
        assert llm.calls == 3 and run.steps[0].status == STATUS_OK
        assert run.steps[0].retries == 2 and run.status == RUN_OK

    async def test_exhausted_retry_aborts(self, registry, recorder) -> None:
        llm = _FlakyLLM(failures=10)
        run = await run_workflow(build(self.FLOW), {}, registry=registry, llm=llm)
        assert llm.calls == 3
        assert run.steps[0].status == STATUS_ABORTED and "已重试 2 次" in run.steps[0].error
        assert run.status == RUN_ABORTED and recorder.call_count == 0


# ═══════════════════════════════════════════════════════════════
# 执行：foreach
# ═══════════════════════════════════════════════════════════════


class TestForeachExecution:
    async def test_iterates_with_loop_vars(self, registry, recorder) -> None:
        recorder.output = {"rows": [{"name": "甲"}, {"name": "乙"}]}
        run = await run_workflow(build(FOREACH), {}, registry=registry)
        assert run.status == RUN_OK
        values = [c["value"] for c in recorder.calls]
        # list, (send, note) × 2, done
        assert values[1] == "第0项：甲" and values[3] == "第1项：乙"
        out = run.outputs["each"]
        assert [o["index"] for o in out] == [0, 1] and out[0]["status"] == STATUS_OK
        assert set(out[0]["outputs"]) == {"send", "note"}
        assert run.steps[1].detail["executed"] == 2

    async def test_items_from_json_text(self, registry, recorder) -> None:
        recorder.output = {"rows": '[{"name": "丙"}]'}
        run = await run_workflow(build(FOREACH), {}, registry=registry)
        assert run.status == RUN_OK and run.steps[1].detail["items"] == 1

    async def test_runtime_limit_runs_nothing(self, registry, recorder) -> None:
        recorder.output = {"rows": [{"name": str(i)} for i in range(5)]}
        run = await run_workflow(build(FOREACH), {}, registry=registry, foreach_max=3)
        each = run.steps[1]
        assert each.status == STATUS_ABORTED and "一项都未执行" in each.error
        assert recorder.call_count == 1                   # 只有 list 那一步

    async def test_abort_child_stops_remaining_items(self, registry, recorder) -> None:
        recorder.output = {"rows": [{"name": "a"}, {"name": "b"}]}

        async def patched(**kw: Any) -> ToolResult:
            recorder.calls.append(kw)
            if kw.get("value", "").startswith("第0项"):
                return ToolResult(tool_name="recorder", success=False, error="发送失败")
            return ToolResult(tool_name="recorder", success=True, output=recorder.output)

        recorder.execute = patched                        # type: ignore[method-assign]
        run = await run_workflow(build(FOREACH), {}, registry=registry)
        each = run.steps[1]
        assert each.status == STATUS_ABORTED and "剩余项不再执行" in each.error
        assert each.detail["executed"] == 1
        assert not any(str(c.get("value", "")).startswith("第1项") for c in recorder.calls)

    async def test_continue_child_marks_item_and_goes_on(self, registry, recorder) -> None:
        text = FOREACH.replace(
            'args: {value: "第{{ loop.index }}项：{{ loop.item.name }}"}',
            'args: {value: "第{{ loop.index }}项：{{ loop.item.name }}"}\n'
            '        on_failure: continue').replace(
            '"{{ steps.send.output }}"', '"{{ loop.item.name }}"').replace(
            "  - id: done", "    on_failure: continue\n  - id: done")

        async def patched(**kw: Any) -> ToolResult:
            recorder.calls.append(kw)
            if kw.get("value") == "第0项：a":
                return ToolResult(tool_name="recorder", success=False, error="发送失败")
            return ToolResult(tool_name="recorder", success=True,
                              output={"rows": [{"name": "a"}, {"name": "b"}], "value": "v"})

        recorder.execute = patched                        # type: ignore[method-assign]
        run = await run_workflow(build(text), {}, registry=registry)
        each = run.steps[1]
        assert each.status == STATUS_FAILED and "1/2 项" in each.error
        assert run.status == RUN_PARTIAL
        assert run.steps[2].status == STATUS_OK      # 整步 continue，后续照跑

    async def test_dry_run_previews_and_checks_literal_limit(self, registry, recorder) -> None:
        run = await run_workflow(build(FOREACH), {}, registry=registry, dry_run=True)
        assert run.status == RUN_DRY_RUN and recorder.call_count == 0
        preview = run.steps[1].detail
        assert preview["foreach"] is True and len(preview["preview"]) == 1


# ═══════════════════════════════════════════════════════════════
# 断点续跑与按步快照（R-104）
# ═══════════════════════════════════════════════════════════════


BRANCHY = """
version: 1
name: 续跑
steps:
  - id: a
    type: tool
    tool: recorder
    args: {value: a}
  - id: gate
    type: branch
    condition: "{{ steps.a.output.value }} == a"
    then: b
    else: c
  - id: b
    type: tool
    tool: recorder
    args: {value: "b-{{ steps.a.output.value }}"}
  - id: c
    type: tool
    tool: recorder
    args: {value: c}
"""


class TestResume:
    async def test_ok_steps_are_reused_and_branch_reevaluated(self, registry, recorder) -> None:
        schema = build(BRANCHY)
        resume = {"from_run": "old1", "steps": {"a": {"output": {"value": "a"},
                                                      "output_digest": "d1"}}}
        run = await run_workflow(schema, {}, registry=registry, resume=resume)
        assert run.status == RUN_OK
        assert [c["value"] for c in recorder.calls] == ["b-a"], "a 不许再执行"
        assert run.resumed_from == "old1" and run.resumed_steps == ["a"]
        a = run.steps[0]
        assert a.detail["resumed"] is True and a.output_digest == "d1"
        assert run.steps[1].detail["goto"] == "b"
        assert "沿用上次结果" in run.summary_line()
        assert run.as_dict()["resumed_steps"] == ["a"]

    async def test_checkpoint_snapshots_after_each_step(self, registry, recorder) -> None:
        snaps: list[dict[str, Any]] = []
        executor = WorkflowExecutor(build(BRANCHY), registry=registry,
                                    on_checkpoint=snaps.append, run_id="r1")
        run = await executor.run()
        assert run.run_id == "r1" and len(snaps) == 3        # a、gate、b 各一次
        last = snaps[-1]
        assert set(last["records"]) == {"a", "gate", "b", "c"}
        assert last["records"]["c"]["status"] == "skipped"      # 没走的分支也如实落盘
        assert set(last["outputs"]) == {"a", "gate", "b"}       # 只有成功步骤带输出
        assert last["outputs"]["a"] == {"value": "a"}
        assert last["source_digest"] == run.source_digest

    async def test_dry_run_never_checkpoints(self, registry) -> None:
        snaps: list[dict[str, Any]] = []
        await WorkflowExecutor(build(BRANCHY), registry=registry, dry_run=True,
                               on_checkpoint=snaps.append).run()
        assert snaps == []


# ═══════════════════════════════════════════════════════════════
# agent_step：工具白名单（D1）与步骤预算
# ═══════════════════════════════════════════════════════════════


class _Tool(AbstractTool):
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    def __init__(self, name: str, tier: PermissionTier) -> None:
        self.name = name
        self.description = f"测试工具 {name}"
        self.permission_tier = tier
        self.calls = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name=self.name, success=True,
                          output={"path": kwargs.get("path", "out.md")})


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_Tool("file_read", PermissionTier.SAFE))
    reg.register(_Tool("file_write", PermissionTier.SENSITIVE))
    reg.register(_Tool("terminal", PermissionTier.DANGEROUS))
    return reg


class TestRestrictedRegistry:
    async def test_declared_allow_list(self) -> None:
        base = _registry()
        reg = RestrictedToolRegistry(base, ["file_read", "file_write", "ghost"])
        assert reg.list_names() == ["file_read", "file_write"] and reg.missing == ["ghost"]
        denied = await reg.dispatch("terminal", command="rm -rf /")
        assert not denied.success and denied.metadata["workflow_denied"]
        assert "allowed_tools" in denied.error and base.get("terminal").calls == 0
        ok = await reg.dispatch("file_read", path="a")
        assert ok.success

    async def test_undeclared_means_safe_only(self) -> None:
        reg = RestrictedToolRegistry(_registry(), None)
        assert reg.list_names() == ["file_read"]
        denied = await reg.dispatch("file_write", path="x")
        assert "只提供只读工具" in denied.error

    async def test_unknown_name_still_gets_suggestions(self) -> None:
        reg = RestrictedToolRegistry(_registry(), ["file_read"])
        res = await reg.dispatch("no_such_tool")
        assert not res.success and not res.metadata.get("workflow_denied")


class TestStepBudget:
    async def test_raises_budget_exhausted_after_limit(self) -> None:
        class _LLM:
            async def generate(self, messages: Any, tools: Any = None, stop: Any = None):
                return LLMResponse(text="x", prompt_tokens=60, completion_tokens=50)

        llm = StepBudgetLLM(_LLM(), budget=100)
        await llm.generate([])
        assert llm.used == 110
        with pytest.raises(BudgetExhaustedError):
            await llm.generate([])

    async def test_zero_means_unlimited(self) -> None:
        class _LLM:
            async def generate(self, messages: Any, tools: Any = None, stop: Any = None):
                return LLMResponse(text="x", prompt_tokens=10_000)

        llm = StepBudgetLLM(_LLM(), budget=0)
        for _ in range(3):
            await llm.generate([])
        assert llm.used == 30_000


class _ScriptedLLM:
    """按脚本返回：先调一个越权工具、再调一个允许的工具、最后给结论。"""

    def __init__(self) -> None:
        self.turn = 0

    async def generate(self, messages: Any, tools: Any = None, stop: Any = None):
        self.turn += 1
        if self.turn == 1:
            return LLMResponse(text="", tool_calls=[
                ToolCall(id="c1", name="terminal", arguments={"command": "curl x"})],
                prompt_tokens=100, completion_tokens=20)
        if self.turn == 2:
            return LLMResponse(text="", tool_calls=[
                ToolCall(id="c2", name="file_write", arguments={"path": "report.md"})],
                prompt_tokens=100, completion_tokens=20)
        return LLMResponse(text="已写入 report.md", prompt_tokens=100, completion_tokens=20)


def _fake_agent(llm: Any) -> Any:
    ex = SimpleNamespace(auto_test=False, react_repeat_action_threshold=2,
                         react_result_cache=True, react_no_progress_limit=12,
                         react_tool_timeout_seconds=30.0, tool_timeout_max_seconds=60.0)
    return SimpleNamespace(llm=llm, tool_registry=_registry(), permissions=None,
                           config=SimpleNamespace(execution=ex))


class TestAgentRunner:
    async def test_end_to_end_with_real_react_loop(self) -> None:
        agent = _fake_agent(_ScriptedLLM())
        events: list[tuple[str, dict]] = []
        runner = make_agent_runner(agent, workflow="日报",
                                   on_event=lambda k, p: events.append((k, p)))
        step = build("""
            version: 1
            name: a
            steps:
              - id: analyze
                type: agent
                goal: 写报告
                allowed_tools: [file_read, file_write]
                max_steps: 5
            """).steps[0]
        out = await runner(step, "写报告")
        assert out["stop_reason"] == "no_more_tools"
        assert out["answer"] == "已写入 report.md"
        assert out["denied_tools"] == ["terminal"], "越权调用必须被记录"
        assert out["allowed_tools"] == ["file_read", "file_write"]
        assert agent.tool_registry.get("terminal").calls == 0, "越权工具绝不能被执行"
        assert agent.tool_registry.get("file_write").calls == 1
        assert out["artifacts"] == ["report.md"]
        assert out["tokens"] == 360 and out["iterations"] >= 3
        assert [k for k, _ in events] == ["agent_action", "agent_action"]

    async def test_step_budget_stops_with_partial_delivery(self) -> None:
        agent = _fake_agent(_ScriptedLLM())
        runner = make_agent_runner(agent)
        step = build("""
            version: 1
            name: a
            steps:
              - id: analyze
                type: agent
                goal: 写报告
                allowed_tools: [file_write]
                token_budget: 100
            """).steps[0]
        out = await runner(step, "写报告")
        assert out["stop_reason"] == "task_budget"
        assert out["actions_failed"] == 1        # 第一轮的越权调用

    async def test_ask_mode_approval_denies_out_of_scope_without_prompting(self) -> None:
        asked: list[str] = []

        async def approval(tool: str, args: Any, tier: str, reason: str) -> bool:
            asked.append(tool)
            return True

        class _AskAll:
            def check(self, name: str, tier: Any, args: Any):
                return SimpleNamespace(value="ask_user"), "需要确认"

        agent = _fake_agent(_ScriptedLLM())
        agent.permissions = _AskAll()
        runner = make_agent_runner(agent, approval_for_step=lambda _step: approval)
        step = build("""
            version: 1
            name: a
            steps:
              - id: analyze
                type: agent
                goal: 写报告
                allowed_tools: [file_write]
            """).steps[0]
        out = await runner(step, "写报告")
        assert asked == ["file_write"], "越权工具不该弹审批，允许的工具照常走审批链路"
        assert out["denied_tools"] == ["terminal"]


def test_unknown_field_error_points_at_its_step_line() -> None:
    """未知字段的值是标量、自身没有行号 —— 退到所在步骤的行号，界面点击能跳过去。"""
    schema, errors, _ = load("""
        version: 1
        name: a
        steps:
          - id: s
            type: tool
            tool: recorder
            on_failur: continue
        """)
    assert schema is None
    issue = next(e for e in errors if "on_failur" in e.message)
    assert issue.position.line == 4
