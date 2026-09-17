"""执行器测试 —— 顺序、失败策略、fail-closed、dry-run、报告、取消。

这些用例的共同前提是**离线**：工具是假工具（在进程内实现 `AbstractTool`），
模型是假模型。被测的是执行器自己的语义，不是某个真实系统能不能连上。

其中三组断言值得单独说：
  · `TestHumanFailClosed` —— "没有审批通道 = 拒绝"。这是安全属性，
    不是功能属性：它错了的后果是"需要人批的生产变更被静默执行"。
  · `TestDryRun` —— 断言的是"工具**一次都没被调用**"。dry-run 的承诺是
    "不产生副作用"，只能靠调用计数证明，靠输出长得像不像证明不了。
  · `TestFailureNeverLooksLikeSuccess` —— 失败不许被记成成功。
    这条是 CI 门禁与客户验收的判据来源。
"""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from automind.workflow.exceptions import WorkflowExecutionError
from automind.workflow.executor import (
    EXIT_CANCELLED,
    EXIT_OK,
    EXIT_STEP_FAILED,
    RUN_ABORTED,
    RUN_CANCELLED,
    RUN_DRY_RUN,
    RUN_FAILED,
    RUN_OK,
    RUN_PARTIAL,
    STATUS_ABORTED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    WorkflowExecutor,
    check_inputs,
    digest_of,
    run_workflow,
)
from automind.workflow.loader import WorkflowLoader


def build(text: str, **kw):
    return WorkflowLoader(**kw).parse(textwrap.dedent(text))


async def run(text: str, *, registry=None, llm=None, approval=None, inputs=None,
              dry_run=False, **kw):
    schema = build(text, **{k: v for k, v in kw.items() if k in ("tool_names",)})
    return await run_workflow(schema, inputs or {}, registry=registry, llm=llm,
                              approval=approval, dry_run=dry_run,
                              env={"TOKEN": "t0ken", "HOME": r"C:\u"})


TWO_STEPS = """\
version: 1
name: 两步
inputs:
  value: {type: string, default: 甲}
steps:
  - id: first
    type: tool
    tool: recorder
    args: {value: "{{ inputs.value }}"}
  - id: second
    type: tool
    tool: recorder
    args: {value: "来自第一步：{{ steps.first.output.value }}"}
"""


class TestSequentialExecution:
    async def test_runs_in_declared_order(self, registry, recorder) -> None:
        run = await self._two(registry)
        assert [s.id for s in run.steps] == ["first", "second"]
        assert [s.status for s in run.steps] == [STATUS_OK, STATUS_OK]
        assert run.status == RUN_OK and run.ok

    async def _two(self, registry):
        return await run(TWO_STEPS, registry=registry, inputs={"value": "甲"})

    async def test_result_is_passed_to_next_step(self, registry, recorder) -> None:
        await self._two(registry)
        # 第二步收到的参数必须是**渲染后**的文本，而不是模板原文
        assert recorder.calls[1]["value"] == "来自第一步：甲"

    async def test_defaults_applied(self, registry, recorder) -> None:
        await run(TWO_STEPS, registry=registry, inputs={})
        assert recorder.calls[0]["value"] == "甲"

    async def test_template_types_survive(self, registry, recorder) -> None:
        """整串模板保留原类型：工具拿到的是 int，不是 "3"。"""
        text = """\
        version: 1
        name: 类型
        inputs:
          n: {type: integer, default: 3}
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: "{{ inputs.n }}"}
        """
        await run(text, registry=registry, inputs={})
        assert recorder.calls[0]["value"] == 3
        assert isinstance(recorder.calls[0]["value"], int)

    async def test_nested_args_rendered(self, registry, recorder) -> None:
        text = """\
        version: 1
        name: 嵌套
        inputs:
          id: {type: string, default: INC1}
        steps:
          - id: a
            type: tool
            tool: recorder
            args:
              body:
                ticket: "{{ inputs.id }}"
                list: ["{{ inputs.id }}-x"]
        """
        await run(text, registry=registry)
        assert recorder.calls[0]["body"] == {"ticket": "INC1", "list": ["INC1-x"]}

    async def test_progress_events_emitted(self, registry) -> None:
        events: list[tuple[str, dict]] = []

        def sink(event, payload):
            events.append((event, payload))

        schema = build(TWO_STEPS)
        await run_workflow(schema, {"value": "甲"}, registry=registry, on_event=sink)
        names = [e for e, _ in events]
        assert names.count("step_start") == 2
        assert names.count("step_end") == 2
        assert names[-1] == "run_end"

    async def test_event_sink_failure_does_not_break_run(self, registry) -> None:
        """进度是观测，不是流程的一部分：回调炸了不该让跑完的流程白费。"""

        def bad_sink(event, payload):
            raise RuntimeError("进度回调炸了")

        result = await run_workflow(build(TWO_STEPS), {"value": "甲"},
                                    registry=registry, on_event=bad_sink)
        assert result.status == RUN_OK

    async def test_async_event_sink_supported(self, registry) -> None:
        seen: list[str] = []

        async def sink(event, payload):
            seen.append(event)

        await run_workflow(build(TWO_STEPS), {"value": "甲"}, registry=registry,
                           on_event=sink)
        assert "step_start" in seen and "run_end" in seen


