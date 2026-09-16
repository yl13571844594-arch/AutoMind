"""ReAct「原地打转」治理 —— 无进展检测 + 只读结果跨轮复用。

## 修的是什么

此前 ReAct 只有一道熔断：**同一工具连续失败 3 次**。它挡的是"坏工具被反复
调用"，挡不住"**成功但毫无进展**"的动作 —— 而后者才是卡壳最常见的形态：

    file_read("app.py")   → 成功，第 1 次
    file_read("app.py")   → 成功，第 2 次（参数逐字相同）
    file_read("app.py")   → 成功，第 3 次 …… 一路烧到 max_iterations=50

不报错、不熔断、不提醒，用户最后只拿到「部分交付清单」。代价还特别高：
ReAct 每一步都要重发工具 schema + 全量消息，转一圈就是几万 token。

另一条并行的浪费：Plan 路径早有 ``_subtask_cache`` 复用只读调用，ReAct 没有。
同一份文件内容被反复读进上下文，只有体积截断（output_budget）与旧消息折叠
（compact）在救，而折叠要等预算用到 80% 才触发。
"""

from __future__ import annotations

import asyncio

from automind.core.types import (
    LLMResponse,
    PermissionTier,
    ToolCall,
    ToolResult,
)
from automind.planning.react_executor import ReActExecutor
from automind.planning.react_progress import ReactProgressGuard, action_key
from automind.tools.function_calling import FunctionCallHandler

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str, tier: PermissionTier = PermissionTier.SAFE) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.parameters = {"properties": {"path": {"type": "string"}}, "required": []}
        self.permission_tier = tier

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object",
                               "properties": self.parameters["properties"],
                               "required": []}}


class _Registry:
    def __init__(self, tools: list[_Tool]) -> None:
        self._tools = {t.name: t for t in tools}
        self.calls: list[tuple[str, dict]] = []
        #: 每个工具的返回值（默认给一段"很大"的内容，便于观察是否被重发）
        self.outputs: dict[str, object] = {}
        self.hang: set[str] = set()

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def list_all(self) -> list[_Tool]:
        return [self._tools[n] for n in sorted(self._tools)]

    def get(self, name: str) -> _Tool:
        return self._tools[name]

    async def dispatch(self, name: str, **kw) -> ToolResult:
        self.calls.append((name, dict(kw)))
        if name in self.hang:
            await asyncio.sleep(3600)          # 模拟"挂住不返回"
        out = self.outputs.get(name, "X" * 5000)
        return ToolResult(tool_name=name, success=True, output=out)


class _ScriptedLLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls = 0

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.calls += 1
        return self._script.pop(0) if self._script else LLMResponse(text="done")


def _read_calls(n: int, path: str = "app.py") -> list[LLMResponse]:
    """连续 n 轮都请求**完全相同**的 file_read。"""
    return [LLMResponse(text=f"再看一眼 {path}（第 {i + 1} 次）",
                        tool_calls=[ToolCall(id=str(i), name="file_read",
                                             arguments={"path": path})])
            for i in range(n)]


def _executor(script, registry, task: str = "看看这个文件", **kw) -> ReActExecutor:
    llm = _ScriptedLLM(script)
    ex = ReActExecutor(llm, registry, **kw)
    asyncio.run(ex.run(task))
    return ex


# ═══════════════════════════════════════════════════════════
# 1. 无进展检测
# ═══════════════════════════════════════════════════════════


