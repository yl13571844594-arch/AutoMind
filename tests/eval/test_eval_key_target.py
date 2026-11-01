"""v1.11.24 评测取"当前已接入的模型与凭据"—— 执行器侧离线用例。

## 修的是什么

Web 工作台已经接入 deepseek-flash 并能正常对话，但 ``/api/eval/run`` 的
dry-run 仍报 ``provider='openai'`` 没有可用凭据。根因在评测执行器：它只认
"显式 provider/model + 环境变量"，其余全部落到 ``AgentConfig`` 的**代码默认值**
（openai/gpt-4o），而工作台当前那套配置（deepseek + 已保存 Key + api_base）
根本没有入口传进来。

修复给 ``detect_llm_target`` / ``make_agent_config`` / ``AutoMindExecutor``
增加可选的 ``api_key``（及 api_base / temperature / max_tokens）显式覆盖。

## 测试边界（全部离线）

* 不需要 Key 也能判定"可用/不可用"，不发任何网络请求；
* 显式 Key 只影响可用性与评测 Agent 配置，**不得出现在任何返回值/报告里**；
* 不传新参数时行为与旧版完全一致（CLI 默认路径不受影响）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from automind.eval.assertions import EvalOutcome
from automind.eval.executors import (
    AutoMindExecutor,
    RunContext,
    detect_llm_target,
    make_agent_config,
)
from automind.eval.runner import run_suite
from automind.eval.suite import EvalCase, EvalSuite

#: 测试用假 Key —— 任何返回值里出现它都算泄露
SECRET = "sk-eval-offline-secret-0123456789"


def _clear_provider_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉全部 provider 环境变量，保证"没有 Key"的用例不受本机环境影响。"""
    from automind.core.provider_resolver import ENV_KEY_MAP

    for name in sorted(set(ENV_KEY_MAP.values())):
        monkeypatch.delenv(name, raising=False)


class _FakeAgent:
    """最小 agent 替身：只满足 AutoMindExecutor.run 的接口，不调任何模型。"""

    def __init__(self, config) -> None:
        self.config = config
        self.llm = object()             # 非 None 即可（执行器只检查是否可比 None）
        self._interaction = None

    async def run(self, prompt: str) -> str:
        return "任务完成"


# ═══════════════════════════════════════════════════════════
# 1. detect_llm_target：显式 Key
# ═══════════════════════════════════════════════════════════


class TestDetectLLMTarget:
    def test_explicit_key_marks_available_without_echoing_it(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_provider_envs(monkeypatch)
        target = detect_llm_target("deepseek", "deepseek-chat",
                                   api_key=SECRET,
                                   api_key_source="web:agent")
        assert target.available is True
        assert target.provider == "deepseek"
        assert target.model == "deepseek-chat"
        assert target.api_key_source == "web:agent"
        # 返回值里只有来源标签，绝不能有 Key 明文
        assert SECRET not in json.dumps(target.as_dict(), ensure_ascii=False)
        assert set(target.as_dict()) == {
            "provider", "model", "api_key_source", "available", "reason"}

    def test_default_detection_is_unchanged_for_cli(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传新参数时行为与旧版一致：默认 openai/gpt-4o、没 Key 就不可用。"""
        _clear_provider_envs(monkeypatch)
        target = detect_llm_target()
        assert target.available is False
        assert target.provider == "openai" and target.model == "gpt-4o"
        assert "OPENAI_API_KEY" in target.reason
        assert target.api_key_source == ""

    def test_env_key_still_works_without_explicit_override(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI 路径：显式 --provider 时仍从环境变量取 Key（行为不变）。"""
        _clear_provider_envs(monkeypatch)
        monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
        target = detect_llm_target("deepseek", "deepseek-chat")
        assert target.available is True
        assert target.api_key_source == "env:DEEPSEEK_API_KEY"
        assert SECRET not in json.dumps(target.as_dict(), ensure_ascii=False)

    def test_ollama_needs_no_key_even_with_override_options(self) -> None:
        target = detect_llm_target("ollama", "llama3.2")
        assert target.available is True
        assert "ollama" in target.api_key_source


# ═══════════════════════════════════════════════════════════
# 2. make_agent_config：可选覆盖 + 缺省行为不变
# ═══════════════════════════════════════════════════════════


class TestMakeAgentConfig:
    def test_defaults_match_old_cli_behaviour(self, tmp_path) -> None:
        cfg = make_agent_config(tmp_path)
        assert cfg.llm.provider == "openai"
        assert cfg.llm.model == "gpt-4o"
        assert cfg.llm.api_key == ""
        assert cfg.llm.api_base == ""
        assert cfg.llm.temperature == pytest.approx(0.7)
        assert cfg.llm.max_tokens == 8192
        assert cfg.execution.approval_mode == "approve_all"
        # v1.11.24 的**有意变更**：评测默认走 react（与工作台一致）。
        # 此前落到 AgentConfig 的代码默认 plan_and_execute，其返回值是步骤摘要
        # 而非最终输出，`contains` 断言会系统性误判。CLI 没有该开关（runner 未改），
        # 因此 CLI 评测同样默认 react；需要 plan_and_execute 的调用方显式传参。
        assert cfg.execution.mode == "react"
        assert str(tmp_path) in cfg.project_root

    def test_execution_mode_is_configurable(self, tmp_path) -> None:
        cfg = make_agent_config(tmp_path, execution_mode="plan_and_execute")
        assert cfg.execution.mode == "plan_and_execute"

    def test_unknown_execution_mode_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="execution_mode"):
            make_agent_config(tmp_path, execution_mode="plan-and-execute")

    def test_optional_overrides_are_applied(self, tmp_path) -> None:
        cfg = make_agent_config(
            tmp_path, "deepseek", "deepseek-chat",
            api_key=SECRET, api_base="https://api.deepseek.com/v1",
            temperature=0.2, max_tokens=1024)
        llm = cfg.llm
        assert llm.provider == "deepseek" and llm.model == "deepseek-chat"
        assert llm.api_key == SECRET
        assert llm.api_base == "https://api.deepseek.com/v1"
        assert llm.temperature == pytest.approx(0.2)
        assert llm.max_tokens == 1024

    def test_invalid_approval_mode_still_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="approval_mode"):
            make_agent_config(tmp_path, approval_mode="approve-all")

    def test_eval_config_really_builds_a_react_agent(
            self, tmp_path, monkeypatch) -> None:
        """配置真的生效：真实 Agent 构造后 ``_mode`` 必须是 ReAct。

        这是本次输出误判的另一半修复 —— 只在 config 上写 mode 还不够，Agent
        构造时必须读到它（``AutoMindAgent.__init__`` 用 ``config.execution.mode``
        初始化 ``self._mode``）。
        """
        from automind.agent import AutoMindAgent
        from automind.core.types import ExecutionMode

        monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "data"))
        cfg = make_agent_config(tmp_path)
        agent = AutoMindAgent(cfg)
        assert agent._mode is ExecutionMode.REACT


