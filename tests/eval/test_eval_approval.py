"""评测执行器的「无人值守审批」与「硬超时」测试 —— 全部离线，不需要 API Key。

背景（2026-10-24 测评报告）：
  · 标准工具赛道 ``smoke_run_command``：任务超时 237.2s（180s 上限）+
    436,698 token（120k 上限）；
  · 同赛道 ``smoke_chinese_content``：180,170 token（120k 上限）。
根因不在模型：``automind/eval/executors.py`` 把评测 agent 的 ``approval_mode``
设成 ``auto``。``terminal`` 的 preflight 风险分是 60，``PermissionEngine`` 在 auto
下返回 ASK_USER；评测进程没有审批通道，``react_executor._gate`` 对"问不到人"
fail-closed 拒绝，``agent._on_approval_needed`` 又回落到读 stdin 的 human_loop
（EOF → 拒绝）。模型拿不到结果只会反复重试 → 耗时与 token 一起爆炸。

这里把修复后的三条契约钉死（避免将来"改回去还全绿"）：

  1. 评测默认 ``approve_all``，且该模式下 terminal 命令的决策确实是 allow
     —— 同时用 ``auto`` 做对照，证明根因可复现；
  2. CLI 的 ``--approval-mode`` 能覆盖默认值、透传到执行器，并写进报告 ``notes``
     （不写清模式，同套件两份报告的耗时/token 根本没法对比）；
  3. 执行器按任务上限加硬超时：执行器内部卡死时 ``asyncio.wait_for`` 能收住，
     超时后 ``agent.close()`` 仍会被调用（资源不漏）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from automind.core.types import PermissionTier
from automind.eval import runner
from automind.eval.assertions import EvalOutcome
from automind.eval.executors import (
    AutoMindExecutor,
    LLMTarget,
    RunContext,
    align_task_budget,
    make_agent_config,
)
from automind.eval.suite import EvalCase, EvalSuite
from automind.tools.permissions import PermissionEngine

#: 与 smoke.yml 的 smoke_run_command 同形：terminal 命令，preflight 风险=60。
TERMINAL_COMMAND = 'python -c "print(\'PY-OK\')"'


def _engine(cfg: Any, workspace: Any) -> PermissionEngine:
    """按 agent 的真实构造方式建权限引擎（策略/根目录/审批模式都取自 cfg）。"""
    return PermissionEngine(policy=cfg.permissions, project_root=str(workspace),
                            approval_mode=cfg.execution.approval_mode)


# ═══════════════════════════════════════════════════════════════
# 1. 默认审批模式：approve_all（根因修复本身）
# ═══════════════════════════════════════════════════════════════


class TestDefaultApprovalMode:
    def test_agent_config_defaults_to_approve_all(self, tmp_path):
        cfg = make_agent_config(tmp_path)
        assert cfg.execution.approval_mode == "approve_all"

    def test_agent_config_can_be_overridden(self, tmp_path):
        assert make_agent_config(
            tmp_path, approval_mode="auto").execution.approval_mode == "auto"
        assert make_agent_config(
            tmp_path, approval_mode="ask").execution.approval_mode == "ask"

    def test_unknown_mode_is_rejected_instead_of_silently_downgraded(self, tmp_path):
        """写错模式时不能静默回落到 auto。

        ``PermissionEngine`` 对非法模式会默默降级成 auto；如果评测也照抄这个行为，
        一个拼写错误就会把"全批准"悄悄变回"无人可问 → 拒绝"，重新触发本次故障。
        """
        with pytest.raises(ValueError, match="approval_mode"):
            make_agent_config(tmp_path, approval_mode="approve-all")

    def test_executor_carries_default_and_override(self):
        assert AutoMindExecutor().approval_mode == "approve_all"
        assert AutoMindExecutor(approval_mode="ask").approval_mode == "ask"

    def test_approve_all_allows_sensitive_terminal_command(self, tmp_path):
        engine = _engine(make_agent_config(tmp_path), tmp_path)
        decision, reason = engine.check("terminal", PermissionTier.SENSITIVE,
                                        {"command": TERMINAL_COMMAND})
        assert decision.value == "allow", reason
        assert "全批准" in reason

    def test_auto_mode_asks_and_that_is_the_root_cause(self, tmp_path):
        """对照实验：auto 模式下同一条命令是 ask_user。

        评测没有审批通道 → 该调用会被拒 → 模型重试。把它固定成测试，是为了让
        "把默认值改回 auto"这种回归立刻变红，而不是等到下一次评测超时才发现。
        """
        engine = _engine(make_agent_config(tmp_path, approval_mode="auto"), tmp_path)
        decision, reason = engine.check("terminal", PermissionTier.SENSITIVE,
                                        {"command": TERMINAL_COMMAND})
        assert decision.value == "ask_user", reason


# ═══════════════════════════════════════════════════════════════
# 2. 墙钟预算对齐：agent 先收尾，外层硬超时只是保险
# ═══════════════════════════════════════════════════════════════


class TestTaskBudgetAlignment:
    def test_aligns_to_task_limit_when_agent_budget_is_larger(self, tmp_path):
        cfg = make_agent_config(tmp_path)
        cfg.execution.max_task_seconds = 1800.0
        align_task_budget(cfg, 180)
        assert cfg.execution.max_task_seconds == pytest.approx(180.0)

    def test_keeps_stricter_existing_budget(self, tmp_path):
        """调用方设了更小的预算时不能被放宽（那是更严格的约束）。"""
        cfg = make_agent_config(tmp_path)
        cfg.execution.max_task_seconds = 60.0
        align_task_budget(cfg, 180)
        assert cfg.execution.max_task_seconds == pytest.approx(60.0)

    def test_unlimited_budget_adopts_task_limit(self, tmp_path):
        cfg = make_agent_config(tmp_path)
        cfg.execution.max_task_seconds = 0.0        # 0 = 不限
        align_task_budget(cfg, 180)
        assert cfg.execution.max_task_seconds == pytest.approx(180.0)

    def test_no_task_limit_leaves_budget_untouched(self, tmp_path):
        cfg = make_agent_config(tmp_path)
        cfg.execution.max_task_seconds = 900.0
        align_task_budget(cfg, 0)
        assert cfg.execution.max_task_seconds == pytest.approx(900.0)


# ═══════════════════════════════════════════════════════════════
# 3. CLI：--approval-mode 的解析与透传
# ═══════════════════════════════════════════════════════════════


class TestCliApprovalMode:
    def test_parser_defaults_to_approve_all(self):
        args = runner._build_parser().parse_args(["run", "smoke.yml"])
        assert args.approval_mode == "approve_all"

    def test_parser_accepts_override(self):
        args = runner._build_parser().parse_args(
            ["run", "smoke.yml", "--approval-mode", "auto"])
        assert args.approval_mode == "auto"

    def test_parser_rejects_unknown_mode(self):
        with pytest.raises(SystemExit):
            runner._build_parser().parse_args(
                ["run", "smoke.yml", "--approval-mode", "approve-all"])

    def test_cli_passes_mode_to_executor_and_into_report(self, monkeypatch, capsys,
                                                         tmp_path):
        captured: dict[str, Any] = {}

        class _RecordingExecutor:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                self.approval_mode = kwargs.get("approval_mode", "")

            async def run(self, ctx: RunContext) -> EvalOutcome:
                return EvalOutcome(output="ok", success=True, seconds=0.01,
                                   prompt_tokens=10, completion_tokens=5,
                                   total_tokens=15)

        monkeypatch.setattr(runner, "AutoMindExecutor", _RecordingExecutor)
        monkeypatch.setattr(
            runner, "detect_llm_target",
            lambda *_a, **_k: LLMTarget(provider="openai", model="m", available=True))
        suite_file = tmp_path / "suite.yml"
        suite_file.write_text(
            "name: x\ncases:\n  - id: a\n    prompt: hi\n"
            "    expect:\n      contains: ok\n", encoding="utf-8")

        code = runner.run_command(["run", str(suite_file), "--approval-mode", "ask"])

        assert code == runner.EXIT_OK
        assert captured["approval_mode"] == "ask", "CLI 必须把模式透传给执行器"
        out = capsys.readouterr().out
        assert "无人值守审批模式=ask" in out, "报告里必须能看出用的是哪种审批模式"


# ═══════════════════════════════════════════════════════════════
# 4. 报告 notes：审批模式必须写进报告
# ═══════════════════════════════════════════════════════════════


class _OkExecutor:
    """最小假执行器：瞬时成功，完全离线。"""

    async def run(self, ctx: RunContext) -> EvalOutcome:
        return EvalOutcome(output="ok", success=True, seconds=0.01,
                           prompt_tokens=10, completion_tokens=5, total_tokens=15)


def _suite(case_id: str = "a") -> EvalSuite:
    return EvalSuite(name="unit", cases=[EvalCase(id=case_id, prompt="hi")])


class TestReportNotes:
    async def test_notes_record_approve_all(self):
        report = await runner.run_suite(_suite(), _OkExecutor(),
                                        check_credentials=False,
                                        approval_mode="approve_all")
        notes = "\n".join(report.notes)
        assert "无人值守审批模式=approve_all" in notes
        assert "敏感工具自动放行" in notes
        assert "审批通道缺失会按拒绝处理" in notes
        # 人类可读渲染里也要看得到（CI 日志里最常被翻的就是这段）
        assert "无人值守审批模式=approve_all" in report.render()

    async def test_notes_infer_mode_from_executor(self):
        """不传参数时从执行器自身取（AutoMindExecutor 就有这个属性）。"""

        class _ExecutorWithMode(_OkExecutor):
            approval_mode = "approve_all"

        report = await runner.run_suite(_suite(), _ExecutorWithMode(),
                                        check_credentials=False)
        assert any("approve_all" in n for n in report.notes)

    async def test_notes_warn_about_non_unattended_modes(self):
        report = await runner.run_suite(_suite(), _OkExecutor(),
                                        check_credentials=False,
                                        approval_mode="auto")
        notes = "\n".join(report.notes)
        assert "无人值守审批模式=auto" in notes
        assert "按拒绝处理" in notes, "非 approve_all 必须提示会被 fail-closed 拒绝"


# ═══════════════════════════════════════════════════════════════
# 5. 执行器硬超时：卡死能收住，且超时后仍 close
# ═══════════════════════════════════════════════════════════════


class _HangingAgent:
    """最小假 agent：``run`` 一直挂住（直到被取消），记录取消与 close。"""

    def __init__(self) -> None:
        self.llm = object()                 # 非 None 即视为 LLM 已初始化
        self.tool_registry = None           # record_tool_calls 会跳过（无需假注册表）
        self.session_id = ""
        self.config = SimpleNamespace(project_root=".")
        self.closed = False
        self.cancelled = False
        self.captured_config: Any = None

    async def run(self, prompt: str) -> Any:
        try:
            await asyncio.sleep(30)         # 远大于测试里的任务上限
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("挂住的 run 不应该自然结束")     # pragma: no cover

    async def close(self) -> None:
        self.closed = True


class _QuickAgent:
    """最小假 agent：立即返回（用于验证"没超时就不要误判超时"）。"""

    def __init__(self) -> None:
        self.llm = object()
        self.tool_registry = None
        self.session_id = ""
        self.config = SimpleNamespace(project_root=".")
        self._usage_total = {"prompt_tokens": 7, "completion_tokens": 3,
                             "total_tokens": 10}
        self.closed = False

    async def run(self, prompt: str) -> Any:
        return SimpleNamespace(success=True, output=f"done:{prompt}")

    async def close(self) -> None:
        self.closed = True


class TestHardTimeout:
    async def test_hanging_agent_is_cut_off_and_still_closed(self, tmp_path):
        agents: list[_HangingAgent] = []

        def factory(cfg: Any) -> _HangingAgent:
            agent = _HangingAgent()
            agent.captured_config = cfg
            agents.append(agent)
            return agent

        executor = AutoMindExecutor(agent_factory=factory)
        ctx = RunContext(case=EvalCase(id="hang", prompt="x"), workspace=tmp_path,
                         timeout_seconds=0.05)
        started = time.monotonic()
        outcome = await executor.run(ctx)
        elapsed = time.monotonic() - started

        assert outcome.timed_out is True
        assert "超时" in outcome.error
        assert outcome.success is False
        assert elapsed < 5, f"硬超时没有生效，实际等了 {elapsed:.1f}s"
        assert agents[0].cancelled is True, "超时必须真正取消任务，而不是等它自己结束"
        assert agents[0].closed is True, "超时后仍必须 close agent（释放资源）"
        # 配置侧的两项修复也要真的落到 agent 上
        assert agents[0].captured_config.execution.approval_mode == "approve_all"
        assert agents[0].captured_config.execution.max_task_seconds == pytest.approx(0.05)

    async def test_fast_agent_is_not_marked_timed_out(self, tmp_path):
        agent = _QuickAgent()
        executor = AutoMindExecutor(agent_factory=lambda _cfg: agent)
        ctx = RunContext(case=EvalCase(id="quick", prompt="hi"), workspace=tmp_path,
                         timeout_seconds=5)
        outcome = await executor.run(ctx)

        assert outcome.timed_out is False
        assert outcome.error == ""
        assert outcome.output == "done:hi"
        assert outcome.total_tokens == 10
        assert agent.closed is True

    async def test_zero_timeout_disables_hard_timeout(self, tmp_path):
        """``timeout_seconds=0`` = 不限：不能因为 0 而被当成"立即超时"。"""
        agent = _QuickAgent()
        executor = AutoMindExecutor(agent_factory=lambda _cfg: agent)
        ctx = RunContext(case=EvalCase(id="nolimit", prompt="hi"), workspace=tmp_path)
        outcome = await executor.run(ctx)

        assert outcome.timed_out is False
        assert outcome.output == "done:hi"
