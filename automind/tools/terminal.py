"""终端工具 — 异步安全 Shell 命令执行。"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool


class TerminalTool(AbstractTool):
    """安全终端命令执行工具。

    特性:
        - 异步执行，超时控制
        - 环境变量隔离
        - 工作目录控制
        - stdout/stderr 完整捕获
    """

    name = "terminal"
    description = (
        "Execute a shell command in a subprocess. "
        "Returns exit code, stdout, and stderr. "
        "Use for running CLI tools, scripts, package managers, etc."
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
                "description": "Timeout in seconds (default: 120).",
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
        timeout: float = 120.0,
        env: dict[str, str] | None = None,
        block_dangerous: bool = True,
    ) -> None:
        self.workdir = str(Path(workdir).resolve())
        self.timeout = timeout
        self.env = env or {}
        self.block_dangerous = block_dangerous
        self._last_result: ToolResult | None = None

    def _dangerous_reason(self, command: str) -> str | None:
        """命中灾难性命令模式时返回原因，否则 None。"""
        for pat in self._DANGEROUS_PATTERNS:
            if pat.search(command):
                return f"命令匹配高危模式 /{pat.pattern}/，已拒绝执行"
        return None

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs["command"]
        workdir = kwargs.get("workdir", self.workdir)
        timeout = kwargs.get("timeout", self.timeout)

        # 注入/破坏性命令硬拦截（含通过 ; | && 串联的情形）
        if self.block_dangerous:
            reason = self._dangerous_reason(command)
            if reason:
                return ToolResult(
                    tool_name=self.name, success=False, error=reason, exit_code=-1,
                )

        base_env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PIP_PROGRESS_BAR": "off",
            "GIT_TERMINAL_PROMPT": "0",
        }
        base_env.update(self.env)

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

            result = ToolResult(
                tool_name=self.name,
                success=process.returncode == 0,
                output={
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": process.returncode,
                },
                exit_code=process.returncode,
                error=stderr if process.returncode != 0 else None,
            )
            self._last_result = result
            return result

        except asyncio.TimeoutError:
            # B-15 修复：超时后必须杀死子进程并回收，否则残留为僵尸进程。
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass  # 进程可能已自行结束
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Command timed out after {timeout}s: {command[:100]}",
                exit_code=-1,
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
        return f"[terminal] Execute in {wd}:\n  $ {cmd}"

    @property
    def last_result(self) -> ToolResult | None:
        return self._last_result
