"""读目标守卫（v1.11.20）—— 反复读同一份内容必须在烧光预算前叫停。

## 修的是什么

原有"无进展"判据只看**动作指纹是否逐字相同**。于是模型只要每次换一个
offset / limit（或把 query 改一个细微写法），指纹就不同，护栏全部失效 ——
但真正在读的仍是同一份内容，上下文早被它塞满了。实测 150 步跑飞的长任务里，
最常见的形态就是"同一个文件反复读、每次换一点点参数"。

v1.11.20 在 ``ReactProgressGuard`` 里加了**读目标**维度：

* 目标识别：``file_read`` → path（忽略 offset/limit）；``file_search`` →
  path + query；``web_fetch`` → url；``web_search`` → query；
  ``http_request`` → method + url；
* 同一目标累计：``< limit-2`` 放行、``== limit-2`` 提示、``>= limit-1`` 拦截
  （默认 limit=8 → 1~5 放行、第 6 次提示、第 7 次起拦截）；
* 被拦计入 ``blocked_tools`` / ``no_progress_streak``，报告里多出
  ``read_target_hits`` / ``read_targets``；
* 只作用于只读目标：写工具与 terminal/pwsh 不纳入。
"""

from __future__ import annotations

import asyncio

from automind.core.types import LLMResponse, PermissionTier, ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor
from automind.planning.react_progress import ReactProgressGuard, read_target

# ═══════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.permission_tier = PermissionTier.SAFE
        self.parameters = {"type": "object", "properties": {}, "required": []}

    def to_openai_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []}}


class _Registry:
    def __init__(self, names: list[str]) -> None:
        self._names = list(names)
        self.calls: list[tuple[str, dict]] = []

    def list_names(self) -> list[str]:
        return list(self._names)

    def list_all(self) -> list[_Tool]:
        return [_Tool(n) for n in self._names]

    def get(self, name: str) -> _Tool:
        return _Tool(name)

    async def dispatch(self, name: str, **kw) -> ToolResult:
        self.calls.append((name, dict(kw)))
        return ToolResult(tool_name=name, success=True, output="内容")


class _ScriptedLLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        return self._script.pop(0) if self._script else LLMResponse(text="完成")


def _read_script(n: int, path: str = "big.py") -> list[LLMResponse]:
    """连续 n 轮都读**同一个文件**，但每轮 offset 不同（绕开逐字重复判据）。"""
    return [LLMResponse(text=f"读第 {i + 1} 段",
                        tool_calls=[ToolCall(id=str(i), name="file_read",
                                             arguments={"path": path,
                                                        "offset": i * 100,
                                                        "limit": 100})])
            for i in range(n)]


# ═══════════════════════════════════════════════════════════
# 1. 阈值语义
# ═══════════════════════════════════════════════════════════


def test_sixth_read_is_guided_and_seventh_blocked() -> None:
    """默认 limit=8：1~5 放行、第 6 次 guide、第 7 次起 block。"""
    guard = ReactProgressGuard(threshold=99)      # 关掉"逐字重复"判据
    verdicts = []
    for i in range(5):
        verdicts.append(guard.check(
            "file_read", {"path": "a.py", "offset": i * 100})[0])
    assert verdicts == ["run"] * 5, f"前 5 次不该被拦：{verdicts}"

    verdict, advice = guard.check("file_read", {"path": "a.py", "offset": 500})
    assert verdict == "guide", f"第 6 次应提示：{verdict}"
    assert "第 6 次" in advice and "上限 8" in advice, advice
    assert "offset/limit" in advice and "file_search" in advice, advice
    assert "不要重复整文件读取" in advice, "提示必须明确禁止重复整文件读取"

    verdict, advice = guard.check("file_read", {"path": "a.py", "offset": 600})
    assert verdict == "block", f"第 7 次应拦截：{verdict}"
    assert "本次未执行" in advice, advice

    assert guard.read_target_hits == 2, "guide + block 各算一次命中"
    assert guard.read_target_blocked == 1
    assert guard.blocked_tools.get("file_read") == 1
    assert guard.no_progress_streak >= 1, "读目标拦截也要计入无进展连续长度"


def test_same_file_different_offset_is_still_the_same_target() -> None:
    """换 offset/limit 不算换目标 —— 同一文件就是同一份内容。"""
    assert read_target("file_read", {"path": "a.py", "offset": 10}) == \
        read_target("file_read", {"path": "a.py", "offset": 999})
    assert read_target("file_read", {"path": "a.py"}) == "path=a.py"


def test_different_files_are_not_hit() -> None:
    guard = ReactProgressGuard(threshold=99)
    for i in range(12):
        verdict, _ = guard.check("file_read", {"path": f"file{i}.py"})
        assert verdict == "run", f"不同文件的第 {i + 1} 次被误拦"
    assert guard.read_target_hits == 0
    assert guard.blocked == 0


