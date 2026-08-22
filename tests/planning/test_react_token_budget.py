"""ReAct 的两处"钱花在哪"的修复 —— 工具下发预算 + 压缩压对对象。

背景（v1.6.2 及更早）：
  · ReAct 每一轮都把**全部 31 个工具**的 OpenAI schema 重发一遍，
    约 2.5 万字符 ≈ 6k~8k token/步；跑 20 步就有十几万 token 花在
    "告诉模型它有哪些工具"上，而其中绝大多数与任务毫无关系。
  · 预算告警时调用 `_compress_context()`，压的却是 ContextManager ——
    那份记录在 ReAct 路径上从来没进过请求体。等于花了摘要的钱、
    ReAct 的请求体一个 token 都没少。

这些测试锁住修复后的行为，也锁住"能力不能因此变少"这条底线。
"""

from __future__ import annotations

import asyncio

import pytest

from automind.core.types import LLMResponse, ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor


class _Tool:
    def __init__(self, name: str, desc: str = "does something") -> None:
        self.name = name
        self.description = desc
        self.parameters = {"properties": {"path": {"type": "string"}}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object",
                               "properties": self.parameters["properties"],
                               "required": []}}


class _Registry:
    """够用的工具注册表替身（真实注册表要拉起整个 Agent）。"""

    def __init__(self, names: list[str]) -> None:
        self._tools = {n: _Tool(n) for n in names}

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def list_all(self) -> list[_Tool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def get(self, name: str) -> _Tool:
        return self._tools[name]

    async def dispatch(self, name: str, **kw) -> ToolResult:
        return ToolResult(tool_name=name, success=True, output={"ok": True})


#: 与内置工具集同规模的一份名单
ALL_TOOLS = [
    "terminal", "file_read", "file_write", "file_edit", "file_search",
    "file_multi_edit", "python_sandbox", "browser", "web_fetch", "web_search",
    "http_request", "code_generate", "archive", "excel_tool", "word_tool",
    "pdf_tool", "ppt_tool", "email_tool", "calendar", "db_query", "csv_tool",
    "screenshot_tool", "ocr_tool", "image_tool", "chart_tool", "audio_tool",
    "video_tool", "git_tool", "process_tool", "clipboard_tool", "notify",
]


class _ScriptedLLM:
    """按脚本吐响应，并记下每次收到的 tools 与 messages。"""

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.tool_counts: list[int] = []
        self.seen_tools: list[set[str]] = []
        self.seen_messages: list[list[dict]] = []

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.tool_counts.append(len(tools or []))
        self.seen_tools.append({t["name"] for t in (tools or [])})
        self.seen_messages.append([dict(m) for m in messages])
        return self._script.pop(0) if self._script else LLMResponse(text="done")


def _run(ex: ReActExecutor, task: str, context: str = "") -> str:
    return asyncio.run(ex.run(task, context))


# ── 工具下发预算 ──────────────────────────────────────────────


def test_only_a_budgeted_subset_of_schemas_is_sent():
    """31 个工具不该每轮全发 —— 那是每步 6k~8k token 的固定开销。"""
    llm = _ScriptedLLM([LLMResponse(text="好的，我直接回答")])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS), tool_budget=14)
    _run(ex, "帮我把这段 Python 脚本跑一下")

    assert llm.tool_counts == [14], f"实际下发 {llm.tool_counts} 个工具 schema"
    assert ex.token_savings["tools_total"] == 31
    assert ex.token_savings["tools_sent"] == 14


def test_task_relevant_tools_win_the_budget():
    """中文任务也要挑得准 —— 只匹配英文工具名的话一个都命中不了。"""
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS), tool_budget=8)
    _run(ex, "把这份数据做成 Excel 表格，再画个柱状图表")

    sent = llm.seen_tools[0]
    assert "excel_tool" in sent, "任务里写着 Excel，却没把 excel_tool 发下去"
    assert "chart_tool" in sent, "任务里要画图表，却没把 chart_tool 发下去"


def test_core_tools_are_always_present():
    """读写文件和执行命令是任何任务的地基，不参与打分、必须常驻。"""
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS), tool_budget=6)
    _run(ex, "给客户发一封邮件")

    sent = llm.seen_tools[0]
    for core in ("terminal", "file_read", "file_write", "file_edit", "file_search"):
        assert core in sent, f"基础工具 {core} 被预算挤掉了"


def test_dormant_tools_are_listed_so_capability_is_not_lost():
    """没下发 schema 的工具必须在系统提示里留名 —— 否则等于砍掉了能力。"""
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS), tool_budget=10)
    _run(ex, "随便做点什么")

    sys_text = "\n".join(m["content"] for m in llm.seen_messages[0]
                         if m["role"] == "system")
    missing = set(ALL_TOOLS) - llm.seen_tools[0]
    for name in missing:
        assert name in sys_text, f"{name} 既没下发 schema，也没出现在目录里"


