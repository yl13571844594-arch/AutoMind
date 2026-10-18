"""v1.11.10 提示前缀稳定性 —— 让 DeepSeek / GLM 的自动前缀缓存真的能命中。

## 修的是什么

这三处都会让"已经发出去的前缀"发生变化，而前缀缓存只认**逐字节相同的前缀**：

1. ReAct 在工具集扩充时**改写第 2 条 system 消息**（工具目录）。任务中段改一次，
   后面几十 k 的历史全部按未命中计费。
2. 对话模式每轮 ``hist[-20:]``：超过 20 条后每轮从头丢 1 条，等于**每轮都换前缀**。
3. 记忆片段被拼进 ``Environment:`` 系统消息里：每个任务记忆都不一样，把环境与
   项目索引的缓存一起作废。

这个文件把三条都钉死，并顺带覆盖"ReAct 在跑时不再白花钱做一次 ContextManager
摘要"（那次调用是一次 0% 命中的一次性提示，纯亏）。
"""

from __future__ import annotations

import asyncio
import copy

from automind.core.chat_window import (
    CHAT_HISTORY_MAX,
    CHAT_HISTORY_TRIM,
    chat_history_window,
    chat_window_start,
)
from automind.core.types import LLMResponse, PermissionTier, ToolCall, ToolResult
from automind.planning.react_executor import (
    ReActExecutor,
    build_initial_messages,
)

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.parameters = {"properties": {"path": {"type": "string"}}, "required": []}
        self.permission_tier = PermissionTier.SAFE

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object",
                               "properties": self.parameters["properties"],
                               "required": []}}


class _Registry:
    def __init__(self, names: list[str]) -> None:
        self._tools = {n: _Tool(n) for n in names}
        self.calls: list[tuple[str, dict]] = []

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def list_all(self) -> list[_Tool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def get(self, name: str) -> _Tool:
        return self._tools[name]

    async def dispatch(self, name: str, **kw) -> ToolResult:
        self.calls.append((name, dict(kw)))
        return ToolResult(tool_name=name, success=True, output="ok")


class _CapturingLLM:
    """每次调用前把 messages **深拷贝**一份 —— 后续改写才看得见。"""

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.seen: list[list[dict]] = []

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.seen.append(copy.deepcopy(messages))
        return self._script.pop(0) if self._script else LLMResponse(text="done")


# ═══════════════════════════════════════════════════════════
# 1. 初始消息顺序：稳定前缀在前、易变内容在后
# ═══════════════════════════════════════════════════════════


class TestBuildInitialMessages:
    def test_order_is_stable_first(self):
        msgs = build_initial_messages("SYS", "CATALOG", "ENV", "MEMORY", "TASK")
        assert [m["role"] for m in msgs] == [
            "system", "system", "system", "system", "user"]
        assert msgs[0]["content"] == "SYS"
        assert msgs[1]["content"] == "CATALOG"
        assert msgs[2]["content"] == "Environment:\nENV"
        assert msgs[3]["content"] == "MEMORY"
        assert msgs[4]["content"] == "TASK"

    def test_memories_do_not_break_stable_prefix(self):
        """两次任务记忆不同，但系统提示/目录/环境这段前缀必须逐字节相同。"""
        a = build_initial_messages("SYS", "CAT", "ENV", "- 旧记忆", "任务 A")
        b = build_initial_messages("SYS", "CAT", "ENV", "- 新记忆", "任务 B")
        assert a[:3] == b[:3], "易变记忆把稳定前缀带崩了"

    def test_empty_parts_are_omitted(self):
        msgs = build_initial_messages("SYS", "", "", "", "TASK")
        assert [m["role"] for m in msgs] == ["system", "user"]


# ═══════════════════════════════════════════════════════════
# 2. ReAct 工具目录只追加、不改写
# ═══════════════════════════════════════════════════════════


class TestReActPrefixStability:
    def test_tool_expansion_appends_instead_of_rewriting(self):
        reg = _Registry(["alpha_tool", "beta_tool", "gamma_tool"])
        llm = _CapturingLLM([
            LLMResponse(text="下一步我会用 beta_tool 处理",
                        tool_calls=[ToolCall(id="1", name="alpha_tool",
                                             arguments={"path": "x"})]),
            LLMResponse(text="done"),
        ])
        ex = ReActExecutor(llm, reg, tool_budget=1, max_iterations=4,
                           no_progress_limit=0)
        asyncio.run(ex.run("用 alpha_tool 处理这个文件"))

        assert len(llm.seen) >= 2, "至少应有两次模型调用"
        first, second = llm.seen[0], llm.seen[1]
        # 工具目录是第 2 条（system）。旧实现会在扩充时改写它 → 前缀失效。
        assert first[1]["content"] == second[1]["content"], (
            "工具目录被就地改写了 —— DeepSeek/GLM 的前缀缓存会从第 2 条起全部失效")
        # 新工具通过尾部追加的说明告知，而不是覆盖原目录
        assert any("工具目录更新" in str(m.get("content", ""))
                   for m in second), "扩充后应追加一条目录更新说明"
        assert second[-1]["role"] == "system" and "工具目录更新" in second[-1]["content"]

    def test_memory_note_goes_after_environment(self):
        reg = _Registry(["alpha_tool"])
        llm = _CapturingLLM([LLMResponse(text="done")])
        ex = ReActExecutor(llm, reg, tool_budget=0, max_iterations=2)
        asyncio.run(ex.run("任务", "PROJECT-INDEX", memory_note="[Relevant Memories]\n- x"))
        msgs = llm.seen[0]
        contents = [str(m.get("content", "")) for m in msgs]
        env_at = next(i for i, c in enumerate(contents) if c.startswith("Environment:"))
        mem_at = contents.index("[Relevant Memories]\n- x")
        assert env_at < mem_at, "记忆必须排在环境之后（稳定前缀优先）"


# ═══════════════════════════════════════════════════════════
# 3. 对话历史：格点裁剪，连续几轮共享前缀
# ═══════════════════════════════════════════════════════════


def _hist(n: int) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
            for i in range(n)]


