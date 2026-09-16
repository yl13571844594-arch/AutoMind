"""中途插话（interjection）—— 收下、并入、以及**没并进去时必须说出来**。

## 修的是什么

AI 正在写长回答时，用户看到一半发现"它还漏了一件事"。此前的界面只有两条路：
等它写完再说（那一轮已经跑偏了），或者点停止（已生成的内容全丢）。而
**任务执行期间输入框是禁用的** —— 用户想补一句话，第一件要做的事竟然是"先停下"。

现在补上第三条：补一句，它带着这句话继续。

## 这个文件盯住的三件事

1. **收下 ≠ 生效**：队列把两件事分开记账，所以"没来得及纳入本轮"才可能被
   如实报出来。静默丢弃是这里最不能接受的失败方式 —— 用户看到自己那句话
   出现在屏幕上，就默认它生效了，而模型从头到尾没见过它。
2. **并入的是 user 消息，且措辞要说清"这是补充、不是新任务"**：否则模型会
   丢下手里的事重头开始，用户看到的是"回答写了一半突然重启"。
3. **有一轮上限**：每续写一次就是一次额外计费的 LLM 调用，而插话是手打的、
   可能连着来。到上限后剩余插话要退回去，不能吞。
"""

from __future__ import annotations

import pytest

from automind.core.interject import (
    MAX_PENDING,
    MAX_ROUNDS,
    InterjectionQueue,
    InterjectionTooLong,
    render_for_model,
)
from automind.core.types import LLMResponse, ToolCall, ToolResult

# ═══════════════════════════════════════════════════════════
# 1. 队列语义
# ═══════════════════════════════════════════════════════════


def test_push_and_drain_round_trip():
    q = InterjectionQueue()

    a = q.push("  改用 pandas  ")
    b = q.push("输出存成 csv")

    assert (a.seq, b.seq) == (1, 2)
    assert a.text == "改用 pandas", "首尾空白要清掉（用户手打时经常带）"
    assert q.pending() == 2
    assert [i.text for i in q.drain()] == ["改用 pandas", "输出存成 csv"]
    assert q.pending() == 0


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_empty_interjection_is_rejected_loudly(bad):
    q = InterjectionQueue()

    with pytest.raises(InterjectionTooLong):
        q.push(bad)


def test_overlong_interjection_is_rejected_with_the_numbers():
    q = InterjectionQueue(max_chars=10)

    with pytest.raises(InterjectionTooLong) as e:
        q.push("x" * 11)

    assert "11" in str(e.value) and "10" in str(e.value), \
        "拒绝时必须把实际长度和上限都写出来，用户才知道要删多少"


def test_pending_cap_prevents_using_interjection_as_a_firehose():
    q = InterjectionQueue(max_pending=MAX_PENDING)
    for i in range(MAX_PENDING):
        q.push(f"第 {i} 条")

    with pytest.raises(InterjectionTooLong):
        q.push("再来一条")


def test_report_separates_accepted_applied_and_dropped():
    q = InterjectionQueue()
    q.push("a")
    q.drain()

    rep = q.report()

    assert rep["accepted"] == 1
    assert rep["applied"] == 0, "取走 ≠ 交给模型：两件事必须分开记账"
    assert rep["pending"] == 0


# ═══════════════════════════════════════════════════════════
# 2. 给模型看的那段话
# ═══════════════════════════════════════════════════════════


def test_render_says_it_is_a_supplement_not_a_new_task():
    q = InterjectionQueue()
    block = render_for_model([q.push("排序改成按时间倒序")])

    assert "排序改成按时间倒序" in block
    assert "不是新任务" in block, "不说这句，模型会丢下手里的事重头开始"
    assert "补充" in block


def test_render_tells_the_model_not_to_rewrite_what_it_already_said():
    q = InterjectionQueue()
    continuing = render_for_model([q.push("再补一段测试")], continuing=True)

    assert "接着" in continuing and "不要" in continuing


