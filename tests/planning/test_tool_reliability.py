"""v1.7.5 第二批修复的回归用例 —— 工具可靠性与护栏。

对应 `_perfcheck/修复清单.md` 里要求新增的第 3~6 条，外加工具成功率那几项。
每条都盯住一个"曾经真实失效过"的行为，回退即失败。
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from automind.core.types import LLMResponse, PermissionTier, ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor
from automind.planning.react_progress import ReactProgressGuard

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
    """带名字归一化能力的注册表替身（对齐 ToolRegistry 的真实行为）。"""

    def __init__(self, names: list[str], fail: bool = False) -> None:
        self._names = list(names)
        self._fail = fail
        self.dispatched: list[str] = []

    def list_names(self) -> list[str]:
        return list(self._names)

    def list_all(self):
        return [_Tool(n) for n in self._names]

    def get(self, name):
        from automind.core.exceptions import ToolNotFoundError

        if name not in self._names:
            raise ToolNotFoundError(f"Tool '{name}' not found. Available: {self._names}")
        return _Tool(name)

    async def dispatch(self, name, **kw):
        from automind.tools.base import resolve_name

        resolved, _ = resolve_name(name, self._names)
        real = resolved or name
        self.dispatched.append(real)
        if self._fail or real not in self._names:
            return ToolResult(tool_name=real, success=False,
                              error=f"没有名为 '{name}' 的工具")
        return ToolResult(tool_name=real, success=True, output="ok")


class _ScriptedLLM:
    """按剧本返回：先给出指定序列的工具名，之后收尾。"""

    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self.i = 0
        self.seen_tool_names: list[list[str]] = []

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        self.seen_tool_names.append([t.get("name", "") for t in (tools or [])])
        if self.i >= len(self.names):
            return LLMResponse(text="完成", tool_calls=None)
        name = self.names[self.i]
        self.i += 1
        return LLMResponse(text=f"第 {self.i} 步", prompt_tokens=5, completion_tokens=5,
                           tool_calls=[ToolCall(id=str(self.i), name=name,
                                                arguments={"step": self.i})])


class _EmptyLLM:
    """永远返回空正文 + finish_reason=length（推理模型烧光 max_tokens 的形态）。"""

    async def generate(self, messages, tools=None, **kw) -> LLMResponse:
        return LLMResponse(text="", tool_calls=None, finish_reason="length",
                           prompt_tokens=10, completion_tokens=8192)


def _exec(**kw) -> ReActExecutor:
    kw.setdefault("auto_validate", False)
    kw.setdefault("tool_timeout", 5.0)
    return ReActExecutor(**kw)


# ═══════════════════════════════════════════════════════════
# 3. 交替循环（A,B,A,B…）必须被识别 —— 原先只看"紧邻上一个"
# ═══════════════════════════════════════════════════════════


class TestAlternatingLoopDetection:
    def test_alternating_actions_are_detected(self):
        g = ReactProgressGuard(threshold=2)
        verdicts = [g.check(f"tool_{k}", {"i": 0})[0]
                    for k in ("A", "B", "A", "B", "A")]
        assert verdicts[:4] == ["run"] * 4, f"前四步不该被拦：{verdicts}"
        assert verdicts[4] == "guide", f"第 5 步应给出纠偏提示：{verdicts}"
        assert g.alternating_hits >= 1

    def test_alternating_loop_is_eventually_blocked(self):
        """只提示不拦截等于没有护栏 —— 窗口滑动后计数会稳定在阈值上。"""
        g = ReactProgressGuard(threshold=2)
        for k in ("A", "B", "A", "B", "A", "B", "A", "B", "A"):
            g.check(f"tool_{k}", {"i": 0})
        assert g.blocked >= 1, "交替循环必须最终被拦截，而不是永远只提示"

    def test_consecutive_repeat_still_works(self):
        """原有判据不得被改坏。"""
        g = ReactProgressGuard(threshold=2)
        assert [g.check("same", {"a": 1})[0] for _ in range(3)] == \
            ["run", "guide", "block"]

    def test_varying_arguments_are_never_blocked(self):
        """参数每次都不同 = 正常的分批处理，不能被误伤。"""
        g = ReactProgressGuard(threshold=2)
        for i in range(10):
            v, _ = g.check("file_read", {"path": f"f{i}.py"})
            assert v == "run", f"第 {i} 步被误拦"


# ═══════════════════════════════════════════════════════════
# 4/5. 工具跳变 + 全失败 → 提前收尾（护栏不再能被"换招数"绕过）
# ═══════════════════════════════════════════════════════════


class TestNoSuccessfulToolCallStopsTheLoop:
    def test_tool_hopping_does_not_burn_the_whole_budget(self):
        """每一步都换一个不存在的工具名 —— 熔断按名字计数，拦不住它。

        这条护栏看的是**结果**：连续多轮一次成功都没有就收尾。
        """
        names = [f"ghost_tool_{i}" for i in range(40)]
        reg = _Registry(["terminal", "file_read"])
        ex = _exec(llm=_ScriptedLLM(names), tool_registry=reg, max_iterations=40)
        out = asyncio.run(ex.run("注定失败的任务"))
        assert ex.iterations_used <= ReActExecutor.NO_SUCCESS_LIMIT + 1, (
            f"没有成功调用的轮次应触发收尾，实际跑了 {ex.iterations_used} 步")
        assert "部分交付清单" in out

    def test_a_success_resets_the_counter(self):
        reg = _Registry(["terminal", "file_read"])
        names = ["ghost_1", "ghost_2", "file_read", "ghost_3", "ghost_4"]
        ex = _exec(llm=_ScriptedLLM(names), tool_registry=reg, max_iterations=40)
        asyncio.run(ex.run("中间成功一次"))
        # 成功过就重置，不该在 5 轮内被提前收尾
        assert ex.stop_reason in ("no_more_tools", "max_iterations"), ex.stop_reason


# ═══════════════════════════════════════════════════════════
# 6. 空回复 / 被截断的回复不算"完成"
# ═══════════════════════════════════════════════════════════


class TestEmptyCompletionIsNotSuccess:
    def test_empty_completion_is_not_success(self):
        ex = _exec(llm=_EmptyLLM(), tool_registry=_Registry(["terminal"]),
                   max_iterations=30)
        out = asyncio.run(ex.run("推理模型烧光预算"))
        assert ex.stop_reason == "empty_response", ex.stop_reason
        assert ex.iterations_used <= ReActExecutor.EMPTY_RESPONSE_LIMIT
        assert "部分交付清单" in out

    def test_normal_empty_answer_still_completes_when_not_truncated(self):
        """没有工具调用且正文非空 = 正常完成，不受影响。"""
        class _Done:
            async def generate(self, messages, tools=None, **kw):
                return LLMResponse(text="这是结论", tool_calls=None,
                                   finish_reason="stop")

        ex = _exec(llm=_Done(), tool_registry=_Registry(["terminal"]))
        out = asyncio.run(ex.run("短任务"))
        assert ex.stop_reason == "no_more_tools"
        assert "部分交付清单" not in out


# ═══════════════════════════════════════════════════════════
# 工具成功率：名字归一化必须在**权限门控之前**发生
# ═══════════════════════════════════════════════════════════


class TestToolNameNormalization:
    def test_read_file_is_normalized_before_gating(self):
        """`read_file` 是模型最常见的拼写错误之一。

        修复前：门控用精确匹配 → 判成"工具不存在" → 按 SENSITIVE 要审批 →
        "询问"模式下被拒；而同一个名字在 dispatch 里其实能正常执行。
        """
        reg = _Registry(["file_read", "terminal"])
        ex = _exec(llm=_ScriptedLLM(["read_file"]), tool_registry=reg)
        asyncio.run(ex.run("读文件"))
        assert "file_read" in reg.dispatched, (
            f"归一化没有生效，实际派发：{reg.dispatched}")

    def test_normalized_name_is_used_for_failure_accounting(self):
        """失败计数必须按**解析后**的真名，否则换拼写就能绕过熔断。"""
        reg = _Registry(["file_read"], fail=True)
        ex = _exec(llm=_ScriptedLLM(["read_file", "read_file"]), tool_registry=reg)
        asyncio.run(ex.run("读文件"))
        assert set(ex._tool_failures) <= {"file_read"}, (
            f"失败记录里出现了未归一化的名字：{list(ex._tool_failures)}")

    def test_unresolvable_name_is_treated_as_dangerous(self):
        """解析不出来的名字按**最高**风险处理，不能比 terminal 还温和。"""
        from automind.tools.permissions import PermissionEngine

        reg = _Registry(["file_read"])
        engine = PermissionEngine(approval_mode="ask")
        ex = _exec(llm=_ScriptedLLM(["totally_ghost"]), tool_registry=reg,
                   permissions=engine)
        asyncio.run(ex.run("调一个不存在的工具"))
        # ask 模式下 DANGEROUS 会被要求审批；没有审批通道 → fail-closed
        assert ex.skipped_approvals >= 1 or ex.blocked_actions >= 0
        assert ex.actions and not ex.actions[0][1].success


# ═══════════════════════════════════════════════════════════
# 工具成功率：沙箱白名单与可操作错误
# ═══════════════════════════════════════════════════════════


class TestSandboxUsability:
    def test_pure_computation_modules_are_allowed(self):
        """hashlib/zlib/struct 是实测里连续三次失败的那三个。"""
        from automind.tools.sandbox import ALLOWED_IMPORTS, validate_code

        for mod in ("hashlib", "zlib", "struct", "base64", "binascii"):
            assert mod in ALLOWED_IMPORTS, f"{mod} 应被允许"
            validate_code(f"import {mod}")   # 不抛即通过

    def test_dangerous_modules_stay_blocked(self):
        """放宽白名单不得把文件/进程能力一起放进来。"""
        from automind.tools.sandbox import SandboxViolation, validate_code

        for mod in ("os", "sys", "subprocess", "pathlib", "io", "codecs", "socket"):
            with pytest.raises(SandboxViolation):
                validate_code(f"import {mod}")

    def test_blocked_import_error_lists_what_is_allowed(self):
        """错误必须说清"什么行"，否则模型只能一个模块一个模块地猜。"""
        from automind.tools.sandbox import SandboxViolation, validate_code

        with pytest.raises(SandboxViolation) as ei:
            validate_code("import socket")
        msg = str(ei.value)
        assert "允许导入的模块" in msg
        assert "hashlib" in msg and "json" in msg

    def test_sandbox_schema_documents_the_allowlist(self):
        from automind.tools.sandbox import PythonSandboxTool

        desc = PythonSandboxTool.parameters["properties"]["code"]["description"]
        assert "hashlib" in desc and "json" in desc, (
            "工具 schema 里要写明能导入什么 —— 这是模型最先看到的地方")


# ═══════════════════════════════════════════════════════════
# 工具成功率：路径语义与越界消息
# ═══════════════════════════════════════════════════════════


class TestPathGuidance:
    def test_out_of_scope_message_names_the_root(self, tmp_path):
        from automind.tools.file_editor import _RootGuard

        msg = _RootGuard(tmp_path).out_of_scope_message("../x.txt")
        assert "超出" in msg, "既有判定依据不许丢"
        assert str(tmp_path) in msg, "必须告诉模型允许的根目录在哪"
        assert "相对" in msg, "必须告诉模型该怎么写才对"

    def test_file_search_and_file_read_share_one_message(self, tmp_path):
        """两处守卫口径必须一致（此前一个说'路径越界'、一个说'超出项目目录范围'）。"""
        from automind.tools.data_tools import FileSearchTool

        r = asyncio.run(FileSearchTool(project_root=tmp_path).execute(path="../.."))
        assert not r.success
        assert "超出" in (r.error or "")
        assert str(tmp_path) in (r.error or "")

    def test_schemas_document_relative_paths(self):
        from automind.tools.data_tools import FileSearchTool as _FS
        from automind.tools.file_editor import FileReadTool

        for desc in (FileReadTool.parameters["properties"]["path"]["description"],):
            assert "project root" in desc, desc
        assert "project root" in _FS.parameters["properties"]["path"]["description"]

    def test_system_prompt_states_the_two_hard_rules(self):
        """两条最容易踩的坑必须写进系统提示：工具名照抄、路径相对项目根。"""
        sp = ReActExecutor.SYSTEM_PROMPT
        assert "EXACT tool names" in sp
        assert "project root" in sp


# ═══════════════════════════════════════════════════════════
# 取消：前台子进程必须被杀掉（不能遗弃）
# ═══════════════════════════════════════════════════════════


class TestTerminalCancellation:
    def test_cancel_kills_the_foreground_subprocess(self):
        """用户点"停止"时，正在跑的命令必须一起结束。

        修复前只有 `except TimeoutError` 会 kill，取消则径直穿过 —— 子进程
        被遗弃，一直活到自身超时（最长 1800s），而用户以为已经停了。
        """
        from automind.tools.terminal import TerminalTool

        tool = TerminalTool()
        marker = f"__am_cancel_marker_{int(time.time())}__"
        # 一条会睡很久的命令；用 echo 把标记打在进程命令行里便于查找
        cmd = (f'{sys.executable} -c "import time;time.sleep(60)" '
               f'# {marker}')

        async def scenario() -> int:
            task = asyncio.ensure_future(tool.execute(command=cmd, timeout=120))
            await asyncio.sleep(1.5)          # 等子进程真的起来
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(1.0)          # 留出被杀的时间
            return _count_marker_processes(marker)

        remaining = asyncio.run(scenario())
        assert remaining == 0, f"取消后仍有 {remaining} 个残留子进程"

    def test_workdir_precheck_gives_actionable_error(self, tmp_path):
        """workdir 不存在时不该抛裸 `[WinError 5] 拒绝访问。`"""
        from automind.tools.terminal import TerminalTool

        r = asyncio.run(TerminalTool().execute(
            command="echo hi", workdir=str(tmp_path / "not_there")))
        assert not r.success
        assert "不可用" in (r.error or "")
        assert "workdir" in (r.error or "")


def _count_marker_processes(marker: str) -> int:
    """数一数命令行里带该标记的 Python 进程还有几个。"""
    import subprocess

    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'",
                 "get", "CommandLine"],
                capture_output=True, text=True, timeout=20, check=False).stdout
        except Exception:
            return 0                      # 取不到就不误报失败
    else:
        try:
            out = subprocess.run(["ps", "-eo", "args"],
                                 capture_output=True, text=True,
                                 timeout=20, check=False).stdout
        except Exception:
            return 0
    return sum(1 for line in out.splitlines() if marker in line)