class TestNoProgressDetection:
    def test_repeated_identical_action_is_guided_then_blocked(self):
        """连续第 2 次相同动作：仍执行但附带纠偏提示；第 3 次直接拦截。"""
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(6), reg, max_iterations=6,
                       repeat_threshold=2, no_progress_limit=0)
        rep = ex.progress.report()
        assert rep["repeats"] >= 4, rep
        assert rep["guided"] == 1, "第一次重复应当只给提示、不拦截"
        assert rep["blocked"] >= 1, "继续重复必须被拦截"

    def test_guided_result_carries_actionable_advice(self):
        """提示必须**具体**（"换个招数"式空话模型会再试一遍）。"""
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(4), reg, max_iterations=4,
                       repeat_threshold=2, no_progress_limit=0)
        blocked = [r for _, r in ex.actions if not r.success
                   and "未检测到进展" in str(r.error)]
        assert blocked, "没有任何被拦截的重复动作"
        text = str(blocked[0].error)
        assert "offset/limit" in text or "file_search" in text, \
            f"纠偏提示不够具体：{text}"

    def test_blocked_action_is_not_dispatched(self):
        """被拦截的动作不该真的执行 —— 否则"拦截"只是嘴上说说。"""
        reg = _Registry([_Tool("file_read")])
        _executor(_read_calls(5), reg, max_iterations=5,
                  repeat_threshold=2, no_progress_limit=0)
        # 第 1 次执行 + 第 2 次（缓存复用，不重复 dispatch），第 3 次起拦截
        assert len(reg.calls) <= 2, f"被拦截的动作仍然被执行了：{reg.calls}"

    def test_different_arguments_are_never_blocked(self):
        """参数不同 = 有可能真在进展（分段读文件、逐条处理），绝不误杀。"""
        script = [LLMResponse(text="读第 i 段",
                              tool_calls=[ToolCall(id=str(i), name="file_read",
                                                   arguments={"path": f"part{i}.txt"})])
                  for i in range(6)]
        reg = _Registry([_Tool("file_read")])
        ex = _executor(script, reg, max_iterations=6, repeat_threshold=2,
                       no_progress_limit=0)
        assert ex.progress.blocked == 0, "参数不同的连续调用被误判为无进展"
        assert len(reg.calls) == 6, "每次不同参数的调用都该真实执行"

    def test_different_tools_are_never_blocked(self):
        script = [LLMResponse(text="换工具", tool_calls=[ToolCall(id=str(i), name=n,
                                                                 arguments={"path": "x"})])
                  for i, n in enumerate(["file_read", "file_search", "file_read"])]
        reg = _Registry([_Tool("file_read"), _Tool("file_search")])
        ex = _executor(script, reg, max_iterations=3, repeat_threshold=2,
                       no_progress_limit=0)
        assert ex.progress.blocked == 0

    def test_no_progress_stops_before_max_iterations(self):
        """反复做同一件事时提前收尾，而不是把 50 步全烧完。"""
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(50), reg, max_iterations=50,
                       repeat_threshold=2, no_progress_limit=3)
        assert ex.stop_reason == "no_progress", ex.stop_reason
        assert ex.iterations_used < 10, f"还是跑了 {ex.iterations_used} 步"

    def test_partial_report_carries_the_no_progress_evidence(self):
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(50), reg, max_iterations=50,
                       repeat_threshold=2, no_progress_limit=3)
        rep = ex.partial_report("看看这个文件")
        assert rep["no_progress"]["blocked"] >= 1
        assert rep["stop_reason"] == "no_progress"
        text = ReActExecutor.render_manifest(rep)
        assert "无进展" in text and "重复动作" in text, text

    def test_no_progress_callback_is_invoked(self):
        """用户要能实时看到"它卡在重复读同一个文件"。"""
        reg = _Registry([_Tool("file_read")])
        seen: list[dict] = []

        async def on_np(ev):
            seen.append(ev)

        async def _go():
            ex = ReActExecutor(_ScriptedLLM(_read_calls(8)), reg,
                               max_iterations=8, repeat_threshold=2,
                               no_progress_limit=3)
            await ex.run("看看这个文件", on_no_progress=on_np)
            return ex

        ex = asyncio.run(_go())
        assert seen, "无进展事件没有推给上层"
        assert seen[0]["type"] == "react_no_progress"
        assert seen[0]["tool"] == "file_read"
        assert ex.progress.blocked >= 1

    def test_threshold_one_blocks_immediately(self):
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(4), reg, max_iterations=4,
                       repeat_threshold=1, no_progress_limit=0,
                       result_cache=False)
        # threshold=1 表示"第一次重复就提示并拦截"
        assert ex.progress.repeats == 3
        assert ex.progress.blocked == 3

    def test_progress_guide_message_names_the_repeat_count(self):
        guard = ReactProgressGuard(threshold=2)
        assert guard.check("file_read", {"path": "a"})[0] == "run"
        verdict, advice = guard.check("file_read", {"path": "a"})
        assert verdict == "guide" and "2 次" in advice
        verdict, advice = guard.check("file_read", {"path": "a"})
        assert verdict == "block" and "3 次" in advice

    def test_action_key_is_order_and_type_sensitive(self):
        assert action_key("t", {"a": 1, "b": 2}) == action_key("t", {"b": 2, "a": 1})
        assert action_key("t", {"a": 1}) != action_key("t", {"a": "1"})
        assert action_key("t", {"a": 1}) != action_key("u", {"a": 1})

    def test_action_key_survives_unserializable_arguments(self):
        class _Weird:
            def __repr__(self) -> str:
                return "<weird>"
        key = action_key("t", {"obj": _Weird()})
        assert key.startswith("t::")


# ═══════════════════════════════════════════════════════════
# 2. 只读结果跨轮复用
# ═══════════════════════════════════════════════════════════


