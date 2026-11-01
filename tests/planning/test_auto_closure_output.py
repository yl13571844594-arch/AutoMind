"""v1.11.24 回归：自主闭环修复轮不得覆盖主交付输出。

真实测评发现：ReAct 主任务已经读到 `ZEBRA-42`，但 Loop 验收误判后进入
修复轮，旧代码用修复轮输出整段替换主输出，最终用户只看到「1/1 步 / 部分
交付清单」，答案丢失。修复后修复轮输出必须追加，主交付保留。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from automind.agent import AutoMindAgent
from automind.core.types import InteractionMode


def _fake_agent() -> AutoMindAgent:
    agent = object.__new__(AutoMindAgent)
    agent.config = SimpleNamespace(execution=SimpleNamespace(
        auto_test=False,
        auto_review=False,
        auto_verify=True,
        auto_verify_max_rounds=1,
        auto_verify_fix_max_iterations=1,
    ))
    agent._interaction = InteractionMode.CODING
    agent.llm = object()
    agent._verify_state = {}
    agent._closure_no_new_window = False
    agent._fix_calls = 0

    async def _emit(_event):
        return None

    async def _loop_verify(_task, _output):
        return {"available": True, "done": False, "reason": "needs more work"}

    async def _run_react_in_fix_round(_task, _context, *, max_iterations):
        agent._fix_calls += 1
        assert max_iterations == 1
        return "FIXED-OUTPUT"

    agent._emit = _emit
    agent._loop_verify = _loop_verify
    agent._run_react_in_fix_round = _run_react_in_fix_round
    return agent


def test_fix_round_appends_instead_of_replacing_main_output():
    agent = _fake_agent()
    result = asyncio.run(agent._autonomy_closure(
        "read note.txt", "MAIN-ANSWER-ZEBRA-42", "ctx"))
    assert agent._fix_calls == 1
    assert "MAIN-ANSWER-ZEBRA-42" in result
    assert "FIXED-OUTPUT" in result
    assert result.index("MAIN-ANSWER-ZEBRA-42") < result.index("FIXED-OUTPUT")
