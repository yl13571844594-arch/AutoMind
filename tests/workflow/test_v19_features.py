"""v1.9.0 引擎新能力：cron / 触发器声明 / parallel 组 / foreach 逐项并发 / 条件扩展。

对应 docs/WORKFLOW_ROADMAP.md 的 R-201、R-204 ~ R-206、R-208、R-209 与设计规则 D2。
这里只测**社区核心**的那一半：加载期校验与拦截、执行器语义。并发执行器本身是
专业版特性（automind_pro 的 workflow_parallel），本文件注入一个测试用的有界并发
执行器，语义与专业版一致；调度循环、入站 webhook、看板见 pro/tests。
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from datetime import datetime
from typing import Any

import pytest

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool, ToolRegistry
from automind.workflow.conditions import check_condition, split_condition
from automind.workflow.cron import CronError, CronExpr
from automind.workflow.executor import (
    RUN_ABORTED,
    RUN_DRY_RUN,
    RUN_OK,
    RUN_PARTIAL,
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    run_group_sequential,
    run_workflow,
)
from automind.workflow.loader import WorkflowLoader

PRO = {"workflow_parallel", "workflow_triggers", "workflow_history"}


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


async def bounded_runner(jobs, *, max_concurrency: int = 4, stop=None) -> list[Any]:
    """测试用有界并发执行器（与专业版 workflow_parallel.run_group 同语义）。"""
    sem = asyncio.Semaphore(max_concurrency)
    results: list[Any] = [None] * len(jobs)

    async def _one(i: int, job) -> None:
        async with sem:
            if stop is not None and stop.is_set():
                return
            results[i] = await job()

    async with asyncio.TaskGroup() as tg:
        for i, job in enumerate(jobs):
            tg.create_task(_one(i, job))
    return results


class Probe(AbstractTool):
    """按 value 定制延迟 / 失败的假工具；记录调用与最大同时在跑的个数。"""

    name = "probe"
    description = "测试用"
    parameters = {"type": "object", "properties": {"value": {}, "delay": {"type": "number"}}}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail: dict[str, int] = {}
        self.inflight = 0
        self.peak = 0

    def count(self, value: str) -> int:
        return sum(1 for c in self.calls if str(c.get("value")) == value)

    async def execute(self, **kw: Any) -> ToolResult:
        self.calls.append(kw)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            delay = float(kw.get("delay") or 0)
            if delay:
                await asyncio.sleep(delay)
        finally:
            self.inflight -= 1
        value = str(kw.get("value"))
        if self.fail.get(value):
            self.fail[value] -= 1
            return ToolResult(tool_name=self.name, success=False, error=f"{value} 失败")
        return ToolResult(tool_name=self.name, success=True, output={"value": kw.get("value")})


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def reg(probe: Probe) -> ToolRegistry:
    r = ToolRegistry()
    r.register(probe)
    return r


# ═══════════════════════════════════════════════════════════════
# cron（R-201）
# ═══════════════════════════════════════════════════════════════


class TestCron:
    @pytest.mark.parametrize(("text", "gap"), [
        ("0 9 * * 1-5", 86400), ("*/5 * * * *", 300), ("* * * * *", 60),
        ("@hourly", 3600), ("15,45 8-18 * * mon-fri", 1800), ("*/7 * * * *", 240),
    ])
    def test_min_interval(self, text: str, gap: int) -> None:
        assert CronExpr.parse(text).min_interval_seconds() == gap

    def test_next_after_weekday_rule(self) -> None:
        expr = CronExpr.parse("0 9 * * 1-5")
        # 2026-10-03 是周六 → 下一次是周一 10-05 09:00
        assert expr.next_after(datetime(2026, 10, 3, 12, 0)) == datetime(2026, 10, 5, 9, 0)
        assert expr.next_after(datetime(2026, 10, 5, 9, 0)) == datetime(2026, 10, 6, 9, 0)

    def test_day_of_month_or_weekday_is_union(self) -> None:
        expr = CronExpr.parse("0 0 1 * 1")              # 每月 1 号 **或** 每周一
        assert expr.matches(datetime(2026, 10, 1, 0, 0))   # 周四、1 号
        assert expr.matches(datetime(2026, 10, 5, 0, 0))   # 周一

    def test_sunday_is_0_and_7(self) -> None:
        assert CronExpr.parse("0 0 * * 7").weekdays == CronExpr.parse("0 0 * * 0").weekdays

    @pytest.mark.parametrize(("text", "needle"), [
        ("0 9 * *", "正好有 5 段"), ("0 0 0 0 0 0", "不支持秒字段"),
        ("0 0 30 2 *", "永远不会触发"), ("0 0 * * 8", "超出范围"),
        ("x * * * *", "无法识别"), ("0 0 L * *", "扩展写法"), ("5-1 * * * *", "起点大于终点"),
        ("*/0 * * * *", "步长"), ("@often", "不认识的 cron 宏"),
    ])
    def test_bad_expressions(self, text: str, needle: str) -> None:
        with pytest.raises(CronError, match=needle):
            CronExpr.parse(text)


# ═══════════════════════════════════════════════════════════════
# 触发器 / on_restart / notify 的声明（R-201 / R-202 / R-208 / R-209）
# ═══════════════════════════════════════════════════════════════


TRIGGERED = """
version: 1
name: 日报
inputs:
  region: {type: string, required: true}
  days: {type: integer, default: 1}