class TestToolFailures:
    """三种 on_failure 行为，以及"失败就是失败"。"""

    THREE = """\
    version: 1
    name: 失败策略
    steps:
      - id: a
        type: tool
        tool: recorder
        on_failure: {policy}
      - id: b
        type: tool
        tool: recorder
    """

    async def test_abort_stops_the_rest(self, registry, recorder) -> None:
        recorder.failures = ["接口 500"]
        result = await run(textwrap.dedent(self.THREE).format(policy="abort"),
                           registry=registry)
        assert result.status == RUN_ABORTED
        assert result.steps[0].status == STATUS_ABORTED
        assert result.steps[1].status == STATUS_SKIPPED
        assert "接口 500" in result.steps[0].error
        assert recorder.call_count == 1                     # 第二步真的没跑
        assert result.exit_code == EXIT_STEP_FAILED

    async def test_continue_keeps_going_but_records_failure(self, registry, recorder) -> None:
        recorder.failures = ["接口 500"]
        result = await run(textwrap.dedent(self.THREE).format(policy="continue"),
                           registry=registry)
        assert result.status == RUN_PARTIAL               # **不是** ok
        assert result.steps[0].status == STATUS_FAILED     # 失败就是失败
        assert result.steps[1].status == STATUS_OK
        assert recorder.call_count == 2
        assert not result.ok
        assert result.exit_code == EXIT_STEP_FAILED

    async def test_retry_then_success(self, registry, recorder) -> None:
        recorder.failures = ["第一次失败"]
        result = await run(textwrap.dedent(self.THREE).format(policy="retry(2)"),
                           registry=registry)
        assert result.steps[0].status == STATUS_OK
        assert result.steps[0].retries == 1
        # a 尝试 2 次（首次失败 + 重试成功），b 一次 —— 断言"这一步调用了几次"，
        # 而不是整表总调用次数（后者会把后面的步骤也算进来，看不出重试行为）
        assert result.steps[0].detail["attempts"] == 2
        assert result.steps[1].detail["attempts"] == 1
        assert recorder.call_count == 3
        assert result.status == RUN_OK

    async def test_retry_exhausted_records_failure_and_retry_count(
            self, registry, recorder) -> None:
        recorder.failures = ["坏", "还是坏", "仍然坏"]
        result = await run(textwrap.dedent(self.THREE).format(policy="retry(2)"),
                           registry=registry)
        assert result.steps[0].status == STATUS_ABORTED
        assert result.steps[0].retries == 2
        assert result.steps[0].detail["attempts"] == 3        # 首次 + 2 次重试
        assert "已重试 2 次" in result.steps[0].error
        assert result.status == RUN_ABORTED                  # 耗尽后按 abort 处理
        assert result.steps[1].status == STATUS_SKIPPED
        assert recorder.call_count == 3                      # b 一次都没跑

    async def test_tool_exception_becomes_failure_not_crash(self, registry, recorder) -> None:
        recorder.raise_error = RuntimeError("工具内部炸了")
        result = await run(textwrap.dedent(self.THREE).format(policy="abort"),
                           registry=registry)
        assert result.steps[0].status == STATUS_ABORTED
        assert "工具内部炸了" in result.steps[0].error
        # registry.dispatch 会把异常转成失败结果，因此这里仍应看到失败而非抛错
        assert result.status in (RUN_ABORTED, RUN_FAILED)

    async def test_unknown_tool_fails_with_suggestion(self, registry) -> None:
        text = """\
        version: 1
        name: 工具名写错
        steps:
          - id: a
            type: tool
            tool: recorer
        """
        result = await run(text, registry=registry)
        assert result.steps[0].status == RUN_ABORTED
        assert "recorder" in result.steps[0].error          # dispatch 给出建议

    async def test_step_timeout(self, registry, recorder) -> None:
        recorder.delay = 0.4
        text = """\
        version: 1
        name: 超时
        steps:
          - id: a
            type: tool
            tool: recorder
            timeout: 0.05
        """
        result = await run(text, registry=registry)
        assert result.steps[0].status == RUN_ABORTED
        assert "步骤超时" in result.steps[0].error
        assert result.steps[0].duration_ms < 400

    async def test_missing_registry_is_explicit(self) -> None:
        text = """\
        version: 1
        name: 没注入注册表
        steps:
          - id: a
            type: tool
            tool: recorder
        """
        result = await run(text, registry=None)
        assert result.steps[0].status == RUN_ABORTED
        assert "没有注入 registry" in result.steps[0].error