# ═══════════════════════════════════════════════════════════
# 3. AutoMindExecutor：把凭据带进评测 Agent（离线假 agent）
# ═══════════════════════════════════════════════════════════


class TestExecutorWiring:
    def test_executor_passes_credentials_into_agent_config(self, tmp_path) -> None:
        captured: dict = {}

        def factory(config):
            captured["config"] = config
            return _FakeAgent(config)

        executor = AutoMindExecutor(
            provider="deepseek", model="deepseek-chat",
            agent_factory=factory, api_key=SECRET,
            api_base="https://api.deepseek.com/v1",
            temperature=0.3, max_tokens=2048)
        ctx = RunContext(case=EvalCase(id="t1", prompt="做点事"),
                         workspace=tmp_path, mode="coding")
        outcome = asyncio.run(executor.run(ctx))

        assert outcome.success is True
        assert outcome.output == "任务完成"
        llm = captured["config"].llm
        assert llm.provider == "deepseek" and llm.model == "deepseek-chat"
        assert llm.api_key == SECRET, "评测 Agent 没拿到工作台当前凭据"
        assert llm.api_base == "https://api.deepseek.com/v1"
        assert llm.temperature == pytest.approx(0.3)
        assert llm.max_tokens == 2048
        assert captured["config"].execution.mode == "react", (
            "评测 Agent 默认必须是 ReAct（否则返回步骤摘要，输出断言失真）")
        assert str(tmp_path) in captured["config"].project_root

    def test_executor_without_overrides_keeps_defaults(self, tmp_path) -> None:
        """CLI 风格构造（不传新参数）→ 评测 Agent 仍是默认配置。

        唯一有意变化是执行模式：v1.11.24 起默认 ``react``（对齐工作台）。
        """
        captured: dict = {}

        def factory(config):
            captured["config"] = config
            return _FakeAgent(config)

        executor = AutoMindExecutor(agent_factory=factory)
        ctx = RunContext(case=EvalCase(id="t2", prompt="做点事"),
                         workspace=tmp_path, mode="coding")
        asyncio.run(executor.run(ctx))
        llm = captured["config"].llm
        assert llm.provider == "openai" and llm.model == "gpt-4o"
        assert llm.api_key == ""
        assert executor.api_key == "" and executor.api_base == ""
        assert executor.execution_mode == "react"
        assert captured["config"].execution.mode == "react"

    def test_executor_can_override_execution_mode(self, tmp_path) -> None:
        captured: dict = {}

        def factory(config):
            captured["config"] = config
            return _FakeAgent(config)

        executor = AutoMindExecutor(agent_factory=factory,
                                    execution_mode="plan_and_execute")
        ctx = RunContext(case=EvalCase(id="t3", prompt="做点事"),
                         workspace=tmp_path, mode="coding")
        asyncio.run(executor.run(ctx))
        assert executor.execution_mode == "plan_and_execute"
        assert captured["config"].execution.mode == "plan_and_execute"


# ═══════════════════════════════════════════════════════════
# 4. 报告里绝不能出现 Key（离线执行器，快速跑完）
# ═══════════════════════════════════════════════════════════


class _OkExecutor:
    approval_mode = "approve_all"

    async def run(self, ctx) -> EvalOutcome:
        return EvalOutcome(output="done", success=True, seconds=0.0)


class TestReportDoesNotLeakKey:
    def test_report_never_contains_the_key(self, monkeypatch) -> None:
        _clear_provider_envs(monkeypatch)
        target = detect_llm_target("deepseek", "deepseek-chat",
                                   api_key=SECRET, api_key_source="web:agent")
        suite = EvalSuite(name="s", cases=[EvalCase(id="c1", prompt="p")])
        report = asyncio.run(run_suite(
            suite, _OkExecutor(), model=target.model, provider=target.provider,
            target=target))
        assert report.model == "deepseek-chat"
        assert report.provider == "deepseek"
        assert SECRET not in report.to_json()
        assert SECRET not in json.dumps(report.as_dict(), ensure_ascii=False)