class TestReadOnlyResultReuse:
    def test_identical_read_is_not_dispatched_twice(self):
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(4), reg, max_iterations=4,
                       repeat_threshold=3, no_progress_limit=0)
        assert len(reg.calls) == 1, f"同一份内容被重复执行了：{reg.calls}"
        assert ex.progress.cache_hits >= 1

    def test_write_tools_are_never_cached(self):
        """写类工具同参重放可能是**故意的**（重试、幂等写），绝不能缓存。"""
        reg = _Registry([_Tool("file_write", PermissionTier.SENSITIVE)])
        script = [LLMResponse(text="写", tool_calls=[ToolCall(id=str(i), name="file_write",
                                                              arguments={"path": "a.py"})])
                  for i in range(3)]
        _executor(script, reg, max_iterations=3, repeat_threshold=9,
                  no_progress_limit=0)
        assert len(reg.calls) == 3, "写类工具被错误地缓存了"

    def test_side_effect_tools_are_never_cached_even_if_marked_safe(self):
        """等级被误标成 SAFE 也不能缓存有副作用的工具（兜底名单）。"""
        reg = _Registry([_Tool("notify", PermissionTier.SAFE)])
        script = [LLMResponse(text="通知", tool_calls=[ToolCall(id=str(i), name="notify",
                                                                arguments={"msg": "hi"})])
                  for i in range(3)]
        _executor(script, reg, max_iterations=3, repeat_threshold=9,
                  no_progress_limit=0)
        assert len(reg.calls) == 3, "有副作用的工具被缓存了"

    def test_cache_can_be_disabled(self):
        reg = _Registry([_Tool("file_read")])
        _executor(_read_calls(3), reg, max_iterations=3, repeat_threshold=9,
                  no_progress_limit=0, result_cache=False)
        assert len(reg.calls) == 3

    def test_failed_read_is_not_cached(self):
        reg = _Registry([_Tool("file_read")])

        async def _go():
            async def _fail(name, **kw):
                reg.calls.append((name, kw))
                return ToolResult(tool_name=name, success=False, error="boom")
            reg.dispatch = _fail        # type: ignore[assignment]
            ex = ReActExecutor(_ScriptedLLM(_read_calls(3)), reg,
                               max_iterations=3, repeat_threshold=9,
                               no_progress_limit=0)
            await ex.run("读")
            return ex

        asyncio.run(_go())
        assert len(reg.calls) == 3, "失败的调用不该被缓存（下次可能成功）"


# ═══════════════════════════════════════════════════════════
# 3. 上下文侧的去重（结果不再原样重发）
# ═══════════════════════════════════════════════════════════


class TestContextDedupe:
    def test_identical_call_content_is_not_resent(self):
        h = FunctionCallHandler(_Registry([_Tool("file_read")]))
        tc = ToolCall(id="1", name="file_read", arguments={"path": "big.txt"})
        result = ToolResult(tool_name="file_read", success=True,
                            output="Y" * 4000)
        first = h.tool_results_to_messages([tc], [result])[0]["content"]
        assert "Y" * 4000 in first
        second = h.tool_results_to_messages(
            [ToolCall(id="2", name="file_read", arguments={"path": "big.txt"})],
            [result])[0]["content"]
        assert len(second) < 400, f"重复结果被原样重发（{len(second)} 字符）"
        assert "完全相同" in second
        assert h.savings_report()["deduped_results"] == 1

    def test_changed_result_is_still_delivered_in_full(self):
        """同参数但结果变了（文件被改过）→ 是新信息，必须完整下发。"""
        h = FunctionCallHandler(_Registry([_Tool("file_read")]))
        tc = ToolCall(id="1", name="file_read", arguments={"path": "a.txt"})
        h.tool_results_to_messages(
            [tc], [ToolResult(tool_name="file_read", success=True, output="v1")])
        second = h.tool_results_to_messages(
            [ToolCall(id="2", name="file_read", arguments={"path": "a.txt"})],
            [ToolResult(tool_name="file_read", success=True, output="v2")],
        )[0]["content"]
        assert "v2" in second and "已经变化" in second

    def test_different_arguments_are_not_deduped(self):
        h = FunctionCallHandler(_Registry([_Tool("file_read")]))
        a = h.tool_results_to_messages(
            [ToolCall(id="1", name="file_read", arguments={"path": "a"})],
            [ToolResult(tool_name="file_read", success=True, output="AAA")])[0]["content"]
        b = h.tool_results_to_messages(
            [ToolCall(id="2", name="file_read", arguments={"path": "b"})],
            [ToolResult(tool_name="file_read", success=True, output="BBB")])[0]["content"]
        assert "AAA" in a and "BBB" in b

    def test_token_report_exposes_dedupe_and_reuse(self):
        reg = _Registry([_Tool("file_read")])
        ex = _executor(_read_calls(3), reg, max_iterations=3,
                       repeat_threshold=9, no_progress_limit=0)
        rep = ex.token_report()
        assert "duplicate_results_shortcircuited" in rep
        assert "results_reused" in rep
        assert "repeat_actions_blocked" in rep
        assert rep["results_reused"] >= 1