class TestLlmSteps:
    async def test_generates_once_and_passes_forward(self, registry) -> None:
        from tests.workflow.conftest import FakeLLM

        llm = FakeLLM("生成结果")
        text = """\
        version: 1
        name: llm 步骤
        inputs:
          topic: {type: string, default: 变更}
        steps:
          - id: draft
            type: llm
            system: 你是助手
            prompt: "请写：{{ inputs.topic }}"
          - id: use
            type: tool
            tool: recorder
            args: {value: "{{ steps.draft.output }}"}
        """
        result = await run(text, registry=registry, llm=llm)
        assert result.steps[0].status == STATUS_OK
        assert len(llm.calls) == 1                          # **只生成一次**，不做规划
        assert llm.calls[0][0] == {"role": "system", "content": "你是助手"}
        assert llm.calls[0][1]["content"] == "请写：变更"
        assert registry.get("recorder").calls[0]["value"] == "生成结果"

    async def test_missing_llm_is_explicit_error(self, registry) -> None:
        text = """\
        version: 1
        name: 没注入模型
        steps:
          - id: draft
            type: llm
            prompt: 写点什么
        """
        result = await run(text, registry=registry, llm=None)
        assert result.steps[0].status == STATUS_ABORTED
        assert "没有注入 llm" in result.steps[0].error

    async def test_plain_string_response_accepted(self, registry) -> None:
        class RawLLM:
            async def generate(self, messages, **kw):
                return "裸字符串"

        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: llm\n    prompt: p\n")
        result = await run(text, registry=registry, llm=RawLLM())
        assert result.steps[0].status == STATUS_OK

    async def test_unrecognized_response_is_failure(self, registry) -> None:
        class WeirdLLM:
            async def generate(self, messages, **kw):
                return 12345

        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: llm\n    prompt: p\n")
        result = await run(text, registry=registry, llm=WeirdLLM())
        assert result.steps[0].status == RUN_ABORTED
        assert "无法识别的响应类型" in result.steps[0].error

    async def test_prompt_not_echoed_in_report(self, registry) -> None:
        from tests.workflow.conftest import FakeLLM

        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: llm\n"
                "    prompt: \"含敏感内容 {{ env.TOKEN }}\"\n")
        result = await run(text, registry=registry, llm=FakeLLM())
        # 报告会进日志/前端/CI 产物，提示词只留摘要不留原文
        assert "t0ken" not in str(result.as_dict())


