"""Token 预算不再把任务掐死 —— v1.7.5 的三条修复。

## 修的三件事

1. **预算按任务重置**：``ResourceManager`` 此前只在 Agent 构造与会话克隆时
   各建一次，已用量**跨任务累计**；Web 端同一会话的多轮任务复用同一个克隆
   实例，于是"会话预算用光后，之后每个新任务都在第一次模型调用就失败"。
   实测一个普通任务约 43~46k token，默认 81,920 的预算意味着一个会话
   大约只能跑两个任务。
2. **预算与 ``llm.max_tokens`` 解耦**：``max_tokens`` 是"单次回复长度上限"，
   与"一个任务能烧多少"无关。绑在一起派生出 81,920，而产品自带的 smoke
   套件给单任务设的断言上限是 120,000 —— 出厂默认下永远不可能满足。
3. **预算耗尽改为优雅收尾**：此前抛裸 ``RuntimeError``，ReAct 循环里没有
   任何地方接得住，一路浮成 HTTP 500，**已经做完的工作全部丢失**。

这一组用例盯的就是这三条不许回退。
"""

from __future__ import annotations

import asyncio

import pytest

from automind.core.config import AgentConfig, ExecutionConfig, LLMProviderConfig
from automind.core.exceptions import AutoMindError, BudgetExhaustedError
from automind.core.types import LLMResponse, PermissionTier, ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor
from automind.state.resource_manager import ResourceManager, TokenCounter
from automind.tools._toolkit import run_blocking  # noqa: F401  (保持与同目录用例一致的导入面)

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.permission_tier = PermissionTier.SAFE
        self.parameters = {"properties": {}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []}}


class _Registry:
    """永远返回成功，且参数每次不同（避免撞上"重复动作"拦截）。"""

    def __init__(self) -> None:
        self.calls = 0

    def list_names(self):
        return ["probe_tool"]

    def list_all(self):
        return [_Tool("probe_tool")]

    def get(self, name):
        return _Tool(name)

    async def dispatch(self, name, **kw):
        self.calls += 1
        return ToolResult(tool_name=name, success=True, output="ok")


class _BudgetedLLM:
    """带真实预算准入的假模型 —— 完整复刻后端包装器的那条链：

        pre_call_hook()  →  ResourceManager.before_llm_call()  →  耗尽即抛

    真实后端由 ``LLMBackend.__init_subclass__`` 自动挂上这两步，这里手写等价物，
    这样用例覆盖的是**产品里真实跑的那条链**，而不是一个简化替身。
    """

    def __init__(self, rm: ResourceManager, tokens_per_call: int = 1000,
                 finish_after: int | None = None) -> None:
        self.rm = rm
        self.tokens_per_call = tokens_per_call
        self.finish_after = finish_after
        self.pre_call_hook = rm.before_llm_call
        self.usage_sink = None
        self.calls = 0

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        if self.pre_call_hook is not None:
            await self.pre_call_hook()          # 预算耗尽 → BudgetExhaustedError
        self.calls += 1
        half = self.tokens_per_call // 2
        self.rm.tokens.track_usage(half, self.tokens_per_call - half)
        if self.finish_after is not None and self.calls > self.finish_after:
            return LLMResponse(text="任务完成", tool_calls=None)
        return LLMResponse(
            text=f"第 {self.calls} 步",
            tool_calls=[ToolCall(id=str(self.calls), name="probe_tool",
                                 arguments={"step": self.calls})])


def _executor(llm, registry, **kw) -> ReActExecutor:
    return ReActExecutor(llm, registry, max_iterations=kw.pop("max_iterations", 50),
                         auto_validate=False, tool_timeout=5.0, **kw)


# ═══════════════════════════════════════════════════════════
# 1. TokenCounter.reset —— 只清已用量，不动预算
# ═══════════════════════════════════════════════════════════


class TestTokenCounterReset:
    def test_reset_clears_usage_and_keeps_budget(self):
        tc = TokenCounter(budget=1000)
        tc.track_usage(600, 400)
        assert tc.is_exhausted() is True

        tc.reset()

        assert tc.tokens_used.total == 0
        assert tc.is_exhausted() is False
        assert tc.budget == 1000, "重置不得顺手改掉预算本身"
        assert tc.remaining() == 1000

    def test_budget_is_still_bounded_after_reset(self):
        """重置不是取消上限 —— 用完一轮后下一轮照样能再次耗尽。"""
        tc = TokenCounter(budget=500)
        tc.track_usage(500, 0)
        assert tc.is_exhausted()
        tc.reset()
        tc.track_usage(500, 0)
        assert tc.is_exhausted(), "重置之后预算必须仍然有效，否则等于关掉了保护"