# ═══════════════════════════════════════════════════════════
# 3. 对话模式：边流边接
# ═══════════════════════════════════════════════════════════


class _StreamLLM:
    """每次流式生成固定吐 3 段；可按回调在指定时刻"用户插话"。"""

    def __init__(self, on_delta=None) -> None:
        self.on_delta = on_delta
        self.calls: list[list[dict]] = []

    def reset(self) -> None:
        pass

    def token_count(self, text: str) -> int:
        return max(1, len(str(text)) // 4)

    async def generate_stream(self, messages):
        self.calls.append([dict(m) for m in messages])
        idx = len(self.calls) - 1
        for k in range(3):
            if self.on_delta:
                self.on_delta(idx, k)
            yield f"R{idx}-{k}"


def _make_agent(llm):
    """造一个**真的 AutoMindAgent 实例**，只补上 chat_stream 需要的那几个属性。

    不用 ``AutoMindAgent(...)`` 构造：那会去探测环境、建记忆库、连 LLM，
    与本文件要验的东西无关。但也不能另造一个假类 —— 那样验的就不是真代码了
    （``CHAT_SYSTEM_PROMPT``、``_emit``、``interject`` 都应当是产品代码本身）。
    """
    from automind.agent import AutoMindAgent
    from automind.core.interject import InterjectionQueue as Q

    agent = object.__new__(AutoMindAgent)
    agent.llm = llm
    agent._chat_history = []
    agent._interjections = Q()
    agent.events = []

    async def _sink(ev):
        agent.events.append(ev)

    agent.event_sink = _sink
    return agent


async def _run_chat(agent, task="写个脚本"):
    return "".join([chunk async for chunk in agent.chat_stream(task)])


async def test_chat_stream_continues_with_the_supplement_in_the_same_answer():
    agent = _make_agent(_StreamLLM())
    agent.interject("改成用 pandas")

    out = await _run_chat(agent)

    # 第 0 轮在第一个 delta 后就被打断，第 1 轮续完 —— 用户看到的是一段连着的回答
    assert out == "R0-0R1-0R1-1R1-2"
    assert len(agent.llm.calls) == 2
    second = agent.llm.calls[1]
    assert any(m["role"] == "assistant" and m["content"] == "R0-0" for m in second), \
        "已生成的部分必须留在上下文里，否则模型会从头重写"
    assert any(m["role"] == "user" and "改成用 pandas" in str(m["content"])
               for m in second)


async def test_applied_event_is_emitted_so_the_user_knows_it_landed():
    agent = _make_agent(_StreamLLM())
    agent.interject("补充一")

    await _run_chat(agent)

    assert any(e.get("type") == "interjection_applied" for e in agent.events), \
        "收下时已经回过执了，并入时必须再回一条 —— 否则用户只能猜"


async def test_supplement_arriving_mid_stream_is_picked_up():
    agent = _make_agent(_StreamLLM(
        on_delta=lambda i, k: agent.interject("中途想起的一件事")
        if (i == 0 and k == 1) else None))

    out = await _run_chat(agent)

    assert out.startswith("R0-0R0-1")
    assert "中途想起的一件事" not in out, "插话是给模型的输入，不该混进回答正文"
    assert any(m["role"] == "user" and "中途想起的一件事" in str(m["content"])
               for m in agent.llm.calls[1])


async def test_supplement_is_kept_in_history_for_the_next_turn():
    """这轮被纠正过，下一轮必须还记得 —— 否则模型会再犯同一个错。"""
    agent = _make_agent(_StreamLLM())
    agent.interject("不要用 numpy")

    await _run_chat(agent)

    roles = [m["role"] for m in agent._chat_history]
    assert roles == ["user", "user", "assistant"]
    assert agent._chat_history[1]["content"] == "不要用 numpy"


async def test_round_cap_keeps_the_extra_supplement_pending_instead_of_dropping_it():
    """上限存在的意义是控成本；但到上限的插话**不能吞**，要退回去如实报。"""
    agent = _make_agent(_StreamLLM(
        on_delta=lambda i, k: agent.interject(f"第 {i} 轮的补充") if k == 0 else None))

    await _run_chat(agent)

    assert len(agent.llm.calls) == MAX_ROUNDS + 1, "续写次数必须封顶"
    assert agent.pending_interjections() == 1, \
        "超出上限的那条要留在队列里（由上层告知用户），不能静默丢弃"
    assert agent.interjection_report()["accepted"] == MAX_ROUNDS + 1


async def test_no_supplement_means_exactly_one_call():
    agent = _make_agent(_StreamLLM())

    out = await _run_chat(agent)

    assert out == "R0-0R0-1R0-2"
    assert len(agent.llm.calls) == 1, "没有插话时不许额外多打一次 LLM"


# ═══════════════════════════════════════════════════════════
# 4. ReAct 模式：步骤边界并入
# ═══════════════════════════════════════════════════════════


class _Tool:
    name = "file_read"
    description = "读文件"
    parameters = {"properties": {"path": {"type": "string"}}, "required": []}
    permission_tier = __import__("automind.core.types", fromlist=["PermissionTier"]).PermissionTier.SAFE

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object",
                               "properties": self.parameters["properties"],
                               "required": []}}


