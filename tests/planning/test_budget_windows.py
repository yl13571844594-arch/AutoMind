"""长任务不再"到 50 万 token 就中断" —— v1.11.0 预算窗口自动续开。

## 修的是什么

``task_token_budget``（默认 500,000）一满，``_pre_call`` 就拒绝后续模型调用，
任务交部分交付清单收尾；前端还不认 ``budget_*`` 事件，于是用户看到的是
"跑到 50 万就安静地停住"。现在窗口用尽时：

1. 先压缩上下文（新窗口不该原样重发上一个窗口的全部观察）；
2. 已用量归零、累计用量照记，推 ``budget_window`` 事件，本次调用照常放行；
3. 墙钟预算（max_task_seconds）同步顺延一份，推给正在跑的执行器；
4. 续开次数用完（``budget_auto_continue``）才按原路径收尾 —— 成本护栏仍在。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from automind.core.exceptions import BudgetExhaustedError


class _FakeLLM:
    def __init__(self) -> None:
        self.usage_sink = None
        self.pre_call_hook = None
        self.heartbeat_hook = None
        self.call_timeout = 0.0


def _agent(budget: int = 1000, auto_continue: int = 2, task_seconds: float = 0.0):
    from automind.agent import AutoMindAgent
    from automind.state.resource_manager import ResourceManager

    agent = object.__new__(AutoMindAgent)
    agent.llm = _FakeLLM()
    agent.resources = ResourceManager(token_budget=budget, rate_limit=1e6)
    agent.config = SimpleNamespace(execution=SimpleNamespace(
        llm_call_timeout_seconds=5.0, budget_auto_continue=auto_continue,
        max_task_seconds=task_seconds))
    agent._usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "calls": 0}
    agent._budget_warned = False
    agent._next_compact_at = agent._BUDGET_WARN_AT
    agent._task_deadline = 1000.0 if task_seconds else None
    agent.react_executor = SimpleNamespace(running=False, messages=[], deadline_shift=0.0)
    agent.plan_executor = SimpleNamespace(deadline_shift=0.0)
    agent._reset_budget_windows()
    events: list = []

    async def _sink(ev):
        events.append(ev)

    agent.event_sink = _sink
    agent.context_mgr = SimpleNamespace(compress=None)
    agent._attach_usage_sink()

    async def _no_compress():
        events.append({"type": "_compressed"})
    agent._compress_context = _no_compress
    return agent, events


async def _burn(agent, n: int) -> None:
    await agent.llm.usage_sink({"prompt_tokens": n, "completion_tokens": 0, "total_tokens": n})


async def test_exhausted_window_rolls_instead_of_stopping():
    agent, events = _agent(budget=1000, auto_continue=2)
    await _burn(agent, 1000)

    await agent.llm.pre_call_hook()            # 不抛 —— 开了新窗口

    types = [e["type"] for e in events]
    assert "budget_window" in types
    assert types.index("_compressed") < types.index("budget_window"), "必须先压缩再续开"
    ev = next(e for e in events if e["type"] == "budget_window")
    assert (ev["window"], ev["max_windows"], ev["total_used"]) == (2, 3, 1000)
    assert agent.resources.tokens.tokens_used.total == 0
    assert "budget_exceeded" not in types


async def test_stops_after_auto_continue_is_used_up():
    agent, events = _agent(budget=100, auto_continue=2)
    for _ in range(2):
        await _burn(agent, 100)
        await agent.llm.pre_call_hook()
    await _burn(agent, 100)
    with pytest.raises(BudgetExhaustedError):
        await agent.llm.pre_call_hook()
    ex = next(e for e in events if e["type"] == "budget_exceeded")
    assert ex["windows"] == 3 and ex["total_used"] == 300, "累计用量要如实报给界面"


async def test_zero_auto_continue_keeps_the_old_behaviour():
    agent, events = _agent(budget=100, auto_continue=0)
    await _burn(agent, 100)
    with pytest.raises(BudgetExhaustedError):
        await agent.llm.pre_call_hook()
    assert not [e for e in events if e["type"] == "budget_window"]


async def test_wall_clock_is_extended_with_each_window():
    """续开的窗口不能马上又撞上 30 分钟墙钟 —— 截止时刻要同步推给执行器。"""
    agent, events = _agent(budget=100, auto_continue=3, task_seconds=1800)
    await _burn(agent, 100)
    await agent.llm.pre_call_hook()
    assert agent.react_executor.deadline_shift == 1800
    assert agent.plan_executor.deadline_shift == 1800
    ev = next(e for e in events if e["type"] == "budget_window")
    assert ev["deadline_extended_s"] == 1800

    agent._reset_budget_windows()                 # 新任务开始：顺延归零
    assert agent.react_executor.deadline_shift == 0


async def test_warning_reports_windows_left():
    agent, events = _agent(budget=1000, auto_continue=2)
    await _burn(agent, 850)
    await agent.llm.pre_call_hook()
    w = next(e for e in events if e["type"] == "budget_warning")
    assert (w["window"], w["windows_left"]) == (1, 2)


def test_react_loop_honours_deadline_shift():
    """源码接线：ReAct / Plan 两条循环的墙钟判据都要带上顺延量。"""
    import inspect

    from automind.planning import plan_executor, react_executor
    assert "deadline + self.deadline_shift" in inspect.getsource(react_executor)
    assert "deadline_shift" in inspect.getsource(plan_executor)


def test_config_default_enables_auto_continue():
    from automind.core.config import ExecutionConfig
    assert ExecutionConfig().budget_auto_continue > 0