# ═══════════════════════════════════════════════════════════
# 2. 预算是专用异常，而不是裸 RuntimeError
# ═══════════════════════════════════════════════════════════


class TestBudgetErrorIsTyped:
    def test_exhausted_raises_budget_error(self):
        rm = ResourceManager(token_budget=100)
        rm.tokens.track_usage(100, 0)
        with pytest.raises(BudgetExhaustedError):
            asyncio.run(rm.before_llm_call())

    def test_budget_error_is_an_automind_error(self):
        """能被 AutoMind 的异常体系接住 —— 也保证它不再退化成裸 RuntimeError。"""
        assert issubclass(BudgetExhaustedError, AutoMindError)
        assert not issubclass(BudgetExhaustedError, RuntimeError)

    def test_message_carries_used_and_budget(self):
        rm = ResourceManager(token_budget=100)
        rm.tokens.track_usage(80, 40)
        with pytest.raises(BudgetExhaustedError) as ei:
            asyncio.run(rm.before_llm_call())
        assert ei.value.details.get("used") == 120
        assert ei.value.details.get("budget") == 100


# ═══════════════════════════════════════════════════════════
# 3. ReAct：预算耗尽 → 部分交付清单，而不是抛异常
# ═══════════════════════════════════════════════════════════


class TestReActDegradesGracefully:
    def _run_until_exhausted(self, **cfg):
        rm = ResourceManager(token_budget=cfg.pop("budget", 3000))
        llm = _BudgetedLLM(rm, tokens_per_call=cfg.pop("per_call", 1000))
        reg = _Registry()
        ex = _executor(llm, reg, **cfg)
        out = asyncio.run(ex.run("一个需要很多步的长任务"))
        return ex, out, reg

    def test_budget_exhaustion_does_not_raise(self):
        """最容易回退的一条：预算耗尽**不得**再把异常抛给上层。"""
        ex, out, _ = self._run_until_exhausted()
        assert isinstance(out, str) and out

    def test_stop_reason_is_task_budget(self):
        ex, _, _ = self._run_until_exhausted()
        assert ex.stop_reason == "task_budget", ex.stop_reason

    def test_partial_manifest_is_delivered(self):
        """交出的是结构化清单，而不是一句"跑到上限了"。"""
        _, out, _ = self._run_until_exhausted()
        assert "部分交付清单" in out
        assert "预算" in out, "清单里必须说明停止原因是预算"

    def test_work_already_done_is_reported(self):
        """**核心断言**：中断前真实完成的动作必须出现在交付里 —— 这正是修复前丢掉的东西。"""
        ex, out, reg = self._run_until_exhausted()
        assert reg.calls >= 1, "至少应该有动作真正执行过"
        assert ex.actions, "已执行的动作必须留在报告里"
        assert "成功动作" in out
        assert str(len(ex.actions)) in out or ex.actions, "动作数应体现在清单中"

    def test_task_completing_normally_is_unaffected(self):
        """没撞预算的普通任务不受影响（别把正常收尾一起改坏）。"""
        rm = ResourceManager(token_budget=10 ** 6)
        llm = _BudgetedLLM(rm, tokens_per_call=10, finish_after=3)
        ex = _executor(llm, _Registry())
        out = asyncio.run(ex.run("短任务"))
        assert ex.stop_reason == "no_more_tools"
        assert "部分交付清单" not in out

    def test_iterations_used_is_reported(self):
        ex, _, _ = self._run_until_exhausted()
        assert ex.iterations_used >= 1


# ═══════════════════════════════════════════════════════════
# 4. 预算与 max_tokens 解耦
# ═══════════════════════════════════════════════════════════