def test_naming_a_dormant_tool_loads_it_next_step():
    """模型在思考里点名休眠工具 → 下一轮补发完整 schema。"""
    llm = _ScriptedLLM([
        LLMResponse(text="这里需要用 ocr_tool 来识别图片里的文字",
                    tool_calls=[ToolCall(id="1", name="file_read",
                                         arguments={"path": "a.txt"})]),
        LLMResponse(text="好了"),
    ])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS), tool_budget=8)
    _run(ex, "处理一批文件")

    assert "ocr_tool" not in llm.seen_tools[0], "前提不成立：第一轮就发了 ocr_tool"
    assert "ocr_tool" in llm.seen_tools[1], "点名之后仍然没补发 ocr_tool"
    assert "ocr_tool" in ex.token_savings["tools_expanded"]


def test_budget_zero_means_send_everything():
    """留一条退路：设为 0 就是老行为（全量下发）。"""
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS), tool_budget=0)
    _run(ex, "任意任务")
    assert llm.tool_counts == [31]


def test_small_registries_are_never_trimmed():
    """工具本来就没几个时不该多此一举地挑挑拣拣。"""
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    ex = ReActExecutor(llm, _Registry(["terminal", "file_read"]), tool_budget=14)
    _run(ex, "任意任务")
    assert llm.tool_counts == [2]


# ── 压缩压对对象 ──────────────────────────────────────────────


def test_messages_are_reachable_from_outside():
    """预算吃紧时上层要压的就是这个列表 —— 它必须暴露出来。"""
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    ex = ReActExecutor(llm, _Registry(ALL_TOOLS))
    _run(ex, "任意任务", context="环境说明")
    assert ex.messages, "run() 之后 messages 是空的，外部根本压不到"
    assert any(m["role"] == "user" for m in ex.messages)


def test_compact_folds_old_observations_only():
    """折叠旧观察，最近几条原样保留 —— 模型正靠它们判断下一步。"""
    ex = ReActExecutor(_ScriptedLLM([]), _Registry(ALL_TOOLS))
    ex.messages = [{"role": "system", "content": "sys"}]
    for i in range(12):
        ex.messages.append({"role": "assistant", "content": f"think {i}"})
        ex.messages.append({"role": "tool", "tool_call_id": str(i),
                            "content": "X" * 5000})

    before = len(ex.messages)
    stat = ex.compact(keep_recent=4)

    assert len(ex.messages) == before, "压缩不该删消息（工具调用必须成对出现）"
    assert stat["folded"] > 0 and stat["chars_reclaimed"] > 40000
    assert len(ex.messages[2]["content"]) < 500, "旧观察没被折叠"
    assert ex.messages[-1]["content"] == "X" * 5000, "最近的观察被误伤了"


def test_compact_is_idempotent():
    """反复压不会越压越离谱（也不会把折叠说明再折叠一遍）。"""
    ex = ReActExecutor(_ScriptedLLM([]), _Registry(ALL_TOOLS))
    ex.messages = [{"role": "tool", "content": "Y" * 4000} for _ in range(6)]
    ex.compact(keep_recent=0)
    first = [m["content"] for m in ex.messages]
    ex.compact(keep_recent=0)
    assert [m["content"] for m in ex.messages] == first


def test_running_flag_tracks_the_live_run():
    """上层据此判断"现在压缩压得到实处吗"。"""
    seen = {}

    class _Peek(_ScriptedLLM):
        async def generate(self, messages, tools=None, **kw):
            seen["running"] = ex.running
            return await super().generate(messages, tools=tools, **kw)

    ex = ReActExecutor(_Peek([LLMResponse(text="ok")]), _Registry(ALL_TOOLS))
    assert ex.running is False
    _run(ex, "任意任务")
    assert seen["running"] is True, "run() 进行中 running 应为 True"
    assert ex.running is False, "run() 结束后 running 必须复位"


@pytest.mark.asyncio
async def test_agent_compresses_react_not_the_bystander_context():
    """预算告警时压的必须是 ReAct 的消息列表，而不是没人读的 ContextManager。"""
    from automind.agent import AutoMindAgent

    agent = AutoMindAgent.__new__(AutoMindAgent)
    agent.event_sink = None

    ex = ReActExecutor(_ScriptedLLM([]), _Registry(ALL_TOOLS))
    ex.running = True
    ex.messages = [{"role": "tool", "content": "Z" * 6000} for _ in range(10)]
    agent.react_executor = ex

    called = {"ctx": False}

    class _Ctx:
        async def compress(self, llm=None):
            called["ctx"] = True
            return ""

    agent.context_mgr = _Ctx()
    agent.llm = None

    await agent._compress_context()

    assert ex.token_savings["chars_reclaimed"] > 0, "ReAct 的消息列表没被压到"
    assert called["ctx"] is False, (
        "ReAct 已经压出空间了，还去调 LLM 做摘要 —— 那是白花的钱")
