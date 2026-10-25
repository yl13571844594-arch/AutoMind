"""后台任务双通道（``terminal`` 的 shell 通道 / ``pwsh`` 的 argv 通道）。

## 这个文件要钉住什么

1. ``background.start_argv`` 用 ``create_subprocess_exec`` 直启（不经 shell），
   命令能真的跑完、结果能经 ``terminal_background`` 取回；
2. 两类任务共享**同一张任务表**：``list`` 能同时看到 shell / pwsh 两种 kind，
   ``poll`` / ``output`` / ``kill`` 的语义完全一致（模型只学一个入口）；
3. ``pwsh(background=true)`` 的回执必须带 ``task_id`` 与"下一步怎么取回"的指引，
   且被配置关闭时明确拒绝（而不是偷偷前台跑或者假装成功）；
4. 被手动终止的任务状态是 ``killed``，不能被记成"命令失败"。

## 为什么整组 spawn 用例标 Windows-only

这些用例会在测试进程里养真实子进程；任务要求只在 Windows 上验证这条通道
（其它平台由 ``create_subprocess_exec`` 的标准行为覆盖，不再重复跑一遍）。
不 spawn 的 schema / 参数校验用例照常在所有平台跑。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from automind.tools import background as bg
from automind.tools.pwsh import PwshTool
from automind.tools.terminal import TerminalBackgroundTool

#: 真正拉起子进程的用例统一打这个标记
_WIN = pytest.mark.skipif(
    os.name != "nt", reason="后台子进程用例仅在 Windows 验证（其它平台 skip）")


@pytest.fixture(autouse=True)
def _clean_task_table():
    """每个用例前后清空任务表；用例自己负责把还在跑的任务 kill 掉。"""
    bg.reset_for_tests()
    yield
    bg.reset_for_tests()


async def _wait_finished(tool: TerminalBackgroundTool, task_id: str,
                         timeout: float = 20.0) -> object:
    """轮询到任务进入终态；超时如实失败（不返回一个看起来成功的假结果）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = await tool.execute(action="poll", task_id=task_id)
        if result.output["status"] != "running":
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"后台任务 {task_id} 在 {timeout:.0f}s 内没有结束")


# ═══════════════════════════════════════════════════════════
# 1. start_argv：不经 shell 的直启通道
# ═══════════════════════════════════════════════════════════


@_WIN
async def test_start_argv_runs_and_polls_to_ok():
    task = await bg.start_argv(
        [sys.executable, "-c", "print('bg-argv-ok')"],
        str(Path.cwd()), env=dict(os.environ), timeout=60)

    # 立即返回：进程已经 spawn 好（句柄从第一刻起可控，kill 才可能生效）
    assert task.task_id.startswith("bg")
    assert task.kind == "pwsh"
    assert task.meta.get("pid")
    try:
        result = await _wait_finished(TerminalBackgroundTool(), task.task_id)
    finally:
        await bg.kill(task.task_id)

    assert result.success is True
    assert result.output["status"] == "ok"
    assert result.output["kind"] == "pwsh"
    assert result.output["exit_code"] == 0
    assert "bg-argv-ok" in result.output["stdout_tail"]


@_WIN
async def test_output_action_returns_full_stdout():
    tool = TerminalBackgroundTool()
    task = await bg.start_argv(
        [sys.executable, "-c", "print('argv-full-output')"],
        str(Path.cwd()), env=dict(os.environ), timeout=60)
    try:
        await _wait_finished(tool, task.task_id)
        full = await tool.execute(action="output", task_id=task.task_id)
    finally:
        await bg.kill(task.task_id)

    assert full.success is True
    assert full.output["stdout"].strip() == "argv-full-output"


@_WIN
async def test_kill_running_argv_task_is_reported_as_killed():
    tool = TerminalBackgroundTool()
    task = await bg.start_argv(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        str(Path.cwd()), env=dict(os.environ), timeout=120)
    try:
        # 进程已在 spawn 时创建好，所以此刻必然是 running（不是"尚未启动"）
        first = await tool.execute(action="poll", task_id=task.task_id)
        assert first.output["status"] == "running"
        assert first.success is False, "仍在运行不能算成功"

        killed = await tool.execute(action="kill", task_id=task.task_id)
        assert killed.success is True
        assert killed.output["status"] == "killed"
    finally:
        await bg.kill(task.task_id)

    after = await tool.execute(action="poll", task_id=task.task_id)
    assert after.output["status"] == "killed"
    assert after.success is False
    assert "手动终止" in (after.error or "")
    # 被我们杀掉的进程退出码必然非零 —— 关键是不能因此被记成"命令失败"
    assert "退出码" not in (after.error or "")


