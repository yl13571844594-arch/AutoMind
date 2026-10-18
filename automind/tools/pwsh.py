"""原生 PowerShell 工具（v1.11.11，仅 Windows 注册）。

为什么补它：现有 ``terminal`` 在 Windows 上默认是 ``cmd.exe``，只对常见的
Unix 写法做"换 Git Bash / 译成 PowerShell"兜底；模型写原生 PowerShell
（``$env:NAME``、``Get-ChildItem``、``Get-Process``）时，cmd 不认这些语法，
一次调用就白跑。这里给 Windows 一条"确定能按 PowerShell 语义执行"的通道。

边界：每次调用一个全新进程（不保留 cwd/变量，避免并发串状态）；超时会杀掉
整棵进程树；非零退出按失败返回（与 ``terminal`` 口径一致）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, run_blocking
from automind.tools.base import AbstractTool
from automind.tools.output_budget import limits_from_config
from automind.tools.terminal import decode_output

logger = get_logger("automind.tool.pwsh")

#: 单次输出最多返回多少字符（复用工具输出体积治理配置，防止一条命令撑爆上下文）
_MAX_CHARS_FALLBACK = 12000


class PwshTool(AbstractTool):
    """在 PowerShell（pwsh/powershell）里执行一条命令。"""

    name = "pwsh"
    description = (
        "Run a PowerShell command on Windows and return exit code, stdout, stderr. "
        "Use native PowerShell syntax ($env:NAME, Get-ChildItem, ...) — this tool "
        "does NOT translate bash syntax. Each call uses a fresh process; pass "
        "workdir instead of cd. Raise timeout (up to 1800s) for installs/builds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "PowerShell command to execute."},
            "workdir": {"type": "string",
                        "description": "Working directory (optional; relative to project root)."},
            "timeout": {"type": "number",
                        "description": "Timeout in seconds (default 300, max 1800)."},
        },
        "required": ["command"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 50

    def __init__(self, workdir: str | Path = ".", timeout: float = 300.0,
                 max_timeout: float = 1800.0) -> None:
        self.workdir = str(Path(workdir).resolve())
        self.timeout = max(5.0, float(timeout or 300.0))
        self.max_timeout = max(self.timeout, float(max_timeout or 1800.0))
        try:
            limits = limits_from_config()
            self.max_chars = int(limits.get("max_chars", _MAX_CHARS_FALLBACK)
                                 or _MAX_CHARS_FALLBACK)
        except Exception:                     # pragma: no cover - 防御性
            self.max_chars = _MAX_CHARS_FALLBACK

    @staticmethod
    def available() -> bool:
        return os.name == "nt" and bool(
            shutil.which("pwsh") or shutil.which("powershell"))

    @staticmethod
    def _exe() -> str:
        return shutil.which("pwsh") or shutil.which("powershell") or ""

    def _workdir(self, raw: Any) -> tuple[str, str]:
        text = str(raw or "").strip()
        if not text:
            return self.workdir, ""
        p = Path(text).expanduser()
        if not p.is_absolute():
            p = Path(self.workdir) / p
        try:
            resolved = p.resolve()
        except OSError:
            return "", f"工作目录无法解析：{text}"
        if not resolved.is_dir():
            return "", f"工作目录不存在或不是目录：{resolved}"
        return str(resolved), ""

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = str(kwargs.get("command") or "").strip()
        if not command:
            return bad(self.name, "command 不能为空")
        exe = self._exe()
        if not exe:
            return bad(self.name,
                       "未找到 PowerShell 可执行文件（pwsh/powershell）。"
                       "Windows 10+ 自带 powershell.exe；若被裁剪，请安装 PowerShell 7")
        # 模型偶尔把整条命令包进 Markdown 代码围栏；先剥掉再执行
        if command.startswith("```"):
            import re as _re
            command = _re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", command)
            command = _re.sub(r"\s*```$", "", command).strip()
        workdir, problem = self._workdir(kwargs.get("workdir"))
        if problem:
            return bad(self.name, problem)
        try:
            timeout = float(kwargs.get("timeout") or self.timeout)
        except (TypeError, ValueError):
            timeout = self.timeout
        timeout = min(max(timeout, 1.0), self.max_timeout)

        argv = [exe, "-NoProfile", "-NonInteractive", "-Command", command]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout)
            except TimeoutError:
                from automind.tools.terminal import TerminalTool

                if proc.returncode is None:
                    await run_blocking(TerminalTool._kill_tree, proc)
                with_suppress = asyncio.wait_for(proc.wait(), timeout=5.0)
                try:
                    await with_suppress
                except (TimeoutError, ProcessLookupError, OSError):
                    pass
                return bad(self.name,
                           f"命令超过 {timeout:.0f} 秒仍未结束，已终止。"
                           "安装/编译/长测试请加大 timeout（上限 1800 秒），"
                           "或拆成更小的命令分段执行。",
                           timed_out=True)
        except FileNotFoundError:
            return bad(self.name, f"PowerShell 启动失败：找不到 {exe}")
        except Exception as e:
            if proc is not None and proc.returncode is None:
                from automind.tools.terminal import TerminalTool

                await run_blocking(TerminalTool._kill_tree, proc)
            return bad(self.name, f"命令执行失败：{type(e).__name__}: {e}")

        stdout = decode_output(stdout_b) if stdout_b else ""
        stderr = decode_output(stderr_b) if stderr_b else ""
        combined = stdout
        if stderr:
            combined = (combined + "\n[stderr]\n" + stderr).strip()
        if len(combined) > self.max_chars:
            keep = max(200, self.max_chars)
            combined = combined[-keep:]
            combined = ("…（输出过长，仅保留尾部；如需完整输出请把命令结果写入文件后"
                        "用 file_read 分段读取）\n" + combined)
        code = proc.returncode if proc is not None else -1
        return ToolResult(
            tool_name=self.name, success=(code == 0),
            output={"command": command, "stdout": combined, "workdir": workdir},
            error=None if code == 0 else f"命令退出码 {code}",
            exit_code=code,
        )

    def get_execution_plan(self, **kwargs: Any) -> str:
        wd = kwargs.get("workdir") or self.workdir
        return f"[pwsh] {wd}\n  PS> {kwargs.get('command', '')}"
