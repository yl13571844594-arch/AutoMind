"""评测框架测试 —— **全部离线**：假 agent + 假工具，不联网、不需要 API Key。

评测框架本身最危险的失效形态是"永远全绿"：断言被跳过、执行器异常被吞、
"没配 Key"被写成"全部通过"。因此这里的重点不是"能跑出报告"，而是：

  · 四类路径都被真实走一遍：全部通过 / 断言失败 / 超时 / 执行器异常；
  · 失败报告必须能看出**哪一条**断言失败、期望什么、实际什么；
  · 退出码把"配置问题"（2）与"模型没达标"（1）分开；
  · 每个任务用独立工作目录（互不污染），且不往仓库里写东西。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from automind.core.types import ToolResult
from automind.eval import runner
from automind.eval.assertions import EvalOutcome, check_all, check_assertion
from automind.eval.executors import (
    AutoMindExecutor,
    RunContext,
    detect_llm_target,
    list_artifacts,
    record_tool_calls,
)
from automind.eval.pricing import estimate
from automind.eval.report import STATUS_ERROR, STATUS_FAILED, STATUS_PASSED, EvalReport
from automind.eval.runner import EXIT_CONFIG, EXIT_FAILURES, EXIT_OK, run_case, run_suite
from automind.eval.suite import (
    Assertion,
    EvalCase,
    SuiteError,
    find_suites,
    load_suite,
    parse_expect,
)

# ═══════════════════════════════════════════════════════════════
# 假工具 / 假 agent
# ═══════════════════════════════════════════════════════════════


class FakeWriteTool:
    """假的 file_write：真的落盘，但完全离线、可预测。

    相对路径以 ``root`` 为基准 —— 与真实 ``file_write``（``_RootGuard`` 以
    agent 的 ``project_root`` 解析相对路径）语义一致。少了这一步，测试就会把
    文件写到**进程当前目录**（仓库根），既污染仓库又让断言看起来"文件不存在"。
    """

    name = "file_write"
    description = "fake write"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    def __init__(self, root: Path) -> None:
        self.root = root

    async def execute(self, **kwargs):
        path = Path(kwargs.get("path", ""))
        content = kwargs.get("content", "")
        p = path if path.is_absolute() else self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(tool_name=self.name, success=True, output=f"wrote {p}")


class FakeRegistry:
    def __init__(self, root: Path) -> None:
        self._tools = {"file_write": FakeWriteTool(root)}

    def list_names(self):
        return sorted(self._tools)

    async def dispatch(self, tool_name: str, **kwargs):
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(tool_name=tool_name, success=False, error="not found")
        return await tool.execute(**kwargs)


class FakeAgent:
    """最小 agent：暴露 runner 需要的三个接口（llm / tool_registry / run）。"""

    def __init__(self, *, output="完成", write=None, tokens=(30, 20),
                 sleep=0.0, boom=None, success=True, tool_calls=True):
        self.llm = object()                      # 非 None 即视为已初始化
        self._usage_total = {"prompt_tokens": tokens[0], "completion_tokens": tokens[1],
                             "total_tokens": tokens[0] + tokens[1]}
        self.session_id = ""
        self._interaction = None
        self.config = type("C", (), {"project_root": "."})()
        self.tool_registry = FakeRegistry(Path())
        self._write = write
        self._output = output
        self._sleep = sleep
        self._boom = boom
        self._success = success
        self._tool_calls = tool_calls
        self.seen_prompt = ""

    def bind_root(self, root: Path) -> None:
        self.config.project_root = str(root)
        self.tool_registry = FakeRegistry(root)

    async def run(self, prompt: str):
        self.seen_prompt = prompt
        if self._boom:
            raise self._boom
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._tool_calls and self._write:
            await self.tool_registry.dispatch("file_write", path=self._write[0],
                                              content=self._write[1])
        return type("R", (), {"success": self._success, "output": self._output})()

    async def chat(self, prompt: str):
        return await self.run(prompt)

    async def close(self):
        return None


def agent_factory(**kwargs):
    """构造一个"知道自己的工作区在哪"的假 agent 工厂（供 AutoMindExecutor 注入）。"""

    def _make(config):
        agent = FakeAgent(**kwargs)
        agent.bind_root(Path(config.project_root))
        return agent

    return _make


class FakeExecutor:
    """可编程执行器：按任务 id 决定"怎么跑"，覆盖成功/失败/超时/异常。"""

    def __init__(self, behaviour=None, workspace_writer=None):
        self.behaviour = behaviour or {}
        self.seen: list[str] = []
        self.workspaces: list[Path] = []
        self.workspace_writer = workspace_writer

    async def run(self, ctx: RunContext) -> EvalOutcome:
        self.seen.append(ctx.case.id)
        self.workspaces.append(ctx.workspace)
        if self.workspace_writer:
            self.workspace_writer(ctx)
        b = self.behaviour.get(ctx.case.id, {})
        if b.get("raise"):
            raise b["raise"]
        if b.get("hang"):
            await asyncio.sleep(b["hang"])
        calls = b.get("tool_calls", [])
        return EvalOutcome(
            output=b.get("output", "ok"),
            success=b.get("success", True),
            tool_calls=calls,
            seconds=b.get("seconds", 0.01),
            prompt_tokens=b.get("prompt_tokens", 10),
            completion_tokens=b.get("completion_tokens", 5),
            total_tokens=b.get("prompt_tokens", 10) + b.get("completion_tokens", 5),
            timed_out=b.get("timed_out", False),
            error=b.get("error", ""),
        )


def _suite(cases: list[EvalCase], name: str = "unit") -> runner.EvalSuite:
    return runner.EvalSuite(name=name, cases=cases)


def _case(cid: str, **kw) -> EvalCase:
    return EvalCase(id=cid, prompt=kw.pop("prompt", f"做 {cid}"), **kw)


# ═══════════════════════════════════════════════════════════════
# 1. 断言单元
# ═══════════════════════════════════════════════════════════════


class TestAssertions:
    def test_contains_failure_shows_expectation_and_actual(self, tmp_path):
        a = Assertion(type="contains", params={"value": "魔法词"})
        r = check_assertion(a, EvalOutcome(output="完全没有那句话"), tmp_path)
        assert r.passed is False
        assert "魔法词" in r.expected                 # 期望什么
        assert "完全没有那句话" in r.actual            # 实际什么

    def test_not_contains_and_regex(self, tmp_path):
        out = EvalOutcome(output="结果是 PY-OK")
        assert check_assertion(Assertion("not_contains", {"value": "Traceback"}), out,
                               tmp_path).passed
        assert check_assertion(Assertion("regex", {"value": r"PY-\w+"}), out,
                               tmp_path).passed
        assert not check_assertion(Assertion("regex", {"value": r"PY-\d+"}), out,
                                   tmp_path).passed

    def test_bad_regex_is_reported_as_suite_problem(self, tmp_path):
        r = check_assertion(Assertion("regex", {"value": "([unclosed"}), EvalOutcome(),
                           tmp_path)
        assert r.passed is False and "非法" in r.actual

    def test_file_exists_min_bytes_and_artifact_hint(self, tmp_path):
        (tmp_path / "ok.txt").write_text("12345", encoding="utf-8")
        assert check_assertion(Assertion("file_exists", {"path": "ok.txt"}), EvalOutcome(),
                               tmp_path).passed
        small = check_assertion(Assertion("file_exists", {"path": "ok.txt",
                                                          "min_bytes": 99}),
                                EvalOutcome(), tmp_path)
        assert small.passed is False and "5 字节" in small.actual
        missing = check_assertion(Assertion("file_exists", {"path": "nope.txt"}),
                                  EvalOutcome(artifacts=["ok.txt"]), tmp_path)
        assert missing.passed is False
        assert missing.detail["artifacts"] == ["ok.txt"], "应回显工作区实际有哪些文件"

    def test_file_exists_rejects_path_escape(self, tmp_path):
        r = check_assertion(Assertion("file_exists", {"path": "../../etc/passwd"}),
                            EvalOutcome(), tmp_path)
        assert r.passed is False and "越界" in r.actual

    def test_tool_called_csv_with_arg_subset(self, tmp_path):
        out = EvalOutcome(tool_calls=[{"name": "file_write",
                                       "arguments": {"path": "a.py", "content": "x"}}])
        assert check_assertion(Assertion("tool_called", {"value": "file_write"}), out,
                               tmp_path).passed
        assert check_assertion(Assertion("tool_called", {
            "name": "file_write", "args": {"path": "a.py"}}), out, tmp_path).passed
        wrong = check_assertion(Assertion("tool_called", {
            "name": "file_write", "args": {"path": "b.py"}}), out, tmp_path)
        assert wrong.passed is False and "参数不匹配" in wrong.actual

    def test_tool_called_lists_actual_sequence_on_failure(self, tmp_path):
        out = EvalOutcome(tool_calls=[{"name": "file_read", "arguments": {}}])
        r = check_assertion(Assertion("tool_called", {"value": "file_write"}), out,
                            tmp_path)
        assert r.passed is False and "file_read" in r.actual

    def test_max_seconds_and_max_tokens(self, tmp_path):
        assert check_assertion(Assertion("max_seconds", {"value": 5}),
                               EvalOutcome(seconds=1.0), tmp_path).passed
        slow = check_assertion(Assertion("max_seconds", {"value": 1}),
                               EvalOutcome(seconds=3.5), tmp_path)
        assert slow.passed is False and "超出" in slow.actual
        assert check_assertion(Assertion("max_tokens", {"value": 100}),
                               EvalOutcome(total_tokens=80), tmp_path).passed
        over = check_assertion(Assertion("max_tokens", {"value": 10}),
                               EvalOutcome(total_tokens=80), tmp_path)
        assert over.passed is False and "超出" in over.actual

    def test_max_tokens_fails_when_usage_unknown(self, tmp_path):
        """成本约束无法验证 ≠ 满足约束：拿不到用量必须判失败，不能静默放行。"""
        r = check_assertion(Assertion("max_tokens", {"value": 10}),
                            EvalOutcome(total_tokens=0), tmp_path)
        assert r.passed is False and "拿不到" in r.actual

    def test_unknown_assertion_type_fails_loudly(self, tmp_path):
        r = check_assertion(Assertion("contians", {"value": "x"}), EvalOutcome(),
                            tmp_path)
        assert r.passed is False and "未知断言类型" in r.actual

    def test_check_all_survives_one_broken_assertion(self, tmp_path):
        """断言实现内部抛异常时，要变成一条"失败"，而不是让整场评测崩掉。

        场景：断言对象本身是坏的（插件/自定义断言类写错）。这类"坏在判定之前"
        的问题若直接冒泡，整场评测会以 traceback 结束 —— 那比任何断言失败都难
        排查，因为报告里什么都没有。
        """

        class BrokenAssertion(Assertion):
            def __getattribute__(self, name):
                if name == "type":
                    raise RuntimeError("boom")
                return object.__getattribute__(self, name)

        results = check_all([Assertion("contains", {"value": "x"}),
                             Assertion("regex", {"value": "([unclosed"}),
                             BrokenAssertion("contains", {"value": "x"})],
                            EvalOutcome(output="x"), tmp_path)
        assert [r.passed for r in results] == [True, False, False]
        assert "正则本身非法" in results[1].actual
        assert "断言执行异常" in results[2].actual and "boom" in results[2].actual


# ═══════════════════════════════════════════════════════════════
# 2. 套件解析
# ═══════════════════════════════════════════════════════════════


class TestSuiteParsing:
    def test_builtin_smoke_suite_is_valid(self):
        suites = find_suites()
        names = [p.name for p in suites]
        assert "smoke.yml" in names
        s = load_suite(next(p for p in suites if p.name == "smoke.yml"))
        assert 6 <= len(s.cases) <= 8, "冒烟套件应覆盖 6-8 个任务"
        assert s.total_assertions() >= 15
        kinds = {a.type for c in s.cases for a in c.assertions}
        # 题目要求覆盖的全部断言类型
        assert {"contains", "not_contains", "regex", "file_exists", "tool_called",
                "max_seconds", "max_tokens"} <= kinds
        assert any(c.expect_fail for c in s.cases), "应含一条反向断言样例"
        assert any(c.setup for c in s.cases), "应含一条带 setup 的任务（文件读写覆盖）"

    def test_unknown_assertion_key_is_rejected(self, tmp_path):
        p = tmp_path / "bad.yml"
        p.write_text("name: x\ncases:\n  - id: a\n    prompt: hi\n"
                     "    expect:\n      contians: x\n", encoding="utf-8")
        with pytest.raises(SuiteError) as e:
            load_suite(p)
        assert "未知的断言类型" in str(e.value)
        # 定位到出问题的那个任务的 `id:` 行（第 3 行）——报错要能直接对着文件改
        assert ":3" in str(e.value), str(e.value)

    def test_unknown_case_field_is_rejected(self, tmp_path):
        p = tmp_path / "bad2.yml"
        p.write_text("name: x\ncases:\n  - id: a\n    prompt: hi\n    expects: []\n",
                     encoding="utf-8")
        with pytest.raises(SuiteError) as e:
            load_suite(p)
        assert "未知字段" in str(e.value)

    def test_duplicate_id_and_missing_prompt(self, tmp_path):
        p = tmp_path / "dup.yml"
        p.write_text("name: x\ncases:\n  - id: a\n    prompt: hi\n"
                     "  - id: a\n    prompt: ho\n", encoding="utf-8")
        with pytest.raises(SuiteError, match="重复"):
            load_suite(p)
        q = tmp_path / "nop.yml"
        q.write_text("name: x\ncases:\n  - id: a\n", encoding="utf-8")
        with pytest.raises(SuiteError, match="prompt"):
            load_suite(q)

    def test_empty_cases_is_rejected(self, tmp_path):
        p = tmp_path / "empty.yml"
        p.write_text("name: x\ncases: []\n", encoding="utf-8")
        with pytest.raises(SuiteError, match="cases"):
            load_suite(p)

    def test_expect_forms_are_equivalent(self):
        where = "t"
        a = parse_expect({"contains": ["a", "b"], "max_seconds": 3}, where)
        b = parse_expect([{"contains": "a"}, {"contains": "b"}, {"max_seconds": 3}], where)
        c = parse_expect([{"type": "contains", "value": "a"},
                          {"type": "contains", "value": "b"},
                          {"type": "max_seconds", "value": 3}], where)
        assert [x.type for x in a] == [x.type for x in b] == [x.type for x in c]
        assert a[0].params["value"] == "a" and a[2].params["value"] == 3.0

    def test_expect_requires_mapping_or_list(self):
        with pytest.raises(SuiteError):
            parse_expect("contains: x", "t")

    def test_missing_file(self):
        with pytest.raises(SuiteError, match="不存在"):
            load_suite("根本没有这个文件.yml")


# ═══════════════════════════════════════════════════════════════
# 3. 执行器与工具调用记录
# ═══════════════════════════════════════════════════════════════


class TestExecutorPlumbing:
    async def test_record_tool_calls_captures_name_and_args(self):
        agent = FakeAgent(write=("a.txt", "hello"))
        sink: list[dict] = []
        record_tool_calls(agent, sink)
        await agent.run("写文件")
        assert sink and sink[0]["name"] == "file_write"
        assert sink[0]["arguments"]["path"] == "a.txt"
        assert sink[0]["success"] is True

    async def test_record_tool_calls_is_idempotent(self):
        agent = FakeAgent()
        record_tool_calls(agent, [])
        first = agent.tool_registry.dispatch
        record_tool_calls(agent, [])
        assert agent.tool_registry.dispatch is first, "重复挂钩会重复记账"

    async def test_record_tool_calls_keeps_failures_visible(self):
        """工具名不存在时（v1.7.2 契约）返回失败结果而非抛异常；记账必须跟着记。"""
        agent = FakeAgent()
        sink: list[dict] = []
        record_tool_calls(agent, sink)
        result = await agent.tool_registry.dispatch("nope")
        assert result.success is False
        assert sink[-1]["name"] == "nope" and sink[-1]["success"] is False

    def test_list_artifacts_skips_noise_dirs(self, tmp_path):
        (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
        assert list_artifacts(tmp_path, ("__pycache__",)) == ["keep.txt"]

    def test_make_agent_config_points_at_workspace(self, tmp_path):
        from automind.eval.executors import make_agent_config

        cfg = make_agent_config(tmp_path, provider="deepseek", model="deepseek-chat")
        assert str(tmp_path) in cfg.project_root
        assert cfg.llm.provider == "deepseek"
        assert cfg.execution.approval_mode == "auto", "评测不该卡在人工审批上"


# ═══════════════════════════════════════════════════════════════
# 4. 单任务：四类路径
# ═══════════════════════════════════════════════════════════════


class TestRunCase:
    async def test_all_assertions_pass(self, tmp_path):
        case = _case("c1", assertions=parse_expect(
            {"contains": "ok", "max_seconds": 5, "max_tokens": 100}, "t"))
        res = await run_case(case, FakeExecutor(), tmp_path)
        assert res.status == STATUS_PASSED and res.passed

    async def test_assertion_failure_is_failed_not_error(self, tmp_path):
        case = _case("c2", assertions=parse_expect({"contains": "不可能出现的词"}, "t"))
        res = await run_case(case, FakeExecutor(), tmp_path)
        assert res.status == STATUS_FAILED
        assert res.assertions[0]["passed"] is False
        assert "不可能出现的词" in res.assertions[0]["expected"]

    async def test_timeout_is_failed_and_marked(self, tmp_path):
        case = _case("c3", assertions=parse_expect({"max_seconds": 1}, "t"),
                     timeout_seconds=0.05)
        res = await run_case(case, _TimeoutExecutor(), tmp_path)
        assert res.status == STATUS_FAILED and res.timed_out is True
        assert "超时" in res.error

    async def test_executor_exception_is_error_status(self, tmp_path):
        case = _case("c4", assertions=parse_expect({"contains": "x"}, "t"))
        ex = FakeExecutor({"c4": {"raise": RuntimeError("执行器炸了")}})
        res = await run_case(case, ex, tmp_path)
        assert res.status == STATUS_ERROR, "执行器故障不该伪装成'模型没达标'"
        assert "执行器异常" in res.error and "执行器炸了" in res.error

    async def test_real_executor_reports_unbuilt_agent_as_error(self, tmp_path):
        """agent 建不起来（缺 LLM）→ error 状态，且原因可读。"""
        class NoLLM(FakeAgent):
            def __init__(self):
                super().__init__()
                self.llm = None
                self._llm_init_error = "未配置 API Key"

        ex = AutoMindExecutor(agent_factory=lambda _cfg: NoLLM())
        case = _case("c4b", assertions=parse_expect({"contains": "x"}, "t"))
        res = await run_case(case, ex, tmp_path)
        assert res.status == STATUS_ERROR
        assert "LLM" in res.error and "API Key" in res.error

    async def test_timeout_safeguard_when_executor_ignores_it(self, tmp_path):
        """执行器自己不管超时时，runner 至少要把结果标成超时（防挂死/防误判）。"""
        case = _case("c5", assertions=parse_expect({"max_seconds": 1}, "t"),
                     timeout_seconds=0.01)
        res = await run_case(case, FakeExecutor({"c5": {"seconds": 5.0}}), tmp_path)
        assert res.timed_out is True and res.status == STATUS_FAILED

    async def test_each_case_gets_its_own_workspace(self, tmp_path):
        suite = _suite([_case("a"), _case("b")])
        ex = FakeExecutor()
        await run_suite(suite, ex, check_credentials=False)
        assert len(ex.workspaces) == 2
        assert ex.workspaces[0] != ex.workspaces[1], "任务之间必须互不污染"

    async def test_tool_calls_are_recorded_in_report(self, tmp_path):
        case = _case("c6", assertions=parse_expect({"tool_called": "file_write"}, "t"))
        ex = FakeExecutor({"c6": {"tool_calls": [
            {"name": "file_write", "arguments": {"path": "a.py"}, "success": True}]}})
        res = await run_case(case, ex, tmp_path)
        assert res.status == STATUS_PASSED
        assert res.tool_calls[0]["name"] == "file_write"


class _TimeoutExecutor:
    """把 asyncio 超时翻译成 EvalOutcome（真实执行器的推荐做法）。"""

    async def run(self, ctx: RunContext) -> EvalOutcome:
        try:
            return await asyncio.wait_for(
                FakeExecutor({"c3": {"hang": 0.5}}).run(ctx), timeout=ctx.timeout_seconds)
        except TimeoutError:
            return EvalOutcome(seconds=ctx.timeout_seconds, timed_out=True,
                               error=f"任务超时（{ctx.timeout_seconds:g}s）")


# ═══════════════════════════════════════════════════════════════
# 5. 套件级：报告结构、反向断言、隔离
# ═══════════════════════════════════════════════════════════════


class TestRunSuite:
    async def test_report_structure_and_totals(self, tmp_path):
        suite = _suite([
            _case("pass", assertions=parse_expect({"contains": "ok"}, "t")),
            _case("fail", assertions=parse_expect({"contains": "nope"}, "t")),
        ])
        report = await run_suite(suite, FakeExecutor(), model="gpt-4o-mini",
                                 check_credentials=False)
        d = json.loads(report.to_json())          # 必须可 JSON 序列化

        assert d["suite"] == "unit" and d["total"] == 2
        assert d["passed"] == 1 and d["failed"] == 1 and d["errors"] == 0
        assert d["pass_rate"] == 0.5 and d["all_passed"] is False
        assert d["total_tokens"] == 30 and d["estimated_cost_usd"] > 0
        assert isinstance(d["total_seconds"], float)
        assert [c["id"] for c in d["cases"]] == ["pass", "fail"]

        failed = d["cases"][1]
        assert failed["status"] == STATUS_FAILED
        assert failed["failed_assertions"][0]["expected"].startswith("输出包含")
        assert "预计什么" not in failed["failed_assertions"][0]["actual"]
        # 人类可读渲染里也要能看到期望/实际
        text = report.render()
        assert "期望：" in text and "实际：" in text and "通过率" in text

    async def test_expect_fail_case_checks_the_checker(self, tmp_path):
        """反向断言：断言真的在判定（它失败了才算通过）。"""
        suite = _suite([_case("control", expect_fail=True, assertions=parse_expect(
            {"contains": "永远不可能出现_9f3"}, "t"))])
        report = await run_suite(suite, FakeExecutor(), check_credentials=False)
        assert report.cases[0].status == STATUS_PASSED
        assert report.cases[0].assertions[0]["passed"] is False
        assert report.cases[0].expect_fail is True

    async def test_report_written_to_disk(self, tmp_path):
        report = EvalReport(suite="s", model="m")
        out = report.write(tmp_path / "sub" / "report.json")
        assert json.loads(out.read_text(encoding="utf-8"))["suite"] == "s"

    async def test_data_dir_is_isolated_and_restored(self):
        before = os.environ.get("AUTOMIND_DATA_DIR")
        with runner.isolated_data_dir(Path(os.environ["TEMP"])) as d:
            assert os.environ["AUTOMIND_DATA_DIR"] == str(d)
        assert os.environ.get("AUTOMIND_DATA_DIR") == before

    async def test_include_and_limit(self):
        suite = _suite([_case("a"), _case("b"), _case("c")])
        ex = FakeExecutor()
        await run_suite(suite, ex, include=["b"], check_credentials=False)
        assert ex.seen == ["b"]
        ex2 = FakeExecutor()
        await run_suite(suite, ex2, limit=2, check_credentials=False)
        assert ex2.seen == ["a", "b"]

    async def test_include_unknown_id_is_suite_error(self):
        with pytest.raises(SuiteError, match="不存在"):
            await run_suite(_suite([_case("a")]), FakeExecutor(), include=["zzz"],
                            check_credentials=False)

    async def test_all_error_cases_add_explanatory_note(self):
        suite = _suite([_case("a"), _case("b")])
        ex = FakeExecutor({"a": {"raise": RuntimeError("x")},
                           "b": {"raise": RuntimeError("y")}})
        report = await run_suite(suite, ex, check_credentials=False)
        assert report.error_cases == 2
        assert any("执行器/环境" in n for n in report.notes)


# ═══════════════════════════════════════════════════════════════
# 6. 无 Key 行为 与 CLI 退出码
# ═══════════════════════════════════════════════════════════════


class TestCredentialsAndExitCodes:
    def test_detect_target_reports_missing_credentials(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("automind.core.config.AgentConfig",
                            _no_key_config_factory())
        t = detect_llm_target("openai", "gpt-4o-mini")
        assert t.available is False and "LLM 未配置" in t.reason
        assert "OPENAI_API_KEY" in t.reason

    def test_ollama_needs_no_key(self, monkeypatch):
        """本地模型没有 Key 也应判定为可运行（完全离线跑评测是有效路径）。"""
        monkeypatch.setattr("automind.core.config.AgentConfig",
                            _no_key_config_factory())
        t = detect_llm_target("ollama", "llama3.2")
        assert t.available is True and "ollama" in t.api_key_source

    async def test_run_suite_aborts_instead_of_failing_everything(self, monkeypatch):
        """无 Key：报告必须是 aborted，而不是"全部失败/全部通过"。"""
        from automind.eval.executors import LLMTarget

        suite = _suite([_case("a")])
        report = await run_suite(
            suite, FakeExecutor(), check_credentials=True,
            target=LLMTarget(available=False, reason="LLM 未配置：没有凭据"))
        assert report.aborted is True
        assert report.cases == []
        assert report.passed_cases == 0 and report.failed_cases == 0
        assert report.all_passed is False

    def test_cli_missing_credentials_exit_code_2(self, monkeypatch, capsys, tmp_path):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr("automind.core.config.AgentConfig",
                            _no_key_config_factory())
        p = tmp_path / "s.yml"
        p.write_text("name: x\ncases:\n  - id: a\n    prompt: hi\n", encoding="utf-8")
        code = runner.run_command(["run", str(p)])
        err = capsys.readouterr().err
        assert code == EXIT_CONFIG == 2
        assert "LLM 未配置，无法评测" in err
        assert "不会把" in err, "要明确声明不会把'跑不了'记成失败或通过"

    def test_cli_dry_run_needs_no_key(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("automind.core.config.AgentConfig",
                            _no_key_config_factory())
        code = runner.run_command(["run", "automind/eval/suites/smoke.yml", "--dry-run"])
        out = capsys.readouterr().out
        assert code == EXIT_OK == 0
        assert "smoke_write_file" in out and "断言" in out

    def test_cli_bad_suite_exit_code_2(self, capsys, tmp_path):
        p = tmp_path / "bad.yml"
        p.write_text("name: x\ncases:\n  - id: a\n    prompt: hi\n    expect:\n"
                     "      nope: 1\n", encoding="utf-8")
        code = runner.run_command(["run", str(p)])
        assert code == EXIT_CONFIG
        assert "套件格式错误" in capsys.readouterr().err

    def test_cli_list_prints_suites(self, capsys):
        assert runner.run_command(["list"]) == EXIT_OK
        assert "smoke" in capsys.readouterr().out

    def test_success_and_failure_exit_codes(self, monkeypatch, capsys, tmp_path):
        from automind.eval.executors import LLMTarget

        cfg = tmp_path / "s.yml"
        cfg.write_text("name: x\ncases:\n  - id: a\n    prompt: hi\n"
                       "    expect:\n      contains: ok\n", encoding="utf-8")
        monkeypatch.setattr(runner, "detect_llm_target",
                            lambda *_a, **_k: LLMTarget(provider="openai", model="m",
                                                       available=True))
        monkeypatch.setattr(runner, "AutoMindExecutor", lambda **_kw: FakeExecutor())
        assert runner.run_command(["run", str(cfg)]) == EXIT_OK
        capsys.readouterr()

        monkeypatch.setattr(runner, "AutoMindExecutor",
                            lambda **_kw: FakeExecutor({"a": {"output": "没中"}}))
        assert runner.run_command(["run", str(cfg)]) == EXIT_FAILURES == 1


def _no_key_config_factory():
    """返回一个"什么 Key 都没有"的 AgentConfig 工厂（离线验证无 Key 行为）。"""
    from automind.core.config import AgentConfig

    def _factory(*args, **kwargs):
        cfg = AgentConfig(*args, **kwargs)
        cfg.llm.api_key = ""
        cfg.llm.provider = "openai"
        return cfg

    return _factory


# ═══════════════════════════════════════════════════════════════
# 7. 成本估算
# ═══════════════════════════════════════════════════════════════


class TestPricing:
    def test_known_model_price(self):
        c = estimate(1_000_000, 1_000_000, "gpt-4o-mini")
        assert c.usd == pytest.approx(0.75) and c.unknown is False

    def test_prefix_match_prefers_longest(self):
        # 'gpt-4o-mini-2024-07-18' 不能被 'gpt-4o' 抢先匹配成贵 16 倍的价
        c = estimate(1_000_000, 0, "gpt-4o-mini-2024-07-18")
        assert c.matched == "gpt-4o-mini" and c.usd == pytest.approx(0.15)

    def test_unknown_model_is_flagged_and_not_underestimated(self):
        c = estimate(1_000_000, 1_000_000, "some-new-model")
        assert c.unknown is True and c.usd >= 18.0

    def test_local_models_are_free(self):
        assert estimate(10_000_000, 10_000_000, "llama3.2").usd == 0.0


# ═══════════════════════════════════════════════════════════════
# 8. 真实执行器（用假 agent 注入，不联网）
# ═══════════════════════════════════════════════════════════════


class TestAutoMindExecutor:
    async def test_runs_case_and_collects_tool_calls(self, tmp_path):
        ex = AutoMindExecutor(agent_factory=agent_factory(
            output="已写入 hello.txt", write=("hello.txt", "hello")))
        case = _case("e1", assertions=parse_expect(
            {"file_exists": "hello.txt", "tool_called": "file_write",
             "contains": "hello"}, "t"))
        ctx = RunContext(case=case, workspace=tmp_path, mode="coding",
                         timeout_seconds=5)
        outcome = await ex.run(ctx)
        assert outcome.tool_calls[0]["name"] == "file_write"
        assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"
        checks = check_all(case.assertions, outcome, tmp_path)
        assert all(c.passed for c in checks), [c.as_dict() for c in checks]

    def test_missing_llm_is_reported_clearly(self, tmp_path):
        class NoLLM(FakeAgent):
            def __init__(self):
                super().__init__()
                self.llm = None
                self._llm_init_error = "未配置 API Key"

        ex = AutoMindExecutor(agent_factory=lambda _cfg: NoLLM())
        ctx = RunContext(case=_case("e2"), workspace=tmp_path, timeout_seconds=5)
        outcome = asyncio.run(ex.run(ctx))
        assert outcome.success is False and "LLM" in outcome.error

    async def test_chat_mode_uses_chat_entrypoint(self, tmp_path):
        agent = FakeAgent(output="你好")
        agent.bind_root(tmp_path)
        ex = AutoMindExecutor(agent_factory=lambda _cfg: agent)
        ctx = RunContext(case=_case("e3", mode="chat"), workspace=tmp_path,
                         mode="chat")
        outcome = await ex.run(ctx)
        assert outcome.output == "你好" and agent.seen_prompt == "做 e3"

    async def test_config_points_at_workspace_so_writes_stay_inside(self, tmp_path):
        captured = {}

        def factory(cfg):
            captured["root"] = cfg.project_root
            agent = FakeAgent()
            agent.bind_root(Path(cfg.project_root))
            return agent

        ex = AutoMindExecutor(agent_factory=factory)
        ctx = RunContext(case=_case("e4"), workspace=tmp_path, timeout_seconds=5)
        await ex.run(ctx)
        assert str(tmp_path) in captured["root"]