class TestChatWindow:
    def test_no_trim_within_limit(self):
        h = _hist(CHAT_HISTORY_MAX)
        assert chat_history_window(h) == h

    def test_window_never_exceeds_max(self):
        for n in range(0, 80):
            assert len(chat_history_window(_hist(n))) <= CHAT_HISTORY_MAX

    def test_start_is_a_stable_grid(self):
        assert chat_window_start(CHAT_HISTORY_MAX) == 0
        assert chat_window_start(CHAT_HISTORY_MAX + 1) == CHAT_HISTORY_TRIM
        # 起点在若干轮内不变（每轮 +2 条消息）
        assert chat_window_start(CHAT_HISTORY_MAX + 3) == CHAT_HISTORY_TRIM
        assert chat_window_start(CHAT_HISTORY_MAX + 5) == CHAT_HISTORY_TRIM
        # 窗口将超限前一次前跳
        assert chat_window_start(CHAT_HISTORY_MAX + 7) == CHAT_HISTORY_TRIM * 2

    def test_consecutive_turns_share_prefix(self):
        """21/23/25 条历史时窗口共享同一前缀 —— 这正是缓存能命中的原因。"""
        w21 = chat_history_window(_hist(21))
        w23 = chat_history_window(_hist(23))
        w25 = chat_history_window(_hist(25))
        assert len(w21) == 15 and len(w23) == 17 and len(w25) == 19
        assert w23[:len(w21)] == w21
        assert w25[:len(w23)] == w23

    def test_trim_resets_once_then_stabilizes(self):
        w25 = chat_history_window(_hist(25))
        w27 = chat_history_window(_hist(27))
        assert w27[0]["content"] != w25[0]["content"], "超过上限后应一次性前跳"
        assert len(w27) == 15

    def test_window_starts_with_user(self):
        h = _hist(27)
        # 让裁剪点（12）落到 assistant 上，验证会跳过去
        h[12] = {"role": "assistant", "content": "cut-here"}
        h[13] = {"role": "assistant", "content": "also-cut"}
        w = chat_history_window(h)
        assert w[0]["role"] == "user"


# ═══════════════════════════════════════════════════════════
# 4. 压缩：ReAct 在跑时不再退回付费摘要
# ═══════════════════════════════════════════════════════════


class _FakeExecutor:
    def __init__(self, folded: int = 0, running: bool = True) -> None:
        self.running = running
        self.messages = [{"role": "user", "content": "x"}]
        self.folded = folded
        self.calls = 0

    def compact(self, keep_recent=None) -> dict:
        self.calls += 1
        return {"folded": self.folded, "chars_reclaimed": 100}


class _FakeContextManager:
    def __init__(self) -> None:
        self.calls = 0

    async def compress(self, llm=None) -> str:
        self.calls += 1
        return "summary"


def _bare_agent(events: list[dict]):
    from automind.agent import AutoMindAgent

    agent = object.__new__(AutoMindAgent)
    agent.event_sink = _collector(events)
    agent.llm = None            # `_compress_context` 会把 llm 传给摘要器
    return agent


def _collector(events: list[dict]):
    async def _sink(ev):
        events.append(ev)
    return _sink


class TestCompactionDoesNotBurnCalls:
    def test_no_fold_means_no_paid_summary(self):
        events: list[dict] = []
        agent = _bare_agent(events)
        agent.react_executor = _FakeExecutor(folded=0)
        agent.context_mgr = mgr = _FakeContextManager()
        asyncio.run(agent._compress_context())
        assert agent.react_executor.calls == 1
        assert mgr.calls == 0, "ReAct 在跑时不该再花钱做一次用不上的摘要"

    def test_fold_event_marks_cache_reset(self):
        events: list[dict] = []
        agent = _bare_agent(events)
        agent.react_executor = _FakeExecutor(folded=3)
        agent.context_mgr = mgr = _FakeContextManager()
        asyncio.run(agent._compress_context())
        assert mgr.calls == 0
        compacted = [e for e in events if e.get("type") == "context_compacted"]
        assert compacted and compacted[0]["cache_reset"] is True, (
            "折叠会改写早先消息，必须告诉前端：下一轮前缀缓存会重置一次")

    def test_idle_executor_keeps_context_manager_path(self):
        """ReAct 没在跑（对话/计划路径）时，原有摘要压缩行为保持不变。"""
        events: list[dict] = []
        agent = _bare_agent(events)
        agent.react_executor = _FakeExecutor(folded=0, running=False)
        agent.context_mgr = mgr = _FakeContextManager()
        asyncio.run(agent._compress_context())
        assert mgr.calls == 1