class TestHumanFailClosed:
    """**没有审批通道 = 拒绝**。这是安全属性，不是功能属性。"""

    HUMAN = """\
    version: 1
    name: 审批
    steps:
      - id: approve
        type: human
        prompt: "请批准：{{ env.TOKEN }}"
      - id: after
        type: tool
        tool: recorder
    """

    async def test_no_callback_denies_and_records(self, registry, recorder) -> None:
        result = await run(textwrap.dedent(self.HUMAN), registry=registry, approval=None)
        assert result.steps[0].status == STATUS_ABORTED
        assert "按**拒绝**处理" in result.steps[0].error or "拒绝" in result.steps[0].error
        assert result.steps[0].detail["approval"] == "unavailable"
        assert result.steps[1].status == STATUS_SKIPPED
        assert recorder.call_count == 0                     # 关键：后续步骤没被执行
        assert result.status == RUN_ABORTED

    async def test_approved(self, registry, approval_recorder) -> None:
        approve = approval_recorder(approved=True)
        result = await run(textwrap.dedent(self.HUMAN), registry=registry, approval=approve)
        assert result.steps[0].status == STATUS_OK
        assert result.steps[0].detail["approval"] == "approved"
        assert result.steps[1].status == STATUS_OK

    async def test_prompt_is_rendered_before_asking(self, registry, approval_recorder) -> None:
        approve = approval_recorder(approved=True)
        await run(textwrap.dedent(self.HUMAN), registry=registry, approval=approve)
        assert approve.prompts == ["请批准：t0ken"]
        assert approve.steps == ["approve"]

    async def test_denied(self, registry, approval_recorder) -> None:
        approve = approval_recorder(approved=False, comment="回滚方案缺失")
        result = await run(textwrap.dedent(self.HUMAN), registry=registry, approval=approve)
        assert result.steps[0].status == STATUS_ABORTED
        assert "人工审批未通过" in result.steps[0].error
        assert "回滚方案缺失" in result.steps[0].error
        assert result.steps[1].status == STATUS_SKIPPED

    async def test_channel_exception_counts_as_denial(self, registry, approval_recorder) -> None:
        approve = approval_recorder(error=RuntimeError("通道断开"))
        result = await run(textwrap.dedent(self.HUMAN), registry=registry, approval=approve)
        assert result.steps[0].status == STATUS_ABORTED
        assert "审批通道异常" in result.steps[0].error

    async def test_dict_form_returned_by_web_callback(self, registry) -> None:
        async def cb(step, prompt):
            return {"approved": True, "comment": "同意"}

        result = await run(textwrap.dedent(self.HUMAN), registry=registry, approval=cb)
        assert result.steps[0].status == STATUS_OK
        assert result.steps[0].detail["comment"] == "同意"

    async def test_truthy_junk_is_not_approval(self, registry) -> None:
        """回调返回 None / 空 dict 等无法识别的值时，必须按拒绝处置。"""

        async def cb(step, prompt):
            return None

        result = await run(textwrap.dedent(self.HUMAN), registry=registry, approval=cb)
        assert result.steps[0].status == STATUS_ABORTED


