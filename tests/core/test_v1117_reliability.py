"""v1.11.7 工具调用与长任务稳定性：输出解码、Unix 命令换壳、文件锁重试、断网等待、步数续加。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from automind.core import netresilience as nr
from automind.core.types import ToolResult
from automind.tools import terminal as term


def test_decode_output_falls_back_to_gbk():
    assert term.decode_output("中文".encode("gbk")) == "中文"
    assert term.decode_output("中文".encode()) == "中文"
    assert term.decode_output(b"") == ""


@pytest.mark.parametrize(("cmd", "expect"), [
    ("mkdir -p a/b", "New-Item -ItemType Directory -Force -Path 'a/b' | Out-Null"),
    ("rm -rf build", "Remove-Item -Force -Recurse -Path 'build'"),
    ("cat a.txt", "Get-Content -Raw 'a.txt'"),
    ("head -n 5 log.txt", "Get-Content 'log.txt' -TotalCount 5"),
    ("which python", "(Get-Command 'python').Source"),
    ("pwd", "pwd"),
])
def test_unix_to_powershell(cmd, expect):
    assert term._to_powershell(cmd) == expect


@pytest.mark.parametrize("cmd", ["ls -la | grep x", "cd a && ls", "sed -i s/a/b/ f", "cp -r a b"])
def test_untranslatable_commands_left_alone(cmd):
    assert term._to_powershell(cmd) is None


def test_not_recognized_only_for_first_command():
    err_cn = "'ls' 不是内部或外部命令，也不是可运行的程序\n或批处理文件。"
    err_en = "'cat' is not recognized as an internal or external command,"
    assert term._not_recognized(err_cn, "ls")
    assert term._not_recognized(err_en, "cat")
    # 第二个命令不认识：第一个已经执行过了，不能整条重跑
    assert not term._not_recognized(err_cn, "echo")


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe 换壳只在 Windows 上发生")
def test_terminal_runs_unix_command_on_windows(tmp_path):
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    tool = term.TerminalTool(workdir=tmp_path)
    # 模拟 cmd.exe 报"不认识 ls"之后的换壳重跑（本机 PATH 上可能本来就有 ls.exe）
    r = asyncio.run(tool._posix_fallback("ls", "'ls' 不是内部或外部命令，也不是可运行的程序",
                                         str(tmp_path), tool._base_env(), 60))
    assert r is not None and r.success, r
    assert "hello.txt" in r.output["stdout"] and "shell_note" in r.output


# ── 文件锁重试 ─────────────────────────────────────────────────
#
# v1.11.9：锁重试从 core/netresilience 整体移到内置插件 fs_lock_guard
# （两边都在做时同一次写占用最坏执行 12 次、干等约 12 秒 —— 重试风暴）。
# 这里的用例改为验证"core 不再重试"；真正的重试行为见
# tests/core/test_builtin_delivery_chain.py::TestFsLockGuard。

_PLUGIN_FLG = Path(__file__).resolve().parents[2] / "automind" / "builtin_plugins" / "fs_lock_guard"


def _fs_lock_middleware():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_flg_test", _PLUGIN_FLG / "hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_middleware()


@pytest.fixture
def _no_sleep(monkeypatch):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr(nr.asyncio, "sleep", no_sleep)


def test_core_no_longer_retries_file_locks(_no_sleep):
    """core/netresilience 不再对文件锁重试（职责已移交 fs_lock_guard 插件）。"""
    n = {"i": 0}

    async def call_next(kw):
        n["i"] += 1
        return ToolResult(tool_name="file_write", success=False,
                          error="[WinError 32] 另一个程序正在使用此文件，进程无法访问。")
    res = asyncio.run(nr.ResilientDispatch()("file_write", {"path": "a.xlsx"}, call_next))
    assert not res.success and n["i"] == 1, "core 不该再重放文件锁失败"


def test_file_lock_retried_until_released(_no_sleep):
    n = {"i": 0}

    async def call_next(kw):
        n["i"] += 1
        if n["i"] < 3:
            return ToolResult(tool_name="file_write", success=False,
                              error="[WinError 32] 另一个程序正在使用此文件，进程无法访问。")
        return ToolResult(tool_name="file_write", success=True, output={"path": "a.xlsx"})
    mw = _fs_lock_middleware()
    res = asyncio.run(mw("file_write", {"path": "a.xlsx"}, call_next))
    assert res.success and n["i"] == 3


def test_file_lock_gives_up_with_advice(_no_sleep):
    async def call_next(kw):
        return ToolResult(tool_name="file_write", success=False,
                          error="PermissionError: [WinError 32] The process cannot access the file "
                                "because it is being used by another process")
    mw = _fs_lock_middleware()
    res = asyncio.run(mw("file_write", {"path": "a.xlsx"}, call_next))
    assert not res.success and "关闭打开该文件的程序" in res.error


def test_lock_retry_not_applied_to_terminal(_no_sleep):
    n = {"i": 0}

    async def call_next(kw):
        n["i"] += 1
        return ToolResult(tool_name="terminal", success=False, error="[WinError 32] busy")
    mw = _fs_lock_middleware()
    asyncio.run(mw("terminal", {"command": "python x.py"}, call_next))
    assert n["i"] == 1


# ── 长任务：断网时等网络回来 ─────────────────────────────────────


def _react(llm):
    from automind.planning.react_executor import ReActExecutor
    from automind.tools.base import ToolRegistry
    ex = ReActExecutor(llm=llm, tool_registry=ToolRegistry())
    ex.llm_reconnect_delays = (0.0,)
    return ex


def test_llm_waits_for_network_when_offline(monkeypatch):
    from automind.core.exceptions import LLMConnectionError
    events: list[dict[str, Any]] = []
    state = {"calls": 0}

    class LLM:
        async def generate(self, messages, tools=None):
            state["calls"] += 1
            if state["calls"] <= 2:
                raise LLMConnectionError("Connection error.")
            return "ok"

    async def up(timeout=3.0, *, fresh=False):
        return False, "全部不可达"

    async def wait(max_wait=60.0, interval=3.0):
        return True, 42.0
    monkeypatch.setattr(nr, "network_up", up)
    monkeypatch.setattr(nr, "wait_for_network", wait)

    async def sink(ev):
        events.append(ev)
    ex = _react(LLM())
    out = asyncio.run(ex._generate_resilient([], None, sink, None))
    assert out == "ok"
    assert any(e.get("state") == "offline" for e in events)


def test_llm_does_not_wait_when_network_is_up(monkeypatch):
    from automind.core.exceptions import LLMConnectionError

    class LLM:
        async def generate(self, messages, tools=None):
            raise LLMConnectionError("Connection error.")

    async def up(timeout=3.0, *, fresh=False):
        return True, "ok"
    monkeypatch.setattr(nr, "network_up", up)
    ex = _react(LLM())
    with pytest.raises(LLMConnectionError):
        asyncio.run(ex._generate_resilient([], None, None, None))


def test_no_iteration_extension_without_progress():
    from automind.planning.react_executor import ReActExecutor
    from automind.tools.base import ToolRegistry
    ex = ReActExecutor(llm=None, tool_registry=ToolRegistry())
    assert not ex._should_extend_iterations()          # 一次工具都没调过
    ex.actions = [object()]
    ex._rounds_without_success = 0
    assert ex._should_extend_iterations()
    ex._rounds_without_success = 3
    assert not ex._should_extend_iterations()
    ex._rounds_without_success = 0
    ex.iteration_extensions_used = ex.max_iteration_extensions
    assert not ex._should_extend_iterations()