triggers:
  - cron: "0 9 * * 1-5"
    inputs: {region: east}
    retry: 2
  - id: from_ci
    webhook: true
on_restart: resume
notify:
  on: [failed, partial]
  channels: [webhook, im]
steps:
  - id: s
    type: tool
    tool: probe
    args: {value: "{{ inputs.region }}"}
"""


class TestTriggerSchema:
    def test_valid_declaration(self) -> None:
        schema, errors, warnings = load(TRIGGERED)
        assert schema is not None, [e.format() for e in errors]
        cron, hook = schema.triggers
        assert (cron.id, cron.kind, cron.cron, cron.retry, cron.min_interval) == (
            "t1", "cron", "0 9 * * 1-5", 2, 86400)
        assert (hook.id, hook.kind) == ("from_ci", "webhook")
        assert schema.on_restart == "resume"
        assert schema.notify.on == ["failed", "partial"] and schema.notify.declared
        # webhook 的必填入参由调用方在请求体里给：不提醒（缺了那次触发返回 422）
        assert not warnings, [w.message for w in warnings]
        assert schema.as_dict()["triggers"][0]["cron"] == "0 9 * * 1-5"
        assert "触发器" in schema.summarize()

    def test_community_is_blocked_at_load_time_with_line(self) -> None:
        schema, errors, _ = load(TRIGGERED, features=set())
        assert schema is None
        msgs = [e.message for e in errors]
        assert any("顶层字段 'triggers' 需要专业版" in m for m in msgs)
        assert any("'on_restart'" in m for m in msgs) and any("'notify'" in m for m in msgs)
        trig = next(e for e in errors if "'triggers'" in e.message)
        assert trig.position.line == 7          # 指向 triggers 列表的第一项

    def test_min_interval_enforced(self) -> None:
        text = TRIGGERED.replace('"0 9 * * 1-5"', '"*/1 * * * *"')
        msgs = errors_of(text)
        assert any("最短每 1 分钟" in m and "5 分钟" in m for m in msgs), msgs
        # 企业版下限 60 秒：同一条就能过
        assert load(text, min_interval=60)[0] is not None

    def test_cron_missing_required_input_is_error(self) -> None:
        text = TRIGGERED.replace("    inputs: {region: east}\n", "")
        assert any("定时触发没有给出必填入参：region" in m for m in errors_of(text))

    @pytest.mark.parametrize(("old", "new", "needle"), [
        ("inputs: {region: east}", "inputs: {regoin: east}", "未声明的入参 'regoin'"),
        ("  - id: from_ci\n    webhook: true", "  - cron: '0 1 * * *'\n    webhook: true",
         "只能是 cron 或 webhook 之一"),
        ("  - id: from_ci\n    webhook: true", "  - id: from_ci", "缺少 cron"),
        ("webhook: true", "webhook: https://x", "请写 webhook: true"),
        ("  - id: from_ci", "  - id: t1", "触发器 id 重复"),
        ('"0 9 * * 1-5"', '"0 9 * *"', "cron 表达式不合法"),
        ("retry: 2", "retry: 9", "retry 过大"),
        ("on_restart: resume", "on_restart: always", "on_restart 只能是"),
        ("channels: [webhook, im]", "channels: [email]", "notify.channels 不支持 'email'"),
        ("    retry: 2", "    retyr: 2", "未知字段 'retyr'"),
    ])
    def test_bad_declarations(self, old: str, new: str, needle: str) -> None:
        assert old in TRIGGERED
        msgs = errors_of(TRIGGERED.replace(old, new))
        assert any(needle in m for m in msgs), msgs

    def test_on_restart_without_triggers_warns(self) -> None:
        text = TRIGGERED.split("triggers:")[0] + "on_restart: resume\nsteps:" + \
            TRIGGERED.split("steps:")[1]
        schema, _, warnings = load(text)
        assert schema is not None
        assert any("只对触发器发起的运行生效" in w.message for w in warnings)


# ═══════════════════════════════════════════════════════════════
# parallel 组的校验（R-204）
# ═══════════════════════════════════════════════════════════════


PARALLEL = """
version: 1
name: 并行
steps:
  - id: prep
    type: tool
    tool: probe
    args: {value: prep}
  - id: fan
    type: parallel
    max_concurrency: 3
    steps:
      - id: a
        type: tool
        tool: probe
        args: {value: a, delay: 0.3}
      - id: b
        type: tool
        tool: probe
        args: {value: b, delay: 0.3}
      - id: c
        type: tool
        tool: probe
        args: {value: "{{ steps.prep.output.value }}-c", delay: 0.3}
  - id: after
    type: tool
    tool: probe
    args: {value: "{{ steps.a.output.value }}+{{ steps.fan.output.b.value }}"}