class _Registry:
    def __init__(self) -> None:
        self._tools = {"file_read": _Tool()}

    def list_names(self):
        return sorted(self._tools)

    def list_all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools[name]

    async def dispatch(self, name, **kw) -> ToolResult:
        return ToolResult(tool_name=name, success=True, output="文件内容")


class _InterjectingLLM:
    """第一轮调用时"用户插话"，第二轮的消息里就必须能看到它。"""

    def __init__(self, queue) -> None:
        self.queue = queue
        self.seen: list[list[dict]] = []

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.seen.append([dict(m) for m in messages])
        if len(self.seen) == 1:
            self.queue.push("顺便把编码改成 utf-8")
            return LLMResponse(text="先读文件", tool_calls=[
                ToolCall(id="1", name="file_read", arguments={"path": "a.py"})])
        return LLMResponse(text="done")


async def test_react_absorbs_the_supplement_at_the_step_boundary():
    from automind.planning.react_executor import ReActExecutor

    q = InterjectionQueue()
    llm = _InterjectingLLM(q)
    events: list[dict] = []

    ex = ReActExecutor(llm, _Registry(), max_iterations=4,
                       interjection_source=q.drain)
    out = await ex.run("改一下配置", on_interjection=lambda e: _collect(events, e))

    assert out == "done"
    assert len(llm.seen) == 2
    assert any(m["role"] == "user" and "顺便把编码改成 utf-8" in str(m["content"])
               for m in llm.seen[1]), \
        "补充必须在**下一次思考之前**进入消息列表，否则这一轮白补"
    assert ex.interjections_merged and ex.interjections_merged[0]["at"] == "react_step"
    assert events and events[0]["type"] == "interjection_applied"


async def _collect(bucket: list, event: dict) -> None:
    bucket.append(event)


async def test_react_manifest_admits_the_supplement_was_merged():
    from automind.planning.react_executor import ReActExecutor

    q = InterjectionQueue()
    ex = ReActExecutor(_InterjectingLLM(q), _Registry(), max_iterations=4,
                       interjection_source=q.drain)
    await ex.run("改一下配置")

    rep = ex.partial_report("改一下配置")

    assert rep["interjections_merged"] == 1, "交付清单里要能看出补充进没进去"


async def test_react_without_a_source_still_runs():
    """老调用路径（不传 interjection_source）不能被这次改动弄坏。"""
    from automind.planning.react_executor import ReActExecutor

    ex = ReActExecutor(_InterjectingLLM(InterjectionQueue()), _Registry(),
                       max_iterations=2)
    out = await ex.run("随便做点什么")

    assert out == "done"
    assert ex.interjections_merged == []
