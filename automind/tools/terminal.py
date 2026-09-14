"""终端工具 — 异步安全 Shell 命令执行。"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

logger = get_logger("automind.tools.terminal")


class TerminalTool(AbstractTool):
    """安全终端命令执行工具。

    特性:
        - 异步执行，超时控制（默认时长与上限可配，见下）
        - 环境变量隔离
        - 工作目录控制
        - stdout/stderr 完整捕获
        - 后台通道：长耗时命令异步跑 + 轮询，不再"超时即失败"

    超时策略（v1.6.4 修正）：
        默认 120s 对 ``pip install`` / 编译 / 长测试偏短，超时后进程被杀、
        状态丢失、任务判败，模型只能**盲目重跑**（再下载一次、再编译一次）。
        现在：
          · 默认超时来自 ``ExecutionConfig.tool_timeout_seconds``（默认 300s）；
          · 模型可用 ``timeout`` 参数申请更长，上限
            ``ExecutionConfig.tool_timeout_max_seconds``（默认 1800s）；
          · 超时回执**明确携带"可加大 timeout 重试"的可执行指引**，让模型自愈；
          · 首轮默认值够不着的长命令，可用 ``background=True`` 走后台通道
            （配合 ``terminal_background`` 轮询），彻底摆脱"同步等死"。
        **只有前台同步执行才会被杀**；后台任务独立存活，超时不再等于白跑。
    """

    name = "terminal"
    description = (
        "Execute a shell command in a subprocess. "
        "Returns exit code, stdout, and stderr. "
        "Use for running CLI tools, scripts, package managers, etc. "
        "Long commands (pip install / build / full test suite) should use "
        "background=true and then poll with terminal_background. "
        "On timeout the result says how to retry with a larger timeout."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the command (optional).",
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Timeout in seconds. Default 300; raise it (up to 1800) for "
                    "installs/builds/long tests instead of retrying blindly."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "Run detached and return a task_id immediately (default false). "
                    "Poll with terminal_background(action='poll', task_id=...). "
                    "Use for commands that legitimately take minutes."
                ),
            },
        },
        "required": ["command"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 50

    # B-05 修复：命令注入防护。保留 shell 执行以支持管道/重定向/`&&` 等
    # 合法工作流，同时在工具边界硬性拒绝"永不合法"的灾难性命令（复用权限
    # 系统同一套 dangerous 语义），避免通过 `;`、`$(...)` 等串联出破坏性操作。
    _DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
        re.compile(p) for p in (
            r"rm\s+-rf?\s+[/~]",          # 删除根/家目录
            r"rm\s+-rf?\s+\*",            # 通配删除
            r":\(\)\s*\{\s*:\|:&\s*\};:",  # fork bomb
            r"\bmkfs\.",                    # 格式化
            r"\bdd\s+if=.*of=/dev/",       # 覆写块设备
            r">\s*/dev/sd",                # 写入磁盘设备
            r"\bsudo\b",                    # 提权
            r"\bchmod\s+-R\s+777\s+/",     # 递归放开根权限
            r"\bmv\s+[^|;&]*\s+/dev/null",  # 移入黑洞
        )
    ]

    def __init__(
        self,
        workdir: str | Path = ".",
        timeout: float = 300.0,
        env: dict[str, str] | None = None,
        block_dangerous: bool = True,
        max_timeout: float = 1800.0,
        background_enabled: bool = True,
    ) -> None:
        self.workdir = str(Path(workdir).resolve())
        self.timeout = timeout
        self.env = env or {}
        self.block_dangerous = block_dangerous
        #: 模型可通过 timeout 参数申请的上限（防止单条命令把并发槽占死）
        self.max_timeout = max(float(timeout), float(max_timeout))
        self.background_enabled = background_enabled
        self._last_result: ToolResult | None = None

    # ── 超时解释 ────────────────────────────────────────────

    def _effective_timeout(self, requested: Any) -> tuple[float, str]:
        """解析本次超时：返回 (实际秒数, 给模型的说明)。

        超上限时**不静默夹取** —— 静默夹取会让模型以为"我申请了 3600s"，
        实际 1800s 就被杀，然后又是一次莫名其妙的失败。
        """
        note = ""
        try:
            want = float(requested) if requested is not None else float(self.timeout)
        except (TypeError, ValueError):
            want = float(self.timeout)
            note = f"timeout 参数无法解析，已回退为默认 {self.timeout:.0f}s。"
        if want <= 0:
            want = float(self.timeout)
            note = f"timeout 必须为正数，已回退为默认 {self.timeout:.0f}s。"
        if want > self.max_timeout:
            note = (f"请求的 timeout={want:.0f}s 超过上限，已按上限 "
                    f"{self.max_timeout:.0f}s 执行。"
                    f"若命令确实需要更久，请改用 background=true 走后台通道。")
            want = self.max_timeout
        return want, note

    def _dangerous_reason(self, command: str) -> str | None:
        """命中灾难性命令模式时返回原因，否则 None。"""
        for pat in self._DANGEROUS_PATTERNS:
            if pat.search(command):
                return f"命令匹配高危模式 /{pat.pattern}/，已拒绝执行"
        return None

    def _timeout_guidance(self, command: str, timeout: float) -> str:
        """超时回执 —— 必须让模型能自愈，而不是只能盲目重跑。

        包含：实际等待时长、被杀的事实、加大 timeout 的具体写法，
        以及长耗时命令的正解（后台通道）。
        """
        from automind.tools.background import looks_long_running

        bigger = min(max(timeout * 2, timeout + 300), self.max_timeout)
        lines = [
            f"Command timed out after {timeout:.0f}s and was terminated: {command[:120]}",
            "该命令在被终止前没有返回结果，进程输出已丢失（不要原样重跑）。",
            f"【可自愈指引】若判断它只是需要更久：用同一个命令并显式加大 timeout 重试，"
            f"例如 timeout={bigger:.0f}（上限 {self.max_timeout:.0f}s）。",
        ]
        if self.background_enabled:
            lines.append(
                "【推荐】长耗时命令（安装/编译/完整测试/训练）改用后台通道："
                "terminal(command=..., background=true) 立即拿到 task_id，"
                "再用 terminal_background(action='poll', task_id=...) 取结果，"
                "其间可以继续做别的步骤 —— 就不会再被超时杀掉。")
        if looks_long_running(command):
            lines.append(
                "该命令看起来属于长耗时类别（安装/构建/测试），**直接改用后台通道**"
                "比加大 timeout 更稳：前台同步等待期间它占着执行槽且随时可能再被超时。")
        lines.append(
            "若重跑仍会超时，请拆分命令（例如先只装必需的依赖、把测试按文件分开跑），"
            "或先诊断为什么这么慢（网络/依赖体积/死循环）。")
        return "\n".join(lines)

    # ── 执行 ────────────────────────────────────────────────

    def _base_env(self) -> dict[str, str]:
        base_env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PIP_PROGRESS_BAR": "off",
            "GIT_TERMINAL_PROMPT": "0",
        }
        base_env.update(self.env)
        return base_env

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs["command"]
        workdir = kwargs.get("workdir", self.workdir)
        timeout, timeout_note = self._effective_timeout(kwargs.get("timeout"))
        want_background = bool(kwargs.get("background", False))

        # 注入/破坏性命令硬拦截（含通过 ; | && 串联的情形）
        if self.block_dangerous:
            reason = self._dangerous_reason(command)
            if reason:
                return ToolResult(
                    tool_name=self.name, success=False, error=reason, exit_code=-1,
                )

        base_env = self._base_env()

        # ── 后台通道 ──
        if want_background:
            if not self.background_enabled:
                return ToolResult(
                    tool_name=self.name, success=False, exit_code=-1,
                    error="后台通道已在配置中关闭（execution.terminal_background_enabled=false）。"
                          "请改用前台执行并显式设置足够大的 timeout。",
                )
            try:
                from automind.tools import background as bg

                task = await bg.start(str(command), str(workdir), base_env, timeout)
            except Exception as e:
                return ToolResult(
                    tool_name=self.name, success=False, exit_code=-1,
                    error=f"后台任务启动失败：{type(e).__name__}: {e}。"
                          f"可改用前台执行 + 更大 timeout。",
                )
            if task.status != "running":
                # 进程根本没起来（spawn 就失败）：如实报错，**不要**给一个
                # task_id 让模型去轮询一个不存在的东西
                return ToolResult(
                    tool_name=self.name, success=False, exit_code=-1,
                    error=f"后台命令启动失败：{task.error or '未知原因'}。"
                          f"可在前台同步执行并设置足够大的 timeout。",
                )
            return ToolResult(
                tool_name=self.name, success=True, exit_code=None,
                output={
                    "background": True,
                    "task_id": task.task_id,
                    "pid": task.meta.get("pid"),
                    "status": "running",
                    "timeout_s": timeout,
                    "note": (
                        "命令已在后台启动，**尚未结束**。请勿重复启动同一条命令；"
                        "稍后用 terminal_background(action='poll', task_id="
                        f"'{task.task_id}') 取结果，其间可以继续做别的步骤。"
                        + (f"（{timeout_note}）" if timeout_note else "")
                    ),
                },
                metadata={"background_id": task.task_id},
            )

        # ── 前台同步执行 ──
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=base_env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            out: dict[str, Any] = {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": process.returncode,
            }
            if timeout_note:
                out["timeout_note"] = timeout_note
            result = ToolResult(
                tool_name=self.name,
                success=process.returncode == 0,
                output=out,
                exit_code=process.returncode,
                error=stderr if process.returncode != 0 else None,
            )
            self._last_result = result
            return result

        except TimeoutError:
            # B-15 修复：超时后必须杀死子进程并回收，否则残留为僵尸进程。
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass  # 进程可能已自行结束
            logger.warning("terminal_timeout", command=str(command)[:120],
                           timeout_s=timeout)
            return ToolResult(
                tool_name=self.name,
                success=False,
                timed_out=True,
                exit_code=-1,
                error=self._timeout_guidance(str(command), timeout),
                output={"timed_out": True, "timeout_s": timeout,
                        "command": str(command)[:300]},
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(e),
                exit_code=-1,
            )

    def get_execution_plan(self, **kwargs: Any) -> str:
        cmd = kwargs.get("command", "")
        wd = kwargs.get("workdir", self.workdir)
        bg = " [background]" if kwargs.get("background") else ""
        return f"[terminal{bg}] Execute in {wd}:\n  $ {cmd}"

    @property
    def last_result(self) -> ToolResult | None:
        return self._last_result


class TerminalBackgroundTool(AbstractTool):
    """后台命令通道的查询/终止入口（配合 ``terminal(background=True)``）。

    没有这个工具，后台任务就是"启动了但取不回来" —— 模型只拿到一个 task_id
    却无从查询，只能重新同步跑一遍，等于白做。因此它与 terminal 是**成对**
    交付的：一个负责发起，一个负责取回。
    """

    name = "terminal_background"
    description = (
        "Manage background shell tasks started with terminal(background=true). "
        "action='poll' returns status and output tail for a task_id; "
        "'list' lists all tasks; 'output' returns the full captured output; "
        "'kill' terminates a task. Poll instead of re-running long commands."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "poll (default) | list | output | kill",
            },
            "task_id": {
                "type": "string",
                "description": "Task id returned by terminal(background=true).",
            },
            "tail_chars": {
                "type": "number",
                "description": "How many trailing characters of output to return (default 2000).",
            },
        },
        "required": [],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 5

    def __init__(self) -> None:
        self._last_result: ToolResult | None = None

    async def execute(self, **kwargs: Any) -> ToolResult:
        from automind.tools import background as bg

        action = str(kwargs.get("action") or "poll").lower()
        task_id = str(kwargs.get("task_id") or "")
        try:
            tail = int(kwargs.get("tail_chars") or 2000)
        except (TypeError, ValueError):
            tail = 2000
        tail = max(200, min(tail, 8000))

        if action == "list":
            tasks = bg.list_tasks()
            return ToolResult(
                tool_name=self.name, success=True,
                output={"count": len(tasks), "tasks": tasks},
            )

        if not task_id:
            return ToolResult(
                tool_name=self.name, success=False,
                error="缺少 task_id。请先 terminal(background=true) 启动命令，"
                      "或使用 action='list' 查看现有后台任务。",
            )

        task = bg.get(task_id)
        if task is None:
            return ToolResult(
                tool_name=self.name, success=False,
                error=f"后台任务不存在或已被清理：{task_id}。"
                      f"请用 action='list' 确认现有任务；若已丢失，需要重新执行该命令。",
            )

        if action == "kill":
            out = await bg.kill(task_id)
            return ToolResult(tool_name=self.name, success=bool(out.get("ok")),
                              output=out, error=None if out.get("ok") else out.get("error"))

        snap = task.snapshot(tail_chars=tail)
        if action == "output":
            snap["stdout"] = task.stdout
            snap["stderr"] = task.stderr
        # 成功的定义：任务已结束且退出码为 0。仍在 running 不算成功也不算失败
        # —— 让模型自己去 poll，而不是被一个"假的成功"骗过去。
        done = task.status != "running"
        ok = done and task.status == "ok"
        return ToolResult(
            tool_name=self.name, success=ok,
            output=snap,
            error=(None if (ok or not done)
                   else f"后台任务结束但未成功（status={task.status}）：{task.error}"),
        )

    def get_execution_plan(self, **kwargs: Any) -> str:
        return f"[terminal_background] {kwargs.get('action', 'poll')} {kwargs.get('task_id', '')}"

    @property
    def last_result(self) -> ToolResult | None:
        return self._last_result
