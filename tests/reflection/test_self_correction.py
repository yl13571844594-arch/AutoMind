"""自我纠错循环 —— 核心卖点，此前零回归保护。

`SelfCorrectionLoop` 的价值主张是"工具调用失败后自己分析、改参数、重试"。
它一旦悄悄退化（比如修正后的参数没被真正用上、或失败后死循环），
表面上仍然"跑完了"，只是成功率变差 —— 没有测试根本发现不了。

这里盯住五件事：
  1. 修正后的参数**真的**被用于重试（不是拿原参数又跑一遍）；
  2. 成功即停，不多跑无谓的轮次（每轮都是一次 LLM 调用，是真金白银）；
  3. 达到上限就停，不会无限重试；
  4. LLM 判定"修不了"（actionable=false）时立刻放弃；
  5. 重试过程中抛异常不会把整个循环带崩。
"""

from __future__ import annotations

import json

import pytest

from automind.core.types import ToolResult
from automind.reflection.self_correction import SelfCorrectionLoop


class FakeLLM:
    """按脚本依次返回文本；记录收到的每个 prompt 供断言。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def generate(self, messages, tools=None, stop=None):
        self.prompts.append(messages[-1]["content"])
        text = self.replies.pop(0) if self.replies else "{}"
        return type("R", (), {"text": text})()


def fix_reply(params: dict, description="改了参数", actionable=True) -> str:
    return json.dumps({"actionable": actionable, "description": description,
                       "params": params})


def ok(output="done") -> ToolResult:
    return ToolResult(tool_name="t", success=True, output=output)


def fail(error="boom") -> ToolResult:
    return ToolResult(tool_name="t", success=False, error=error)


class TestCorrectedParamsAreActuallyUsed:
    async def test_retry_uses_the_corrected_params(self):
        """最核心的一条：LLM 给出的新参数必须真的传给工具。"""
        llm = FakeLLM(["路径写错了", fix_reply({"path": "/tmp/right.txt"})])
        seen: list[dict] = []

        async def executor(**params):
            seen.append(dict(params))
            return ok()

        r = await SelfCorrectionLoop(llm=llm).correct(
            tool_name="file_write",
            original_params={"path": "/tmp/wrong.txt"},
            error_message="No such file or directory",
            tool_executor=executor,
        )
        assert r.fixed is True
        assert seen == [{"path": "/tmp/right.txt"}], "重试用的不是修正后的参数"
        assert r.iterations == 1
        assert r.records[0].success is True

    async def test_second_round_builds_on_the_first_correction(self):
        """第二轮要基于第一轮改过的参数继续改，而不是退回原始参数。"""
        llm = FakeLLM([
            "分析1", fix_reply({"path": "/a"}),
            "分析2", fix_reply({"path": "/b"}),
        ])
        seen: list[dict] = []

        async def executor(**params):
            seen.append(dict(params))
            return ok() if params.get("path") == "/b" else fail("still bad")

        r = await SelfCorrectionLoop(llm=llm, max_iterations=3).correct(
            "t", {"path": "/orig"}, "err", executor)
        assert r.fixed is True
        assert seen == [{"path": "/a"}, {"path": "/b"}]
        # 第二轮的 prompt 里应带着第一轮改过的参数，而不是最初那个
        assert "/a" in llm.prompts[2], "第二轮没有基于上一轮的修正结果"


class TestStoppingConditions:
    async def test_stops_immediately_on_success(self):
        """成功即停 —— 每多一轮就是一次真金白银的 LLM 调用。"""
        llm = FakeLLM(["分析", fix_reply({"x": 1})])
        calls = 0

        async def executor(**params):
            nonlocal calls
            calls += 1
            return ok()

        r = await SelfCorrectionLoop(llm=llm, max_iterations=5).correct(
            "t", {"x": 0}, "err", executor)
        assert calls == 1 and r.iterations == 1
        assert len(r.records) == 1, "成功后不该再记录额外轮次"

    async def test_gives_up_at_max_iterations(self):
        """永远失败时必须在上限处停下，不能无限重试。"""
        llm = FakeLLM(["a", fix_reply({"x": 1}),
                       "b", fix_reply({"x": 2}),
                       "c", fix_reply({"x": 3}),
                       "d", fix_reply({"x": 4})])
        calls = 0

        async def executor(**params):
            nonlocal calls
            calls += 1
            return fail("always")

        r = await SelfCorrectionLoop(llm=llm, max_iterations=3).correct(
            "t", {"x": 0}, "err", executor)
        assert r.fixed is False
        assert calls == 3, f"应恰好重试 3 次，实际 {calls}"
        assert len(r.records) == 3

    async def test_unactionable_fix_aborts_early(self):
        """LLM 判定修不了就别再耗 —— 继续重试只是烧 token。"""
        llm = FakeLLM(["磁盘满了，改参数没用",
                       fix_reply({}, description="无法自动修复", actionable=False)])
        calls = 0

        async def executor(**params):
            nonlocal calls
            calls += 1
            return ok()

        r = await SelfCorrectionLoop(llm=llm, max_iterations=3).correct(
            "t", {"x": 0}, "No space left on device", executor)
        assert r.fixed is False
        assert calls == 0, "判定不可修复后不该再调用工具"
        assert r.final_analysis == "磁盘满了，改参数没用"

    async def test_no_llm_means_no_correction(self):
        """没有 LLM 时不该假装能修 —— 直接判定不可行动。"""
        calls = 0

        async def executor(**params):
            nonlocal calls
            calls += 1
            return ok()

        r = await SelfCorrectionLoop(llm=None).correct("t", {}, "err", executor)
        assert r.fixed is False and calls == 0


async def _never_called(**_params):
    """占位执行器 —— 以下用例都应在调用工具之前就停下来。"""
    raise AssertionError("不该调用工具")


class TestResilience:
    async def test_executor_exception_does_not_break_the_loop(self):
        """重试时工具抛异常，应记下来继续下一轮，而不是把整个纠错崩掉。"""
        llm = FakeLLM(["a", fix_reply({"x": 1}),
                       "b", fix_reply({"x": 2})])
        calls = 0

        async def executor(**params):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("连接被重置")
            return ok()

        r = await SelfCorrectionLoop(llm=llm, max_iterations=2).correct(
            "t", {"x": 0}, "err", executor)
        assert r.fixed is True, "第一轮抛异常不该终止纠错"
        assert calls == 2
        # 第二轮的分析 prompt 里应带着上一轮的真实异常信息
        assert "连接被重置" in llm.prompts[2]

    async def test_malformed_llm_json_is_treated_as_unfixable(self):
        """LLM 返回的不是合法 JSON 时按"修不了"处理，不能崩。"""
        llm = FakeLLM(["分析", "这不是 JSON"])
        r = await SelfCorrectionLoop(llm=llm).correct(
            "t", {}, "err", _never_called)
        assert r.fixed is False

    async def test_history_accumulates(self):
        """每次纠错都要留档，供后续统计"自我修正率"。"""
        loop = SelfCorrectionLoop(llm=None)
        await loop.correct("t", {}, "e1", _never_called)
        await loop.correct("t", {}, "e2", _never_called)
        assert len(loop.history) == 2
        assert [h.original_error for h in loop.history] == ["e1", "e2"]


class TestRecordFidelity:
    async def test_each_record_carries_its_own_error(self):
        """B-07 修复的回归：每轮记录的是**当轮**的错误，不是最初那个。"""
        llm = FakeLLM(["a", fix_reply({"x": 1}),
                       "b", fix_reply({"x": 2}),
                       "c", fix_reply({"x": 3})])
        errs = iter(["第二次错误", "第三次错误", "第四次错误"])

        async def executor(**params):
            return fail(next(errs))

        r = await SelfCorrectionLoop(llm=llm, max_iterations=3).correct(
            "t", {"x": 0}, "最初的错误", executor)
        assert [rec.error for rec in r.records] == [
            "最初的错误", "第二次错误", "第三次错误"], "每轮应记录当轮真实错误"

    @pytest.mark.parametrize("bad", [0, -1, -99])
    async def test_nonpositive_max_iterations_falls_back_to_default(self, bad):
        """max_iterations=0 会让纠错一轮都不跑，等于功能被静默关掉。"""
        loop = SelfCorrectionLoop(llm=None, max_iterations=bad)
        assert loop.max_iterations == SelfCorrectionLoop.MAX_ITERATIONS