"""


class TestParallelSchema:
    def test_loads_and_children_visible_after_group(self) -> None:
        schema = build(PARALLEL)
        fan = schema.get_step("fan")
        assert fan is not None and fan.max_concurrency == 3
        assert [c.id for c in fan.substeps] == ["a", "b", "c"]
        assert all(c.parent == "fan" for c in fan.substeps)

    def test_default_concurrency_is_4(self) -> None:
        assert build(PARALLEL.replace("    max_concurrency: 3\n", "")).get_step(
            "fan").max_concurrency == 4

    def test_community_blocked(self) -> None:
        msgs = errors_of(PARALLEL, features=set())
        assert any("步骤类型 'parallel' 需要专业版" in m for m in msgs)

    def test_sibling_reference_rejected(self) -> None:
        text = PARALLEL.replace('args: {value: b, delay: 0.3}',
                                'args: {value: "{{ steps.a.output }}"}')
        assert any("同在并行组 'fan' 里" in m for m in errors_of(text))

    def test_own_group_reference_rejected(self) -> None:
        text = PARALLEL.replace('args: {value: b, delay: 0.3}',
                                'args: {value: "{{ steps.fan.output }}"}')
        assert any("不能在并行组 'fan' 的子步骤里引用它自己的输出" in m for m in errors_of(text))

    def test_reference_before_group_is_forward_warning(self) -> None:
        text = PARALLEL.replace("args: {value: prep}", 'args: {value: "{{ steps.a.output }}"}')
        schema, _, warnings = load(text)
        assert schema is not None and any("后面的步骤" in w.message for w in warnings)

    @pytest.mark.parametrize("child_type", ["branch", "foreach", "parallel"])
    def test_disallowed_child_types(self, child_type: str) -> None:
        text = PARALLEL.replace("      - id: b\n        type: tool",
                                f"      - id: b\n        type: {child_type}")
        assert any("parallel 的子步骤不支持" in m for m in errors_of(text))

    @pytest.mark.parametrize("value", ["0", "33", "many"])
    def test_bad_concurrency(self, value: str) -> None:
        assert any("max_concurrency" in m
                   for m in errors_of(PARALLEL.replace("max_concurrency: 3",
                                                       f"max_concurrency: {value}")))

    def test_single_child_warns(self) -> None:
        text = PARALLEL.split("      - id: b")[0] + "  - id: after\n    type: tool\n    tool: probe\n"
        schema, _, warnings = load(text)
        assert schema is not None and any("只有一个子步骤" in w.message for w in warnings)

    def test_foreach_concurrency_is_gated(self) -> None:
        text = """
            version: 1
            name: f
            steps:
              - id: each
                type: foreach
                items: [1, 2]
                max_concurrency: 2
                steps:
                  - {id: x, type: tool, tool: probe}
            """
        assert build(text).steps[0].max_concurrency == 2
        assert any("foreach 逐项并发" in m for m in errors_of(text, features=set()))
        # max_concurrency: 1 就是顺序执行，社区版可写
        assert load(text.replace("max_concurrency: 2", "max_concurrency: 1"),
                    features=set())[0] is not None


# ═══════════════════════════════════════════════════════════════
# parallel 组的执行（R-204 / D2）
# ═══════════════════════════════════════════════════════════════


class TestParallelExecution:
    async def test_children_run_concurrently(self, reg, probe) -> None:
        t0 = time.perf_counter()
        run = await run_workflow(build(PARALLEL), {}, registry=reg, group_runner=bounded_runner)
        elapsed = time.perf_counter() - t0
        assert run.status == RUN_OK, run.as_dict()
        assert elapsed < 0.75, f"3 个 0.3s 的子步骤应当并发（实际 {elapsed:.2f}s）"
        assert probe.peak == 3
        fan = run.steps[1]
        assert [c["id"] for c in fan.detail["children"]] == ["a", "b", "c"]
        assert run.outputs["fan"]["c"] == {"value": "prep-c"}
        # 组外引用：子步骤 id 与组输出两种写法都能取到
        assert run.outputs["after"] == {"value": "a+b"}
        # 报告只列顶层步骤（与文件逐行对应），子步骤在组的 detail 里
        assert [s.id for s in run.steps] == ["prep", "fan", "after"]

    async def test_concurrency_bound_respected(self, reg, probe) -> None:
        text = PARALLEL.replace("max_concurrency: 3", "max_concurrency: 2")
        await run_workflow(build(text), {}, registry=reg, group_runner=bounded_runner)
        assert probe.peak == 2

    async def test_sequential_fallback_has_same_semantics(self, reg, probe) -> None:
        run = await run_workflow(build(PARALLEL), {}, registry=reg,
                                 group_runner=run_group_sequential)
        assert run.status == RUN_OK and probe.peak == 1
        assert run.outputs["after"] == {"value": "a+b"}

    async def test_abort_child_fails_group_and_stops_unstarted(self, reg, probe) -> None:
        probe.fail["a"] = 1
        text = PARALLEL.replace("max_concurrency: 3", "max_concurrency: 1")
        run = await run_workflow(build(text), {}, registry=reg, group_runner=bounded_runner)
        fan = run.steps[1]
        assert fan.status == STATUS_ABORTED and run.status == RUN_ABORTED
        states = {c["id"]: c["status"] for c in fan.detail["children"]}
        assert states == {"a": STATUS_ABORTED, "b": STATUS_SKIPPED, "c": STATUS_SKIPPED}
        assert "因 abort 未启动" in fan.error
        assert probe.count("b") == 0 and run.steps[2].status == STATUS_SKIPPED

    async def test_continue_child_lets_siblings_run_but_group_fails(self, reg, probe) -> None:
        probe.fail["a"] = 1
        text = PARALLEL.replace("args: {value: a, delay: 0.3}",
                                "args: {value: a}\n        on_failure: continue").replace(
            "    max_concurrency: 3", "    max_concurrency: 3\n    on_failure: continue").replace(
            '"{{ steps.a.output.value }}+{{ steps.fan.output.b.value }}"', "after")
        run = await run_workflow(build(text), {}, registry=reg, group_runner=bounded_runner)
        fan = run.steps[1]
        assert fan.status == STATUS_FAILED and run.status == RUN_PARTIAL
        assert probe.count("b") == 1
        states = {c["id"]: c["status"] for c in fan.detail["children"]}
        assert states == {"a": STATUS_FAILED, "b": STATUS_OK, "c": STATUS_OK}
        assert run.steps[2].status == STATUS_OK

    async def test_resume_reruns_only_failed_children(self, reg, probe) -> None:
        """D2：组内已成功的子步骤续跑时跳过，只重跑失败的那个。"""
        snapshots: list[dict[str, Any]] = []
        probe.fail["b"] = 1
        schema = build(PARALLEL)
        run = await run_workflow(schema, {}, registry=reg, group_runner=bounded_runner,
                                 on_checkpoint=snapshots.append)
        assert run.status == RUN_ABORTED
        last = snapshots[-1]
        assert {"a", "c"} <= set(last["outputs"]) and "b" not in last["outputs"]
        assert last["seq"] == max(s["seq"] for s in snapshots)
        assert last["records"]["a"]["parent"] == "fan" and "parent" not in last["records"]["prep"]
        from automind.workflow.runs import summarize
        assert summarize({"records": last["records"]})["steps_done"] == 1   # 只数顶层
        resume = {"from_run": run.run_id,
                  "steps": {sid: {"output": out} for sid, out in last["outputs"].items()}}
        before = {v: probe.count(v) for v in ("prep", "a", "b", "prep-c")}
        rerun = await run_workflow(schema, {}, registry=reg, group_runner=bounded_runner,
                                   resume=resume)
        assert rerun.status == RUN_OK, rerun.as_dict()
        assert probe.count("prep") == before["prep"] and probe.count("a") == before["a"]
        assert probe.count("prep-c") == before["prep-c"]
        assert probe.count("b") == before["b"] + 1
        assert set(rerun.resumed_steps) == {"prep", "a", "c"}
        kids = {c["id"]: c for c in rerun.steps[1].detail["children"]}
        assert kids["a"]["resumed"] and not kids["b"]["resumed"]

    async def test_fully_ok_group_restores_children_outputs(self, reg, probe) -> None:
        schema = build(PARALLEL)
        resume = {"from_run": "x", "steps": {
            "prep": {"output": {"value": "prep"}},
            "fan": {"output": {"a": {"value": "A"}, "b": {"value": "B"}, "c": {"value": "C"}}}}}
        run = await run_workflow(schema, {}, registry=reg, resume=resume)
        assert run.status == RUN_OK
        assert run.outputs["after"] == {"value": "A+B"}
        assert probe.count("a") == 0

    async def test_dry_run_previews_children(self, reg, probe) -> None:
        run = await run_workflow(build(PARALLEL), {}, registry=reg, dry_run=True)
        assert run.status == RUN_DRY_RUN and not probe.calls
        rows = run.steps[1].detail["children"]
        assert [r["id"] for r in rows] == ["a", "b", "c"]

    async def test_cancel_propagates_out_of_group(self, reg, probe) -> None:
        task = asyncio.create_task(run_workflow(build(PARALLEL), {}, registry=reg,
                                                group_runner=bounded_runner))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ═══════════════════════════════════════════════════════════════
# foreach 逐项并发（R-205）
# ═══════════════════════════════════════════════════════════════


FOREACH = """
version: 1
name: 批量
steps:
  - id: each
    type: foreach
    items: "{{ inputs.rows }}"
    max_concurrency: 4
    steps:
      - id: first
        type: tool
        tool: probe
        args: {value: "{{ loop.item.name }}", delay: "{{ loop.item.delay }}"}
      - id: second
        type: tool
        tool: probe
        args: {value: "{{ loop.index }}:{{ steps.first.output.value }}"}
