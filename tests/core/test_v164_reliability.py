"""v1.6.4 闭环可靠性测试 —— 失败不得静默化 / 产物级验收 / 部分交付清单。

覆盖 10 项修正里最容易被"看起来正常"掩盖的几条：

  1. 审查设施故障不得被记成「审查通过 ✓」（伪造通过）；
  2. 验收设施故障不得被记成「验收未过 ✗」（白烧修复轮 token）；
  3. 验收必须包含**产物级确定性断言**（文件真的存在 / 非空 / 含要求的关键词）；
  4. ReAct 迭代上限耗尽时必须给出**结构化部分交付清单**，而不是一句提示；
  5. 并行批内单点异常不得连坐（其它目标成果必须保留）。
"""

from __future__ import annotations

import asyncio

from automind.core.config import AgentConfig
from automind.core.types import Action, Goal, HierarchicalPlan, ToolResult

# ── 测试替身 ────────────────────────────────────────────


class _Resp:
    text = ""
    tool_calls: list = []


def _agent(tmp_path, **execution):
    from automind.agent import AutoMindAgent
    from automind.core.types import InteractionMode

    cfg = AgentConfig(project_root=str(tmp_path))
    for k, v in execution.items():
        setattr(cfg.execution, k, v)
    a = AutoMindAgent(cfg)
    a._interaction = InteractionMode.WORK
    return a


# ── 1. 审查异常不再伪装成「通过」 ────────────────────────


class TestReviewNeverFakesApproval:
    def test_llm_exception_is_unavailable_not_approved(self, tmp_path):
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                raise RuntimeError("provider 502")
        a.llm = LLM()
        rv = asyncio.run(a._review_result("任务", "结果"))
        assert rv["available"] is False
        assert rv["approved"] is False
        assert "502" in rv["error"]

    def test_non_json_answer_is_unavailable(self, tmp_path):
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = "我觉得还行吧"
                return r
        a.llm = LLM()
        rv = asyncio.run(a._review_result("任务", "结果"))
        assert rv["available"] is False

    def test_closure_summary_says_not_executed(self, tmp_path):
        """工作模式下审查挂掉 → 摘要必须说"未执行"，绝不能出现"审查通过"。"""
        a = _agent(tmp_path, auto_test=False, auto_verify=False)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                raise RuntimeError("boom")
        a.llm = LLM()
        out = asyncio.run(a._autonomy_closure("任务", "结果", ""))
        assert "审查通过" not in out
        assert "审查未执行" in out
        assert "请勿据此认为已通过" in out
        assert a._verify_state["review"]["available"] is False
        assert "多 Agent 审查" in a._verify_state["degraded"]

    def test_real_rejection_still_reported(self, tmp_path):
        """真正的"审查不通过"仍然是"有意见"，不能被混成"未执行"。"""
        a = _agent(tmp_path, auto_test=False, auto_verify=False)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = '{"approved": false, "issues": "缺少错误处理"}'
                return r
        a.llm = LLM()
        out = asyncio.run(a._autonomy_closure("任务", "结果", ""))
        assert "审查有意见" in out
        assert "未执行" not in out


# ── 2. 验收设施故障不再烧修复轮、不再谎报"未过" ─────────


class TestVerifyFailureIsNotSilent:
    def test_llm_exception_skips_fix_rounds(self, tmp_path):
        a = _agent(tmp_path, auto_test=False, auto_review=False,
                   auto_verify_max_rounds=2)
        state = {"react_calls": 0}

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                raise RuntimeError("timeout")
        a.llm = LLM()

        async def fake_react(task, context):
            state["react_calls"] += 1
            return "结果"
        a._run_react = fake_react

        out = asyncio.run(a._autonomy_closure("任务", "初版", ""))
        assert state["react_calls"] == 0          # 没有白烧修复轮
        assert "验收未过" not in out               # 也没有谎报"未通过"
        assert "验收未执行" in out
        assert a._verify_state["verify"]["available"] is False

    def test_verdict_shape(self, tmp_path):
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                raise ValueError("bad")
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("任务", "结果"))
        assert v == {**v, "done": False, "available": False,
                     "source": "unavailable"}

    def test_model_must_return_done_key(self, tmp_path):
        """模型回 JSON 但没有 done 字段 → 视为验收不可用，而不是"未通过"。"""
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = '{"reason": "说不清"}'
                return r
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("任务", "结果"))
        assert v["available"] is False


# ── 3. 产物级确定性断言 ─────────────────────────────────