class TestBranch:
    BRANCH = """\
    version: 1
    name: 分支
    inputs:
      mode: {type: string, default: ok}
    steps:
      - id: check
        type: tool
        tool: recorder
        args: {value: "{{ inputs.mode }}"}
      - id: decide
        type: branch
        condition: "{{ steps.check.output.value }} == ok"
        then: good
        else: bad
      - id: good
        type: tool
        tool: recorder
        args: {value: 走了好分支}
      - id: bad
        type: tool
        tool: recorder
        args: {value: 走了坏分支}
    """

    async def test_then_branch(self, registry, recorder) -> None:
        result = await run(textwrap.dedent(self.BRANCH), registry=registry,
                           inputs={"mode": "ok"})
        statuses = {s.id: s.status for s in result.steps}
        assert statuses["good"] == STATUS_OK
        assert statuses["bad"] == STATUS_SKIPPED
        assert recorder.calls[-1]["value"] == "走了好分支"
        assert result.steps[1].detail["matched"] is True

    async def test_else_branch(self, registry, recorder) -> None:
        result = await run(textwrap.dedent(self.BRANCH), registry=registry,
                           inputs={"mode": "别的"})
        statuses = {s.id: s.status for s in result.steps}
        assert statuses["good"] == STATUS_SKIPPED
        assert statuses["bad"] == STATUS_OK
        assert recorder.calls[-1]["value"] == "走了坏分支"

    async def test_skip_reason_recorded(self, registry) -> None:
        result = await run(textwrap.dedent(self.BRANCH), registry=registry,
                           inputs={"mode": "ok"})
        skipped = next(s for s in result.steps if s.id == "bad")
        assert "branch" in skipped.error                    # 报告说明为什么没跑
        assert "未执行" in skipped.error

    async def test_contains_operator(self, registry) -> None:
        text = textwrap.dedent(self.BRANCH).replace(
            "{{ steps.check.output.value }} == ok", "{{ inputs.mode }} contains 坏")
        result = await run(text, registry=registry, inputs={"mode": "有坏消息"})
        assert {s.id: s.status for s in result.steps}["good"] == STATUS_OK

    async def test_not_equal_operator(self, registry) -> None:
        text = textwrap.dedent(self.BRANCH).replace("== ok", "!= ok")
        result = await run(text, registry=registry, inputs={"mode": "坏"})
        assert {s.id: s.status for s in result.steps}["good"] == STATUS_OK

    async def test_referential_integrity_double_check(self, registry) -> None:
        """即使绕过加载器（程序化构造）也不许把不存在的目标当成功。

        目标不存在 = 控制流坏了，必须**中止**而不是按 on_failure 放行：
        放行会让主循环从当前位置继续往下走，把两条互斥分支都执行掉 ——
        那是"报告全绿、实际多做了事"的静默错误。
        """
        schema = build(textwrap.dedent(self.BRANCH))
        schema.get_step("decide").then = "不存在"            # type: ignore[union-attr]
        result = await WorkflowExecutor(
            schema, inputs={"mode": "ok"}, registry=registry).run()
        decide = next(s for s in result.steps if s.id == "decide")
        assert decide.status == STATUS_ABORTED
        assert "跳转目标不存在" in decide.error
        assert result.status == RUN_ABORTED


class TestFailureNeverLooksLikeSuccess:
    """失败不许被记成成功 —— CI 门禁与客户验收的判据来源。"""

    async def test_partial_run_is_not_ok(self, registry, recorder) -> None:
        recorder.failures = ["失败"]
        text = """\
        version: 1
        name: 部分成功
        steps:
          - id: a
            type: tool
            tool: recorder
            on_failure: continue
          - id: b
            type: tool
            tool: recorder
        """
        result = await run(text, registry=registry)
        assert result.status == RUN_PARTIAL
        assert not result.ok
        assert result.failed == 1 and result.succeeded == 1

    async def test_summary_counts_match_steps(self, registry, recorder) -> None:
        recorder.failures = ["失败"]
        text = """\
        version: 1
        name: 计数
        steps:
          - id: a
            type: tool
            tool: recorder
            on_failure: continue
          - id: b
            type: tool
            tool: recorder
        """
        result = await run(text, registry=registry)
        data = result.as_dict()
        assert data["summary"]["total"] == 2
        assert data["summary"]["failed"] == 1
        assert data["summary"]["ok"] is False
        assert data["summary"]["exit_code"] == EXIT_STEP_FAILED