inputs:
  rows: {type: array, required: true}
"""


class TestForeachConcurrency:
    async def test_each_item_sees_only_its_own_outputs(self, reg, probe) -> None:
        # 前面的项故意慢：并发时后面的项先跑完；若上下文是共用的，
        # 某一项的 second 会读到别的项刚写进去的 first（串项）
        rows = [{"name": f"n{i}", "delay": round(0.2 - i * 0.02, 3)} for i in range(8)]
        run = await run_workflow(build(FOREACH), {"rows": rows}, registry=reg,
                                 group_runner=bounded_runner)
        assert run.status == RUN_OK, run.as_dict()
        out = run.outputs["each"]
        assert [o["index"] for o in out] == list(range(8)), "结果必须按项的顺序"
        for o in out:
            assert o["outputs"]["second"] == {"value": f"{o['index']}:n{o['index']}"}
        assert probe.peak == 4

    async def test_abort_stops_unstarted_items(self, reg, probe) -> None:
        rows = [{"name": f"n{i}", "delay": 0.05} for i in range(10)]
        probe.fail["n0"] = 1
        text = FOREACH.replace("max_concurrency: 4", "max_concurrency: 2")
        run = await run_workflow(build(text), {"rows": rows}, registry=reg,
                                 group_runner=bounded_runner)
        each = run.steps[0]
        assert each.status == STATUS_ABORTED and "第 1 项的子步骤 first 失败" in each.error
        assert each.detail["executed"] < 10 and each.detail["not_started"] > 0

    async def test_large_iteration_report_is_bounded(self, reg, probe) -> None:
        rows = [{"name": f"n{i}", "delay": 0} for i in range(300)]
        probe.fail.update({"n7": 1, "n250": 1})
        text = FOREACH.split("      - id: second")[0].replace(
            'args: {value: "{{ loop.item.name }}", delay: "{{ loop.item.delay }}"}',
            'args: {value: "{{ loop.item.name }}"}\n        on_failure: continue')
        text += "inputs:\n  rows: {type: array, required: true}\n"
        # 运行期上限默认按当前版本取（社区版 20）：这里显式按专业版 1000 跑
        run = await run_workflow(build(text), {"rows": rows}, registry=reg,
                                 group_runner=bounded_runner, foreach_max=1000)
        detail = run.steps[0].detail
        assert detail["items"] == 300 and detail["executed"] == 300
        assert detail["failed_items"] == 2 and detail["failed_indexes"] == [7, 250]
        assert len(detail["iterations"]) == 50          # 逐项明细只展开前 50 项

    async def test_loop_vars_not_leaked_after_foreach(self, reg, probe) -> None:
        run = await run_workflow(build(FOREACH), {"rows": [{"name": "x", "delay": 0}]},
                                 registry=reg, group_runner=bounded_runner)
        assert run.status == RUN_OK


# ═══════════════════════════════════════════════════════════════
# branch 条件扩展（R-206）
# ═══════════════════════════════════════════════════════════════


def branch_flow(condition: str) -> str:
    return f"""