class TestArtifactAssertions:
    def test_missing_required_file_is_hard_failure(self, tmp_path):
        a = _agent(tmp_path)

        class LLM:
            calls = 0

            async def generate(self, messages, tools=None, **kw):
                # 模型永远说"完成了" —— 产物断言必须压过这种自评
                self.calls += 1
                r = _Resp()
                r.text = '{"done": true, "reason": ""}'
                return r
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("生成 report.md 报告", "已生成 report.md"))
        assert v["available"] is True
        assert v["done"] is False
        assert v["source"] == "artifact"
        assert "不存在" in v["reason"]

    def test_existing_nonempty_file_passes_and_beats_llm(self, tmp_path):
        (tmp_path / "report.md").write_text("# 报告\n内容在此\n", encoding="utf-8")
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = '{"done": true, "reason": ""}'
                return r
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("生成 report.md", "已完成"))
        assert v["done"] is True and v["source"] == "llm"
        assert v["artifacts"]["checked"] >= 1
        assert v["artifacts"]["passed"] is True

    def test_empty_file_is_not_delivery(self, tmp_path):
        (tmp_path / "out.csv").write_text("", encoding="utf-8")
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = '{"done": true, "reason": ""}'
                return r
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("导出 out.csv", "已导出"))
        assert v["done"] is False and v["source"] == "artifact"
        assert "为空" in v["reason"]

    def test_required_keyword_checked(self, tmp_path):
        (tmp_path / "summary.md").write_text("随便写了点东西", encoding="utf-8")
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = '{"done": true, "reason": ""}'
                return r
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("生成 summary.md，包含「风险清单」", "好了"))
        assert v["done"] is False
        assert "风险清单" in v["reason"]

    def test_non_file_task_not_penalized(self, tmp_path):
        """纯分析任务顺带提到一个不存在的路径名，不该因此判失败。"""
        a = _agent(tmp_path)

        class LLM:
            async def generate(self, messages, tools=None, **kw):
                r = _Resp()
                r.text = '{"done": true, "reason": ""}'
                return r
        a.llm = LLM()
        v = asyncio.run(a._loop_verify("分析这段日志的错误原因", "疑似 config.yaml 写错"))
        assert v["done"] is True
        assert v["artifacts"]["passed"] is True

    def test_claimed_paths_extraction(self):
        from automind.agent import AutoMindAgent

        paths = AutoMindAgent._claimed_paths(
            "生成 automind/agent.py 与 report.docx，见 https://x.com/a.md")
        assert "automind/agent.py" in paths
        assert "report.docx" in paths
        assert not any("http" in p for p in paths)
        # 无扩展名的裸词不当产物
        assert "tests" not in AutoMindAgent._claimed_paths("跑一下 tests")


# ── 4. ReAct 部分交付清单 ───────────────────────────────


class _EchoTool:
    name = "echo_tool"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    permission_tier = None
    risk_score = 1
    source = None

    def __init__(self) -> None:
        from automind.core.types import PermissionTier
        self.permission_tier = PermissionTier.SAFE

    def to_openai_schema(self):
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}

    async def execute(self, **kwargs):
        return ToolResult(tool_name=self.name, success=True,
                          output={"path": str(kwargs.get("text", "")), "ok": True})