@_WIN
async def test_list_contains_both_kinds_of_tasks():
    tool = TerminalBackgroundTool()
    argv_task = await bg.start_argv(
        [sys.executable, "-c", "print('listed')"],
        str(Path.cwd()), env=dict(os.environ), timeout=60)
    shell_task = await bg.start("echo shell-listed", str(Path.cwd()),
                                dict(os.environ), 60)
    try:
        listing = await tool.execute(action="list")
        ids = [item["task_id"] for item in listing.output["tasks"]]
        kinds = {item["task_id"]: item["kind"] for item in listing.output["tasks"]}
        assert argv_task.task_id in ids and shell_task.task_id in ids
        assert kinds[argv_task.task_id] == "pwsh"
        assert kinds[shell_task.task_id] == "shell"
    finally:
        await bg.kill(argv_task.task_id)
        await bg.kill(shell_task.task_id)


# ═══════════════════════════════════════════════════════════
# 2. pwsh 工具的后台参数
# ═══════════════════════════════════════════════════════════


def test_pwsh_schema_declares_background_switch():
    props = PwshTool.parameters["properties"]
    assert props["background"]["type"] == "boolean"
    assert PwshTool.parameters["required"] == ["command"]


@_WIN
async def test_pwsh_background_returns_task_id_and_polls_to_ok():
    if not PwshTool.available():
        pytest.skip("本机没有 pwsh/powershell")
    tool = PwshTool(workdir=str(Path.cwd()), timeout=60)
    started = await tool.execute(command='Write-Output "pwsh-bg-ok"',
                                 background=True)
    assert started.success is True, started.error
    assert started.output["background"] is True
    assert started.output["kind"] == "pwsh"
    assert started.output["status"] == "running"
    task_id = started.output["task_id"]
    assert task_id.startswith("bg")
    assert started.metadata["background_id"] == task_id
    assert "terminal_background" in started.output["note"], \
        "回执必须告诉模型下一步怎么取回"

    poller = TerminalBackgroundTool()
    try:
        result = await _wait_finished(poller, task_id, timeout=40)
    finally:
        await bg.kill(task_id)

    assert result.success is True
    assert result.output["status"] == "ok"
    assert result.output["kind"] == "pwsh"
    assert "pwsh-bg-ok" in result.output["stdout_tail"]


@_WIN
async def test_pwsh_background_disabled_is_an_explicit_refusal():
    if not PwshTool.available():
        pytest.skip("本机没有 pwsh/powershell")
    result = await PwshTool(workdir=str(Path.cwd()),
                            background_enabled=False).execute(
        command='Write-Output "should-not-run"', background=True)

    assert result.success is False
    assert "后台通道已在配置中关闭" in (result.error or "")
    assert "前台" in (result.error or "")
    assert bg.list_tasks() == [], "配置关闭时绝不能偷偷把任务放进后台"


# ═══════════════════════════════════════════════════════════
# 3. terminal_background 的参数校验（不 spawn，各平台都跑）
# ═══════════════════════════════════════════════════════════


async def test_terminal_background_rejects_unknown_action():
    result = await TerminalBackgroundTool().execute(action="wake",
                                                    task_id="bg1")

    assert result.success is False
    assert "未知的 action" in (result.error or "")
    assert "poll" in (result.error or "") and "kill" in (result.error or "")


async def test_terminal_background_unified_receipt_has_kind():
    """两类任务共用 snapshot：kind 字段必须存在（缺失会让模型分不清重跑方式）。"""
    task = bg.BackgroundTask(task_id="bg-x", command="echo hi",
                             workdir=str(Path.cwd()), started_at=0.0, kind="pwsh")
    snapshot = task.snapshot()
    assert snapshot["kind"] == "pwsh"
    assert snapshot["command"] == "echo hi"