class TestReport:
    async def test_required_fields_present(self, registry) -> None:
        result = await run(TWO_STEPS, registry=registry, inputs={"value": "甲"})
        for step in result.steps:
            assert step.id and step.type
            assert step.status in (STATUS_OK, STATUS_FAILED, STATUS_SKIPPED,
                                   STATUS_ABORTED, STATUS_CANCELLED)
            assert step.started_at and step.finished_at
            assert step.duration_ms >= 0
            assert step.output_digest                          # ok 的步骤必须有摘要
        assert result.run_id and result.started_at and result.finished_at
        assert result.duration_ms >= 0
        assert result.version == 1
        assert result.source_digest

    async def test_json_serializable(self, registry) -> None:
        import json

        result = await run(TWO_STEPS, registry=registry)
        payload = json.loads(result.to_json())
        assert payload["workflow"] == "两步"
        assert len(payload["steps"]) == 2

    async def test_outputs_available_on_demand(self, registry) -> None:
        result = await run(TWO_STEPS, registry=registry, inputs={"value": "甲"})
        assert result.outputs["first"] == {"value": "甲"}
        data = result.as_dict()
        assert "outputs" not in data                           # 默认不带全文
        assert "outputs" in result.as_dict(include_outputs=True)

    async def test_digest_is_stable_and_order_independent(self) -> None:
        assert digest_of({"a": 1, "b": 2}) == digest_of({"b": 2, "a": 1})
        assert digest_of({"a": 1}) != digest_of({"a": 2})

    async def test_lines_point_back_to_file(self, registry) -> None:
        result = await run(TWO_STEPS, registry=registry)
        # 报告行号必须与文件里的定义位置一致（"点击报告跳到定义"依赖它）
        assert result.steps[0].line == 6
        assert result.steps[1].line == 10
        assert result.steps[0].line < result.steps[1].line

    async def test_warnings_carried_into_report(self, registry) -> None:
        text = """\
        version: 1
        name: 前向引用带警告
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: "{{ steps.b.output }}"}
          - id: b
            type: tool
            tool: recorder
        """
        result = await run(text, registry=registry)
        assert result.warnings                               # 不许只在加载时闪一下


class TestDryRun:
    """dry-run 的承诺是"不产生副作用" —— 只能靠调用计数证明。"""

    async def test_tools_never_called(self, registry, recorder) -> None:
        result = await run(TWO_STEPS, registry=registry, dry_run=True,
                           inputs={"value": "甲"})
        assert recorder.call_count == 0
        assert result.status == RUN_DRY_RUN
        assert result.dry_run is True
        assert all(s.detail.get("dry_run") for s in result.steps)

    async def test_llm_never_called(self, registry) -> None:
        from tests.workflow.conftest import FakeLLM

        llm = FakeLLM()
        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: llm\n    prompt: p\n")
        result = await run(text, registry=registry, llm=llm, dry_run=True)
        assert llm.calls == []
        assert result.status == RUN_DRY_RUN

    async def test_approval_never_asked(self, registry, approval_recorder) -> None:
        approve = approval_recorder()
        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: human\n    prompt: p\n")
        result = await run(text, registry=registry, approval=approve, dry_run=True)
        assert approve.prompts == []
        assert result.status == RUN_DRY_RUN

    async def test_renders_args_and_lists_plan(self, registry) -> None:
        result = await run(TWO_STEPS, registry=registry, dry_run=True,
                           inputs={"value": "甲"})
        first = result.steps[0]
        assert first.detail["tool"] == "recorder"
        assert first.detail["args"] == {"value": "甲"}
        assert "plan" in first.detail                         # 工具的 get_execution_plan

    async def test_dry_run_exit_code_is_zero(self, registry) -> None:
        result = await run(TWO_STEPS, registry=registry, dry_run=True)
        assert result.exit_code == EXIT_OK
        assert result.ok

    async def test_dry_run_still_reports_bad_template(self, registry) -> None:
        """dry-run 的主要用途就是把模板错误提前暴露出来。"""
        text = """\
        version: 1
        name: 前向引用在 dry-run 下取不到值
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: "{{ steps.b.output }}"}
          - id: b
            type: tool
            tool: recorder
        """
        result = await run(text, registry=registry, dry_run=True)
        assert result.steps[0].status == STATUS_FAILED
        assert "试运行渲染失败" in result.steps[0].error
        assert not result.ok

    async def test_dry_run_marks_unknown_tool(self, registry) -> None:
        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: tool\n"
                "    tool: 不存在的工具\n")
        result = await run(text, registry=registry, dry_run=True)
        assert "不在当前注册表中" in result.steps[0].detail.get("warning", "")


