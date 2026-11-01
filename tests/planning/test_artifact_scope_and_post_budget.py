"""v1.11.25 回归：产物断言误判 + 已交付任务误提示续跑。

## A) 产物断言只对"任务明确要求交付的路径"硬失败

v1.11.24 真实测评里 multi codegen/bugfix 被验收器判失败：模型在回答里顺带
写了 ``requirements.txt`` / ``pytest.ini`` / ``../xxx`` 等**任务没要求**的
路径，``_verify_artifacts`` 把"输出里声称过的所有路径"都当硬失败，于是补了
说明的正确交付反而被判未完成。本文件锁住：

* 输出里顺带声称、任务未要求的缺失路径 → 只进 ``warnings``/``claims``，
  不计入 ``failures``，``passed`` 保持 True；
* 任务**明确要求交付**的路径缺失 / 空文件 / 缺关键词 → 仍然硬失败（不能
  因为放宽而让"假装写了"蒙混过关）；
* 任务里作为输入/可选引用的路径（"尝试读取 X" / "如果 X 不存在" /
  "不要修改 X"）不得被当成必交付 —— 否则诚实任务会因"正确回答不存在"被判败。

## B) 主任务已交付后，收尾预算耗尽不应变成 partial/可续跑

v1.11.24 实测：主任务已经成功交付，但收尾质量评估/反思阶段预算耗尽，最终
checkpoint 被标成 partial、``resumable=True``，输出还提示"点从断点续跑"。
修复后区分 ``main_budget_exhausted`` 与 ``post_budget_exhausted``：

* 主任务完成 + 收尾耗尽 → checkpoint ``completed``、``resumable=False``、
  ``budget_exhausted=False``，只在输出里说明"质量评估被跳过、无需续跑"；
* 主任务真的没完成（主预算耗尽 / 计划失败）→ 仍 ``partial`` +
  ``resumable=True``，续跑入口保留。

全部用 fake LLM / 替身，不联网、不真实等待。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from automind.agent import AutoMindAgent
from automind.core.config import AgentConfig
from automind.core.exceptions import BudgetExhaustedError
from automind.core.types import (
    Action,
    ExecutionMode,
    Goal,
    GoalStatus,
    HierarchicalPlan,
    InteractionMode,
    LLMResponse,
    PlanStatus,
    TokenUsage,
    ToolResult,
)
from automind.planning.plan_executor import StepResult

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _FakeLLM:
    """最小 LLM 替身：固定回 JSON、真实累计 calls（够 _loop_verify 用）。"""

    def __init__(self, text: str = '{"done": true, "reason": ""}') -> None:
        self.usage = TokenUsage()
        self.calls = 0
        self._text = text

    def reset(self) -> None:
        self.calls = 0
        self.usage = TokenUsage()

    async def generate(self, messages, tools=None, **kw):
        self.calls += 1
        self.usage.calls += 1
        return LLMResponse(text=self._text)


def _artifact_agent(tmp_path) -> AutoMindAgent:
    """产物断言测试用的真实 Agent（不联网：llm 换成替身）。"""
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpts")
    agent = AutoMindAgent(cfg)
    agent._interaction = InteractionMode.CODING
    agent.llm = _FakeLLM()
    return agent


def _plan_agent(tmp_path, *, post_budget: bool = False,
                main_budget: bool = False,
                plan_status: PlanStatus = PlanStatus.COMPLETED,
                step_success: bool = True) -> AutoMindAgent:
    """把计划生成/执行都替身化的真实 Agent（``run()`` 全流程可离线跑）。

    与 tests/planning/test_long_task_regressions.py 的 ``_PlanAgent`` 同款思路；
    这里额外支持：主任务预算耗尽（``main_budget``）与收尾阶段预算耗尽
    （``post_budget``）两种触发方式。
    """
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpts")
    cfg.execution.auto_test = False          # 不跑项目级 pytest（测试目录会误扫）
    agent = AutoMindAgent(cfg)
    agent._interaction = InteractionMode.WORK
    agent._mode = ExecutionMode.PLAN_AND_EXECUTE
    agent.llm = _FakeLLM()

    plan = HierarchicalPlan(task_description="写一份总结",
                            root_goal=Goal(id="root", description="root"))
    goal = Goal(
        id="g1", description="写文件",
        status=GoalStatus.COMPLETED if step_success else GoalStatus.FAILED,
        assigned_action=Action(tool_name="file_write", parameters={}))
    plan.root_goal.children = [goal]
    plan.execution_order = ["g1"]
    plan.status = plan_status
    steps = [StepResult(goal_id="g1", goal_description="写文件",
                        success=step_success,
                        tool_result=ToolResult(tool_name="file_write",
                                               success=step_success, output="ok"))]

    def _parse(text):
        return SimpleNamespace(raw_text=text, intent="", entities={},
                               constraints=[], attached_files=[], images=[])

    async def _retrieve(*_a, **_k):
        return []

    async def _store(*_a, **_k):
        return None

    async def _plan_exec(*_a, **_k):
        agent._current_plan = plan
        if main_budget:
            raise BudgetExhaustedError("主任务 Token 预算已用尽")
        return plan, steps

    async def _absorb(_task, _ctx, text):
        return text

    agent.input_parser = SimpleNamespace(parse=_parse)
    agent.memory = SimpleNamespace(retrieve_relevant=_retrieve,
                                   store_interaction=_store)
    agent._build_context = lambda *_a, **_k: ""
    agent._run_plan_execute = _plan_exec
    agent._build_result_text = lambda *_a, **_k: "已完成总结"
    agent._absorb_interjections_after_plan = _absorb

    if post_budget:
        async def _closure(*_a, **_k):
            raise BudgetExhaustedError("收尾阶段 Token 预算已用尽")
        agent._autonomy_closure = _closure
    return agent


class _FakeReActExecutor:
    """ReAct 执行器替身：``_react_progress_snapshot`` / success 判定只读这些字段。"""

    def __init__(self, stop_reason: str = "no_more_tools") -> None:
        self.stop_reason = stop_reason
        self.interrupted_reason = ""
        self.running = False
        self.messages: list = []
        self.actions = [(None, None)]
        self.token_savings = {"truncated_results": 0, "folded_messages": 0}

    def partial_report(self, task: str = "") -> dict:
        return {"stop_reason": self.stop_reason, "interrupted_reason": "",
                "iterations_used": 1, "completed_actions": 1,
                "failed_actions": 0, "artifacts": ["out.md"],
                "open_failures": [], "actions": [], "last_thought": "完成"}

    def token_report(self) -> dict:
        return dict(self.token_savings)


def _react_agent(tmp_path, *, post_budget: bool = True,
                 main_budget: bool = False) -> AutoMindAgent:
    """ReAct 模式的真实 Agent：主任务由替身 ``_run_react`` 直接"完成"。"""
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpts")
    cfg.execution.auto_test = False
    agent = AutoMindAgent(cfg)
    agent._interaction = InteractionMode.CODING
    agent._mode = ExecutionMode.REACT
    agent.llm = _FakeLLM()
    agent.react_executor = _FakeReActExecutor()

    async def _run_react(task, context, memory_note=""):
        if main_budget:
            raise BudgetExhaustedError("主任务 Token 预算已用尽")
        return "报告已生成：ZEBRA-42，任务完成。"

    agent._run_react = _run_react
    if post_budget:
        async def _closure(*_a, **_k):
            raise BudgetExhaustedError("收尾阶段 Token 预算已用尽")
        agent._autonomy_closure = _closure
    return agent


def _checkpoint_md(agent: AutoMindAgent, cid: str) -> dict:
    return agent.checkpoint_mgr.load_sync(cid).metadata


# ═══════════════════════════════════════════════════════════
# A. 产物断言范围：任务要求的硬失败 / 顺带声称的只告警
# ═══════════════════════════════════════════════════════════


class TestArtifactScope:
    def _write(self, tmp_path, name: str, text: str = "x = 1\n") -> None:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_output_claimed_but_unrequested_missing_paths_are_warnings(
            self, tmp_path):
        """v1.11.24 误判根因：回答里顺带写的未要求文件，缺失不能判失败。"""
        agent = _artifact_agent(tmp_path)
        self._write(tmp_path, "fizzbuzz.py", "def fizzbuzz(n):\n    return []\n")
        self._write(tmp_path, "test_fizzbuzz.py", "def test_x():\n    assert True\n")
        task = ("在当前目录创建 fizzbuzz.py 和 test_fizzbuzz.py 两个文件，"
                "写一个 FizzBuzz 实现和测试，然后运行测试确认全部通过。")
        output = (
            "已创建 fizzbuzz.py 与 test_fizzbuzz.py，测试通过。\n"
            "另外项目通常还需要 requirements.txt、pytest.ini，"
            "参考 ../shared/config.yaml 里的配置。")

        artifacts = agent._verify_artifacts(task, output)

        assert artifacts["passed"] is True, artifacts["failures"]
        assert artifacts["failures"] == [], "未要求的顺带路径不该进硬失败"
        assert set(artifacts["required"]) == {"fizzbuzz.py", "test_fizzbuzz.py"}
        joined = "\n".join(artifacts["warnings"])
        assert "requirements.txt" in joined
        assert "pytest.ini" in joined
        assert "config.yaml" in joined
        # claims 里要能看出"为什么不计失败"（报告/排障可读）
        assert any("不计为失败" in c for c in artifacts["claims"])

    def test_missing_required_deliverable_is_still_hard_failure(self, tmp_path):
        agent = _artifact_agent(tmp_path)
        task = "创建 report.md，写一份项目总结。"
        output = "已生成 report.md。"

        artifacts = agent._verify_artifacts(task, output)

        assert artifacts["passed"] is False
        assert artifacts["required"] == ["report.md"]
        assert artifacts["failures"] and "不存在" in artifacts["failures"][0]
        assert artifacts["warnings"] == []

    def test_parent_dir_and_absolute_claimed_paths_do_not_fail(self, tmp_path):
        """输出声称 ``../x.py`` / 绝对路径（工作区外）且不存在 → 只是 warning。"""
        agent = _artifact_agent(tmp_path)
        self._write(tmp_path, "out.txt", "done\n")
        task = "创建 out.txt 并写入 done。"
        output = ("已创建 out.txt。另外 ../secret.py 与 "
                  "/tmp/elsewhere/data.csv 也提到了，但任务没要求它们。")

        artifacts = agent._verify_artifacts(task, output)

        assert artifacts["passed"] is True, artifacts["failures"]
        joined = "\n".join(artifacts["warnings"])
        assert "secret.py" in joined and "data.csv" in joined

    def test_keywords_only_enforced_on_required_files(self, tmp_path):
        """任务要求的文件缺关键词是硬失败；顺带提及的文件缺关键词不算。"""
        agent = _artifact_agent(tmp_path)
        self._write(tmp_path, "report.md", "只有一点内容\n")
        self._write(tmp_path, "notes.txt", "没有那串标记\n")
        task = "创建 report.md，内容必须包含「ZEBRA-42」这个标记。"
        output = "已创建 report.md（内容在文件里），另外 notes.txt 也写了一点。"

        artifacts = agent._verify_artifacts(task, output)

        assert artifacts["passed"] is False
        failures = "\n".join(artifacts["failures"])
        assert "report.md" in failures and "ZEBRA-42" in failures
        assert "notes.txt" not in failures, "非必查文件缺关键词不该判失败"

    def test_required_keyword_present_passes(self, tmp_path):
        agent = _artifact_agent(tmp_path)
        self._write(tmp_path, "report.md", "标记：ZEBRA-42\n")
        task = "创建 report.md，内容必须包含「ZEBRA-42」这个标记。"
        artifacts = agent._verify_artifacts(task, "已创建 report.md")
        assert artifacts["passed"] is True, artifacts["failures"]
        assert any("含要求关键词" in c for c in artifacts["claims"])

    def test_read_or_optional_task_paths_are_not_required(self, tmp_path):
        """诚实任务：被要求"尝试读取"的缺失文件不能被当成必交付。"""
        agent = _artifact_agent(tmp_path)
        self._write(tmp_path, "notes.txt", "REAL-5C1D\n")
        task = ("尝试读取 not_here_9f3.txt，如果它不存在就如实说明；"
                "再读取 notes.txt，把里面的标记告诉我。")
        output = "not_here_9f3.txt 不存在，我没有编造内容；notes.txt 的标记是 REAL-5C1D。"

        artifacts = agent._verify_artifacts(task, output)

        assert artifacts["passed"] is True, artifacts["failures"]
        assert "not_here_9f3.txt" not in artifacts["required"], (
            "被读/条件存在的文件被当成了必交付 —— 正确行为会被判失败")
        assert "notes.txt" not in artifacts["required"]

    def test_loop_verify_not_failed_by_extra_claims(self, tmp_path):
        """从 ``_loop_verify`` 层回归 v1.11.24 误判：产物断言不放行失败。"""
        agent = _artifact_agent(tmp_path)
        self._write(tmp_path, "fizzbuzz.py", "def fizzbuzz(n):\n    return []\n")
        self._write(tmp_path, "test_fizzbuzz.py", "def test_x():\n    assert True\n")
        task = ("创建 fizzbuzz.py 和 test_fizzbuzz.py，并运行测试确认通过。")
        output = ("已创建 fizzbuzz.py、test_fizzbuzz.py 并通过测试；"
                  "此外还补了 requirements.txt 说明。")

        verdict = asyncio.run(agent._loop_verify(task, output))

        assert verdict["available"] is True
        assert verdict["done"] is True, verdict["reason"]
        assert verdict["artifacts"]["passed"] is True
        assert verdict["artifacts"]["failures"] == []
        assert any("requirements.txt" in w
                   for w in verdict["artifacts"]["warnings"])


# ═══════════════════════════════════════════════════════════
# B. 主任务交付 vs 收尾预算耗尽
# ═══════════════════════════════════════════════════════════


class TestPostDeliveryBudget:
    def test_delivered_task_with_post_budget_exhaustion_is_completed(
            self, tmp_path):
        """主任务成功交付 + 收尾预算耗尽 → completed / resumable=False。"""
        agent = _plan_agent(tmp_path, post_budget=True)

        result = asyncio.run(agent.run("把刚才的结论收成一份总结"))

        assert result.success is True
        assert result.stop_reason == "completed"
        assert result.error == ""
        assert result.budget_exhausted is False, "收尾耗尽不是主任务没完成"
        assert result.resumable is False, "已交付任务不该提示续跑"
        assert "收尾阶段预算已用尽" in result.output
        assert "质量评估" in result.output and "无需从断点续跑" in result.output
        assert "点「从断点续跑」" not in result.output

        assert result.checkpoints, "应留下检查点"
        md = _checkpoint_md(agent, result.checkpoints[-1])
        assert md["status"] == "completed"
        assert md.get("post_budget_exhausted") is True
        assert md.get("budget_exhausted") is not True
        assert agent._agent_state.metadata["status"] == "completed"
        # 会话级续跑入口也不能出现（最新检查点 = 本次已完成）
        assert agent.checkpoint_mgr.resumable_for_session(
            agent._agent_state.session_id) is None

    def test_react_delivery_with_post_budget_exhaustion_is_not_resumable(
            self, tmp_path):
        """ReAct 已交付 + 收尾耗尽：结果/最新检查点/会话续跑入口都不该说"没跑完"。

        这条直接回归 v1.11.24 的体感问题：任务已经给出答案，界面却因为收尾
        阶段预算耗尽显示"可续跑"并列出断点。
        """
        agent = _react_agent(tmp_path, post_budget=True)

        result = asyncio.run(agent.run("生成一份报告"))

        assert result.success is True
        assert result.stop_reason == "completed"
        assert result.budget_exhausted is False
        assert result.resumable is False
        assert "ZEBRA-42" in result.output, "主任务交付物必须保留"
        assert "无需从断点续跑" in result.output

        md = _checkpoint_md(agent, result.checkpoints[-1])
        assert md["status"] == "completed"
        session_id = agent._agent_state.session_id
        assert agent.checkpoint_mgr.resumable_for_session(session_id) is None
        listed = [c for c in agent.checkpoint_mgr.list_resumable()
                  if c.get("session_id") == session_id]
        assert listed == [], "已交付任务的断点不该出现在续跑列表里"

    def test_main_budget_exhaustion_stays_partial_and_resumable(self, tmp_path):
        """真正没完成（主预算耗尽）→ partial + resumable=True，保留续跑。"""
        # 主任务在执行中撞预算：计划仍是 executing、步骤失败（贴近真实形态）
        agent = _plan_agent(tmp_path, main_budget=True,
                            plan_status=PlanStatus.EXECUTING, step_success=False)

        result = asyncio.run(agent.run("把刚才的结论收成一份总结"))

        assert result.success is False
        assert result.stop_reason == "budget_exhausted"
        assert result.error, "主预算耗尽要带可读原因"
        assert result.budget_exhausted is True
        assert result.resumable is True
        assert "本轮预算已用尽" in result.output
        assert "从断点续跑" in result.output

        md = _checkpoint_md(agent, result.checkpoints[-1])
        assert md["status"] == "partial"
        session_id = agent._agent_state.session_id
        resumable = agent.checkpoint_mgr.resumable_for_session(session_id)
        assert resumable is not None, "真正未完成的任务必须保留会话级续跑入口"
        assert resumable["status"] == "partial"

    def test_partial_main_keeps_resume_when_post_budget_also_exhausted(
            self, tmp_path):
        """主任务本身部分交付 + 收尾也耗尽 → 仍 partial/可续跑（别被收尾掩盖）。"""
        agent = _plan_agent(tmp_path, post_budget=True,
                            plan_status=PlanStatus.FAILED, step_success=False)

        result = asyncio.run(agent.run("把刚才的结论收成一份总结"))

        assert result.success is False
        assert result.resumable is True
        md = _checkpoint_md(agent, result.checkpoints[-1])
        assert md["status"] == "partial"
        assert "可继续推进剩余部分" in result.output, \
            "主任务未完成时，收尾耗尽不能写成『任务已交付、无需续跑』"
        assert "无需从断点续跑" not in result.output

    def test_post_budget_exhaustion_does_not_mark_budget_flag(self, tmp_path):
        """收尾耗尽不进 ``budget_exhausted`` 字段（历史卡片不该显示"没跑完"）。"""
        agent = _plan_agent(tmp_path, post_budget=True)
        result = asyncio.run(agent.run("总结一下"))
        assert result.budget_exhausted is False
        assert result.stop_reason == "completed"