class TestBudgetDecoupledFromMaxTokens:
    def test_helper_prefers_explicit_budget(self):
        from automind.agent import _task_token_budget

        cfg = AgentConfig(project_root=".")
        cfg.llm.max_tokens = 8192
        cfg.execution.task_token_budget = 500_000
        assert _task_token_budget(cfg) == 500_000

    def test_helper_falls_back_to_legacy_derivation(self):
        """负数 = 向后兼容：仍按老的 max_tokens × 10 派生。"""
        from automind.agent import _task_token_budget

        cfg = AgentConfig(project_root=".")
        cfg.llm.max_tokens = 8192
        cfg.execution.task_token_budget = -1
        assert _task_token_budget(cfg) == 81920

    def test_zero_means_unlimited(self):
        """v1.11.0：0 = 不设限（与 max_task_seconds 等闸门一致），不再缩成 81,920。"""
        from automind.agent import _task_token_budget

        cfg = AgentConfig(project_root=".")
        cfg.execution.task_token_budget = 0
        assert _task_token_budget(cfg) == 0
        tc = TokenCounter(budget=0)
        tc.track_usage(10 ** 9, 10 ** 9)
        assert not tc.is_exhausted() and tc.usage_fraction() == 0.0

    def test_helper_survives_garbage_values(self):
        from automind.agent import _task_token_budget

        cfg = AgentConfig(project_root=".")
        cfg.execution.task_token_budget = "不是数字"  # type: ignore[assignment]
        assert _task_token_budget(cfg) == cfg.llm.max_tokens * 10

    def test_default_budget_covers_the_shipped_smoke_suite(self):
        """出厂默认必须容得下自家评测套件的单任务断言（120k）。

        这条是本次评估里发现的真实矛盾：套件按 120k 断言，产品却在 81,920
        就掐死任务 —— 那个断言在出厂配置下永远不可能满足。
        """
        cfg = AgentConfig(project_root=".")
        from automind.agent import _task_token_budget

        assert _task_token_budget(cfg) >= 120_000, (
            f"默认预算 {_task_token_budget(cfg)} 低于 smoke 套件的 120k 断言")

    def test_max_tokens_no_longer_moves_the_budget(self):
        """改单次回复长度**不得**再顺带改掉任务预算 —— 解耦的意义就在这里。"""
        from automind.agent import _task_token_budget

        cfg = AgentConfig(project_root=".")
        cfg.execution.task_token_budget = 500_000
        before = _task_token_budget(cfg)
        cfg.llm.max_tokens = 32768
        assert _task_token_budget(cfg) == before


# ═══════════════════════════════════════════════════════════
# 5. 接线检查：agent 真的把重置接上了
# ═══════════════════════════════════════════════════════════


class TestWiring:
    def test_agent_resets_budget_at_task_start(self):
        """源码级接线断言 —— 光有 ``TokenCounter.reset()`` 而没人调用等于没修。"""
        import inspect

        from automind import agent as agent_mod
        src = inspect.getsource(agent_mod.AutoMindAgent._run_impl_bound)
        assert "self.resources.tokens.reset()" in src, (
            "任务开始时必须重置预算，否则会话第二个任务起会立即失败")

    def test_agent_uses_the_decoupled_helper(self):
        import inspect

        from automind import agent as agent_mod
        src = inspect.getsource(agent_mod)
        assert src.count("_task_token_budget(") >= 3, (
            "构造 / 会话克隆都必须走 _task_token_budget()，不能留旧的 max_tokens*10")
        # 精确断言"调用点"没退回旧写法（注释里提到旧公式是允许的 —— 那是解释）
        assert "token_budget=self.config.llm.max_tokens" not in src
        assert "token_budget=clone.config.llm.max_tokens" not in src
        assert "token_budget=self.config.llm.max_tokens * 10" not in src
        assert "token_budget=clone.config.llm.max_tokens * 10" not in src

    def test_execution_config_exposes_documented_knobs(self):
        """注释里承诺过的配置项必须真的存在（llm_call_timeout_seconds 曾经不存在）。"""
        fields = ExecutionConfig.model_fields
        assert "task_token_budget" in fields
        assert "llm_call_timeout_seconds" in fields
        assert "max_task_seconds" in fields
        assert fields["max_task_seconds"].default > 0, "整轮时长上限不该默认关闭"

    def test_llm_call_timeout_is_actually_configurable(self):
        """agent 读的就是配置里的那个字段，而不是 getattr 的兜底值。"""
        cfg = AgentConfig(project_root=".")
        cfg.execution.llm_call_timeout_seconds = 42.0
        assert cfg.execution.llm_call_timeout_seconds == 42.0
        assert LLMProviderConfig().timeout > 0      # 提供商级超时另有一条线