class TestReactPartialDelivery:
    def _executor(self, max_iter=2):
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import ToolRegistry

        reg = ToolRegistry()
        reg.register(_EchoTool())

        class LLM:
            n = 0

            async def generate(self, messages, tools=None, **kw):
                self.n += 1
                from automind.core.types import ToolCall
                r = _Resp()
                r.text = f"第 {self.n} 步思考"
                r.tool_calls = [ToolCall(id=str(self.n), name="echo_tool",
                                         arguments={"text": f"out{self.n}.txt"})]
                return r
        return ReActExecutor(llm=LLM(), tool_registry=reg, max_iterations=max_iter,
                             tool_budget=0)

    def test_manifest_lists_artifacts_and_progress(self):
        ex = self._executor(max_iter=3)
        out = asyncio.run(ex.run("做事"))
        assert "部分交付清单" in out
        assert "out1.txt" in out and "out3.txt" in out
        assert ex.stop_reason == "max_iterations"
        assert ex.iterations_used == 3
        assert "最大迭代步数" in out            # 旧的文本判据仍能识别
        rep = ex.partial_report("做事")
        assert rep["completed_actions"] == 3
        assert rep["stop_reason"] == "max_iterations"
        assert len(rep["actions"]) == 3

    def test_manifest_reports_open_failures(self):
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import ToolRegistry

        reg = ToolRegistry()
        reg.register(_EchoTool())

        class FailingLLM:
            async def generate(self, messages, tools=None, **kw):
                from automind.core.types import ToolCall
                r = _Resp()
                r.text = "试一下"
                r.tool_calls = [ToolCall(id="1", name="no_such_tool",
                                         arguments={})]
                return r
        ex = ReActExecutor(llm=FailingLLM(), tool_registry=reg, max_iterations=1,
                           tool_budget=0)
        out = asyncio.run(ex.run("做事"))
        assert "仍未解决的失败" in out
        assert "no_such_tool" in out

    def test_cancellation_leaves_progress(self):
        """被取消时也要留下进度：不能只让用户看到"任务没了"。"""
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import ToolRegistry

        reg = ToolRegistry()
        reg.register(_EchoTool())

        class CancelLLM:
            n = 0

            async def generate(self, messages, tools=None, **kw):
                from automind.core.types import ToolCall
                self.n += 1
                if self.n >= 2:
                    raise asyncio.CancelledError()
                r = _Resp()
                r.text = "第一步"
                r.tool_calls = [ToolCall(id="1", name="echo_tool",
                                         arguments={"text": "a.txt"})]
                return r
        ex = ReActExecutor(llm=CancelLLM(), tool_registry=reg, max_iterations=5,
                           tool_budget=0)
        import pytest
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(ex.run("做事"))
        assert ex.stop_reason == "cancelled"
        rep = ex.partial_report("做事")
        assert rep["artifacts"] == ["a.txt"]
        assert "a.txt" in ex.render_manifest(rep)

    def test_normal_finish_has_no_manifest(self):
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import ToolRegistry

        class DoneLLM:
            n = 0

            async def generate(self, messages, tools=None, **kw):
                self.n += 1
                from automind.core.types import ToolCall
                r = _Resp()
                if self.n == 1:
                    r.tool_calls = [ToolCall(id="1", name="echo_tool",
                                             arguments={"text": "x.txt"})]
                    r.text = "先做一步"
                else:
                    r.tool_calls = []
                    r.text = "全部完成"
                return r
        reg = ToolRegistry()
        reg.register(_EchoTool())
        ex = ReActExecutor(llm=DoneLLM(), tool_registry=reg, max_iterations=5,
                           tool_budget=0)
        out = asyncio.run(ex.run("做事"))
        assert out == "全部完成"
        assert ex.stop_reason == "no_more_tools"
        assert "部分交付清单" not in out


# ── 5. 并行批内异常不连坐 ───────────────────────────────


class TestParallelBatchIsolation:
    def test_one_goal_crash_keeps_others(self):
        from automind.planning.plan_executor import PlanExecutor
        from automind.tools.base import AbstractTool, ToolRegistry

        class Boom(AbstractTool):
            name = "boom"
            description = "raise"
            parameters = {"type": "object", "properties": {}}

            def __init__(self):
                from automind.core.types import PermissionTier
                self.permission_tier = PermissionTier.SENSITIVE

            async def execute(self, **kwargs):
                raise RuntimeError("工具内部炸了")

        class Fine(AbstractTool):
            name = "fine"
            description = "ok"
            parameters = {"type": "object", "properties": {}}

            def __init__(self):
                from automind.core.types import PermissionTier
                self.permission_tier = PermissionTier.SENSITIVE
                self.calls = 0

            async def execute(self, **kwargs):
                self.calls += 1
                return ToolResult(tool_name=self.name, success=True, output={"ok": 1})

        reg = ToolRegistry()
        fine = Fine()
        reg.register(Boom())
        reg.register(fine)
        ex = PlanExecutor(llm=None, tool_registry=reg, parallel=True,
                          use_cache=False, max_retries=1)
        goals = [
            Goal(id="g1", description="g1",
                 assigned_action=Action(tool_name="fine", parameters={})),
            Goal(id="g2", description="g2",
                 assigned_action=Action(tool_name="boom", parameters={})),
            Goal(id="g3", description="g3",
                 assigned_action=Action(tool_name="fine", parameters={})),
        ]
        plan = HierarchicalPlan(task_description="t",
                                root_goal=Goal(id="root", description="root",
                                               children=goals),
                                execution_order=[g.id for g in goals])
        report = asyncio.run(ex.execute(plan))
        by_id = {s.goal_id: s for s in report.steps}
        # 关键：两个正常目标都跑完了，成果没有因为邻居炸掉而丢失
        assert by_id["g1"].success is True
        assert by_id["g3"].success is True
        assert fine.calls == 2
        # 炸掉的那个被如实记为失败，而且原因必须是**真实原因** ——
        # "Failed after N attempts" 这种笼统话等于另一种静默失败
        assert by_id["g2"].success is False
        assert "工具内部炸了" in by_id["g2"].error