class TestCancellation:
    """取消必须传播，同时状态要留在报告里。"""

    async def test_cancelled_error_propagates(self, registry, recorder) -> None:
        recorder.delay = 5.0
        schema = build(TWO_STEPS)
        task = asyncio.create_task(
            run_workflow(schema, {"value": "甲"}, registry=registry))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_report_marks_cancelled_step(self, registry, recorder) -> None:
        recorder.delay = 5.0
        schema = build(TWO_STEPS)
        executor = WorkflowExecutor(schema, inputs={"value": "甲"}, registry=registry)
        task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 取消后仍要能看出"停在哪一步"（报告通过执行器状态保留）
        assert executor.run_cancelled is True

    async def test_cancel_between_events_does_not_hang(self, registry) -> None:
        """事件回调里取消：不许把取消吞掉变成"卡住"。"""

        async def sink(event, payload):
            if event == "step_start":
                raise asyncio.CancelledError()

        schema = build(TWO_STEPS)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                run_workflow(schema, {"value": "甲"}, registry=registry, on_event=sink),
                timeout=2.0)


class TestCheckInputs:
    def test_missing_required(self) -> None:
        schema = build("version: 1\nname: x\ninputs:\n  a: {type: string, required: true}\n"
                       "steps:\n  - id: s\n    type: tool\n    tool: recorder\n")
        final, errors, warnings = check_inputs(schema, {})
        assert errors and "缺少必需入参 'a'" in errors[0]
        assert not warnings

    def test_defaults_filled(self) -> None:
        schema = build("version: 1\nname: x\ninputs:\n  a: {type: string, default: d}\n"
                       "steps:\n  - id: s\n    type: tool\n    tool: recorder\n")
        final, errors, warnings = check_inputs(schema, {})
        assert final == {"a": "d"} and not errors and not warnings

    def test_optional_without_default_becomes_none(self) -> None:
        schema = build("version: 1\nname: x\ninputs:\n  a: {type: string}\n"
                       "steps:\n  - id: s\n    type: tool\n    tool: recorder\n")
        final, errors, _ = check_inputs(schema, {})
        assert final == {"a": None} and not errors

    def test_unknown_input_warns(self) -> None:
        schema = build("version: 1\nname: x\ninputs:\n  ticket_id: {type: string}\n"
                       "steps:\n  - id: s\n    type: tool\n    tool: recorder\n")
        _, _, warnings = check_inputs(schema, {"ticketid": "x"})
        assert warnings and "ticket_id" in warnings[0]        # 给出最像的名字


class TestModuleHelpers:
    async def test_workflow_execution_error_is_raised_for_missing_deps(self) -> None:
        from automind.workflow.executor import StepReport

        schema = build("version: 1\nname: x\nsteps:\n  - id: a\n    type: tool\n"
                       "    tool: recorder\n")
        executor = WorkflowExecutor(schema, registry=None)
        with pytest.raises(WorkflowExecutionError):
            # 直接调内部方法，验证它抛的是可识别的类型（上层据此给指引）
            await executor._run_tool(                        # noqa: SLF001
                schema.steps[0], StepReport(id="a", type="tool"))

    async def test_cancelled_exit_code_constant(self) -> None:
        assert EXIT_CANCELLED == 130
        assert RUN_CANCELLED == "cancelled"