version: 1
name: 分支
inputs:
  n: {{type: string, default: "5"}}
  tags: {{type: array, default: [a, b]}}
steps:
  - id: first
    type: tool
    tool: probe
    args: {{value: "{{{{ inputs.n }}}}"}}
  - id: dec
    type: branch
    condition: '{condition}'
    then: hit
    else: miss
  - id: hit
    type: tool
    tool: probe
    args: {{value: hit}}
  - id: miss
    type: tool
    tool: probe
    args: {{value: miss}}
"""


class TestConditions:
    @pytest.mark.parametrize(("condition", "matched"), [
        ("{{ inputs.n }} > 3", True), ("{{ inputs.n }} >= 5", True),
        ("{{ inputs.n }} < 5", False), ("{{ inputs.n }} <= 4.5", False),
        ("{{ inputs.n }} > 10", False),               # 数值比较，不是文本比较
        ("{{ inputs.n }} in [1, 5, 9]", True), ("{{ inputs.n }} in [1, 9]", False),
        ("b in {{ inputs.tags }}", True), ("z in {{ inputs.tags }}", False),
        ("{{ steps.first.output.value }} exists", True),
        ("{{ steps.first.output.missing }} exists", False),
        ("{{ inputs.n }} == 5", True), ("{{ inputs.n }} contains 5", True),
    ])
    async def test_operators(self, reg, probe, condition: str, matched: bool) -> None:
        run = await run_workflow(build(branch_flow(condition)), {}, registry=reg)
        dec = run.steps[1]
        assert dec.status == STATUS_OK, dec.error
        assert dec.detail["matched"] is matched
        assert probe.count("hit" if matched else "miss") == 1
        assert probe.count("miss" if matched else "hit") == 0

    async def test_non_numeric_comparison_fails_instead_of_going_else(self, reg, probe) -> None:
        run = await run_workflow(build(branch_flow("{{ inputs.n }} > 3")), {"n": "abc"},
                                 registry=reg)
        dec = run.steps[1]
        assert dec.status == STATUS_ABORTED and "不是数字" in dec.error
        assert probe.count("hit") == probe.count("miss") == 0

    async def test_in_with_non_list_template_fails(self, reg, probe) -> None:
        run = await run_workflow(build(branch_flow("a in {{ inputs.n }}")), {}, registry=reg)
        assert run.steps[1].status == STATUS_ABORTED and "不是列表" in run.steps[1].error

    @pytest.mark.parametrize(("condition", "needle"), [
        ("{{ inputs.n }} =~ 5", "单个 '='"), ("{{ inputs.n }} matches 5", "'matches' 不是"),
        ("{{ inputs.n }} > abc", "数值比较"), ("{{ inputs.n }} in plain", "in 的右侧"),
        ("{{ inputs.n }} not in [a]", "not in"), ("{{ inputs.n }} === 5", "'==='"),
        ("prefix {{ inputs.n }} exists", "单个"),
    ])
    def test_bad_conditions_rejected_at_load(self, condition: str, needle: str) -> None:
        assert any(needle in m for m in errors_of(branch_flow(condition)))

    def test_parser_ignores_operators_inside_templates_and_quotes(self) -> None:
        cond = split_condition('"a in b" == {{ steps.x.output }}')
        assert cond is not None and cond.op == "==" and cond.left == '"a in b"'
        assert split_condition("{{ x }} contains main").op == "contains"
        assert not check_condition("{{ x }} == a>b")

    async def test_dry_run_with_new_operators(self, reg, probe) -> None:
        for cond in ("{{ inputs.n }} in [1, 5]", "{{ steps.first.output }} exists",
                     "{{ inputs.n }} >= 1"):
            run = await run_workflow(build(branch_flow(cond)), {}, registry=reg, dry_run=True)
            assert run.status == RUN_DRY_RUN and run.steps[1].status == STATUS_OK


def test_planned_features_moved_to_commercial() -> None:
    from automind.core.edition import COMMERCIAL_FEATURES, PLANNED_FEATURES

    for key in ("workflow_triggers", "workflow_parallel", "workflow_history"):
        assert key in COMMERCIAL_FEATURES and key not in PLANNED_FEATURES


def test_quota_has_history_days_and_trigger_check() -> None:
    from automind.core import quota

    assert quota.QUOTA_RULES["pro"]["workflow_history_days"] == 90
    ok, why = quota.check_workflow_triggers(0)
    assert not ok and "社区版没有内置触发器" in why


@pytest.mark.parametrize("name", ["nightly_sync.yaml", "inbound_tickets.yaml",
                                  "governed_payment.yaml"])
def test_pro_edition_examples_are_well_formed(name: str) -> None:
    """examples/06-workflow/pro-edition/：按专业版口径零错误零警告；社区版口径在加载期拦下。

    商业包不在公开仓库里，这里只用"特性已授权"的加载器验证文件本身写得对；
    真正跑一遍（专业版并发执行器 + 试运行）见 pro/tests/test_workflow_pro.py。
    """
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "examples" / "06-workflow" / "pro-edition" / name
    schema, errors, warnings = WorkflowLoader(features=set(PRO) | {"workflow_governance"},
                                              min_interval=300, foreach_max=1000).try_load(path)
    assert schema is not None and not errors and not warnings, [
        i.format() for i in errors + warnings]
    _, blocked, _ = WorkflowLoader(features=set(), min_interval=None).try_load(path)
    assert blocked and all(e.position.line > 0 for e in blocked)
    assert any("需要专业版" in e.message for e in blocked)