def test_different_read_tools_have_independent_targets() -> None:
    guard = ReactProgressGuard(threshold=99)
    # 三路工具各读 5 次不同目标 → 全是 run
    for i in range(5):
        assert guard.check("web_search", {"query": f"问题 {i}"})[0] == "run"
        assert guard.check("web_fetch", {"url": f"https://example.com/{i}"})[0] == "run"
        assert guard.check("file_search", {"path": "src", "query": f"关键词 {i}"})[0] == "run"
    assert guard.read_target_hits == 0


def test_http_request_target_includes_method() -> None:
    """同一 URL 的 GET / POST 是两个不同目标；同方法同 URL 才算重复。"""
    guard = ReactProgressGuard(threshold=99)
    for _ in range(5):
        assert guard.check("http_request",
                           {"method": "GET", "url": "https://api.example.com/x"})[0] == "run"
    # 5 次 GET 后换 POST：目标不同，不该被拦
    assert guard.check("http_request",
                       {"method": "POST", "url": "https://api.example.com/x"})[0] == "run"


def test_non_read_tools_are_untouched() -> None:
    """写工具 / terminal / pwsh 不纳入读目标守卫（精确重复仍由原判据处理）。"""
    guard = ReactProgressGuard(threshold=99)
    for i in range(10):
        assert guard.check("file_write", {"path": "a.py", "content": f"v{i}"})[0] == "run"
        assert guard.check("terminal", {"command": f"echo {i}"})[0] == "run"
    assert guard.read_target_hits == 0


def test_zero_disables_the_guard() -> None:
    guard = ReactProgressGuard(threshold=99, read_target_limit=0)
    for i in range(20):
        verdict, _ = guard.check("file_read", {"path": "a.py", "offset": i * 10})
        assert verdict == "run", f"limit=0 时第 {i + 1} 次被拦"
    assert guard.read_target_hits == 0


def test_report_carries_read_target_accounting() -> None:
    guard = ReactProgressGuard(threshold=99)
    for i in range(7):
        guard.check("file_read", {"path": "a.py", "offset": i * 10})
    rep = guard.report()
    assert rep["read_target_limit"] == 8
    assert rep["read_target_hits"] == 2
    assert rep["read_target_blocked"] == 1
    assert rep["read_targets"]["path=a.py"] == 7


def test_reset_clears_read_targets() -> None:
    guard = ReactProgressGuard(threshold=99)
    for i in range(7):
        guard.check("file_read", {"path": "a.py", "offset": i})
    guard.reset()
    assert guard.report()["read_targets"] == {}
    assert guard.read_target_hits == 0
    assert guard.check("file_read", {"path": "a.py"})[0] == "run"


# ═══════════════════════════════════════════════════════════
# 2. 执行器接线：被拦后推 react_no_progress 事件
# ═══════════════════════════════════════════════════════════


def test_executor_blocks_repeated_reads_and_emits_event() -> None:
    reg = _Registry(["file_read"])
    seen: list[dict] = []

    async def on_np(ev: dict) -> None:
        seen.append(ev)

    async def _go() -> ReActExecutor:
        ex = ReActExecutor(_ScriptedLLM(_read_script(8)), reg,
                           max_iterations=12, repeat_threshold=99,
                           no_progress_limit=0)
        await ex.run("看看这个文件", on_no_progress=on_np)
        return ex

    ex = asyncio.run(_go())

    # 前 6 次真的执行了；第 7、8 次被读目标守卫拦下
    assert len(reg.calls) == 6, f"被拦截的动作仍然被执行了：{reg.calls}"
    assert ex.progress.read_target_hits >= 2
    assert ex.progress.blocked >= 2
    assert ex.progress.report()["read_targets"]["path=big.py"] == 8

    assert seen, "被拦后没有推 react_no_progress 事件"
    assert seen[0]["type"] == "react_no_progress"
    assert seen[0]["tool"] == "file_read"
    assert "同一目标" in seen[0]["reason"], seen[0]["reason"]
    assert seen[0]["read_target_hits"] >= 1


def test_executor_does_not_touch_existing_repeat_behaviour() -> None:
    """逐字重复仍走原判据（第 2 次提示、第 3 次拦截），读目标守卫不抢戏。"""
    reg = _Registry(["file_read"])
    script = [LLMResponse(text=f"再看 {i}",
                          tool_calls=[ToolCall(id=str(i), name="file_read",
                                               arguments={"path": "same.py"})])
              for i in range(5)]

    ex = ReActExecutor(_ScriptedLLM(script), reg, max_iterations=8,
                       repeat_threshold=2, no_progress_limit=0)
    asyncio.run(ex.run("读同一份"))

    rep = ex.progress.report()
    assert rep["guided"] == 1, "原有「先提示一次」的行为被改坏了"
    assert rep["blocked"] >= 2
    assert rep["read_target_hits"] == 0, "读目标守卫不该抢逐字重复的案子"
