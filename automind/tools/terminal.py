"""终端工具 — 异步安全 Shell 命令执行。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

logger = get_logger("automind.tools.terminal")


def decode_output(data: bytes) -> str:
    """子进程输出解码（v1.11.7）：先按 UTF-8，失败再按系统代码页（中文 Windows = GBK）。

    此前一律 ``utf-8 + replace``：中文 Windows 上 ``dir``、``ping``、``pip`` 的报错是 GBK，
    解出来满屏 �，模型读不懂报错就只能瞎猜着重试。
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    import locale
    for enc in (locale.getpreferredencoding(False), "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


_NOT_RECOGNIZED = re.compile(
    r"'(?P<cmd>[^']+)' (?:不是内部或外部命令|is not recognized as an internal or external command)")


def _first_token(command: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9_.\-]+)", command or "")
    return m.group(1) if m else ""


def _not_recognized(stderr: str, first: str) -> bool:
    m = _NOT_RECOGNIZED.search(stderr or "")
    return bool(m) and m.group("cmd").strip().lower() == first.lower()


def _unknown_commands(stderr: str) -> list[str]:
    """cmd.exe 报出的所有"不认识"命令（可能是链式命令里的第 2、3 段）。"""
    return [m.group("cmd").strip() for m in _NOT_RECOGNIZED.finditer(stderr or "")]


#: 出现在失败命令**之前**的分段若是这些只读命令，自动换壳重跑是安全的
_SAFE_PREFIX = {"cd", "echo", "pwd", "dir", "ls", "type", "cat", "find", "grep",
                "head", "tail", "where", "which", "set", "ver", "chcp"}
_PS_MARKERS = ("get-", "set-", "select-", "$env:", "invoke-", "write-output",
               "-command", "foreach-", "where-object", "get-childitem")


def _segments(command: str) -> list[str]:
    parts = re.split(r"\s*(?:&&|\|\||[;|&])\s*", str(command or ""))
    return [p.strip() for p in parts if p.strip()]


def _safe_prefix_before(command: str, token: str) -> bool:
    """失败 token 之前的分段是否都是只读命令（决定能否自动重跑）。"""
    segments = _segments(command)
    for seg in segments:
        first = _first_token(seg).lower()
        if first == token.lower():
            return True
        if first not in _SAFE_PREFIX:
            return False
    return True


#: cmd.exe 下可原位替换的 Unix 命令（模型混写 bash/cmd 时最常见）
_CMD_EQUIV = {
    "ls": "dir", "cat": "type", "cp": "copy", "mv": "move",
    "pwd": "cd", "which": "where", "clear": "cls",
}


def _repair_cmd_segment(segment: str, token: str) -> str:
    """把一段 Unix 命令替换成 cmd.exe 等价写法；无法安全替换返回空串。"""
    name = _first_token(segment).lower()
    if name != token.lower():
        return ""
    rest = segment.strip()[len(name):].strip()
    if name in ("rm", "rmdir") or rest.startswith("-") or any(c in rest for c in "`$"):
        return ""                       # 删除类/带 Unix 选项/含展开：交给 Git Bash，不硬译
    if name == "mkdir":
        rest = re.sub(r"^-p\s+", "", rest)
        return f"mkdir {rest}".strip()
    if name == "touch":
        return f"type nul > {rest}".strip()
    if name == "grep" and rest:
        return f"findstr {rest}"
    equiv = _CMD_EQUIV.get(name)
    return f"{equiv} {rest}".strip() if equiv else ""


def _repair_cmd_command(command: str, token: str) -> str:
    """只替换**失败的那一段**，其余原样保留，再交给 cmd 执行一次。"""
    parts = re.split(r"(\s*(?:&&|\|\||[;|&])\s*)", str(command or ""))
    for i in range(0, len(parts), 2):
        repaired = _repair_cmd_segment(parts[i], token)
        if repaired:
            parts[i] = repaired
            return "".join(parts)
    return ""


def _git_bash() -> str:
    """Git for Windows 自带的 bash（排除 System32 下的 WSL 启动器：那是另一套文件系统）。"""
    import shutil
    cands = [shutil.which("bash") or ""]
    for var in ("PROGRAMFILES", "PROGRAMW6432", "LOCALAPPDATA"):
        base = os.environ.get(var, "")
        if base:
            cands += [str(Path(base) / "Git" / "bin" / "bash.exe"),
                      str(Path(base) / "Programs" / "Git" / "bin" / "bash.exe")]
    for c in cands:
        if c and Path(c).is_file() and "system32" not in c.lower() and "windowsapps" not in c.lower():
            return c
    return ""


#: 没有 Git Bash 时，能直接交给 PowerShell 的 Unix 写法（PowerShell 有同名别名或等价命令）
_PS_SIMPLE = {"ls", "cat", "pwd", "cp", "mv", "rm", "mkdir", "echo", "head", "tail", "which",
              "touch", "grep", "clear", "date", "whoami"}


def _to_powershell(command: str) -> str | None:
    """把常见的单条 Unix 命令译成 PowerShell；译不了（管道 / 复杂参数）返回 None。"""
    cmd = command.strip()
    if any(op in cmd for op in ("|", "&&", "||", ";", "`", "$(")):
        return None
    parts = cmd.split()
    if not parts or parts[0] not in _PS_SIMPLE:
        return None
    name, args = parts[0], parts[1:]
    flags = [a for a in args if a.startswith("-")]
    paths = [a for a in args if not a.startswith("-")]

    def q(p: str) -> str:
        return "'" + p.strip("'\"").replace("'", "''") + "'"
    if name == "mkdir":
        return "; ".join(f"New-Item -ItemType Directory -Force -Path {q(p)} | Out-Null" for p in paths) or None
    if name == "rm" and paths:
        rec = any("r" in f.lower() for f in flags)
        return "; ".join(f"Remove-Item -Force{' -Recurse' if rec else ''} -Path {q(p)}" for p in paths)
    if name == "touch" and paths:
        return "; ".join(f"if (Test-Path {q(p)}) {{ (Get-Item {q(p)}).LastWriteTime = Get-Date }} "
                         f"else {{ New-Item -ItemType File -Path {q(p)} | Out-Null }}" for p in paths)
    if name == "ls":
        return "Get-ChildItem -Force " + " ".join(q(p) for p in paths)
    if name == "which" and paths:
        return f"(Get-Command {q(paths[0])}).Source"
    if name == "cat" and paths and not flags:
        return "Get-Content -Raw " + ", ".join(q(p) for p in paths)
    if name in ("head", "tail") and paths:
        n = 10
        for i, a in enumerate(args):
            if a == "-n" and i + 1 < len(args) and args[i + 1].isdigit():
                n = int(args[i + 1])
            elif re.fullmatch(r"-\d+", a):
                n = int(a[1:])
        files = [p for p in paths if not p.isdigit()]
        if not files:
            return None
        return f"Get-Content {q(files[-1])} -{'TotalCount' if name == 'head' else 'Tail'} {n}"
    if name == "grep" and len(paths) >= 2 and not flags:
        return f"Select-String -Pattern {q(paths[0])} -Path " + ", ".join(q(p) for p in paths[1:])
    if flags:
        return None
    return cmd                      # cp / mv / pwd / echo / date 等：PowerShell 别名可直接用


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
                "description": "Working directory for the command (optional). Must already exist; omit it to run in the project root.",
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

    async def _posix_fallback(self, command: str, stderr: str, workdir: str,
                              env: dict[str, str], timeout: float) -> ToolResult | None:
        """Windows 上 cmd.exe 不认识 Unix/PowerShell 写法时自动换壳重跑（v1.11.7 / v1.11.13）。

        模型写的命令混合了两种生态：``ls``/``cat``/``grep``（bash）与
        ``Get-ChildItem``/``$env:``（PowerShell）。cmd.exe 一句"不是内部或外部
        命令"就是一次白跑，模型再换花样又是几次 —— 熔断往往就是这么攒满的。

        v1.11.13：不再只看**第一个**命令。链式命令 ``cd x && echo ... && ls``
        的失败点在第三段，旧逻辑完全不会触发重跑。现在只要 cmd 报出"不认识"，
        且失败点之前的段都是只读命令（cd/echo/dir/ls/...），就安全地用
        Git Bash / PowerShell 重跑整条命令 —— 这些前缀重复执行没有副作用。
        若失败点前出现过写操作（rm/mv/pip/...），不做自动重跑，返回明确指引，
        避免"执行一半再重跑"造成重复副作用。
        """
        unknown = _unknown_commands(stderr)
        if not unknown:
            return None
        token = unknown[0]
        if not _safe_prefix_before(command, token):
            return None

        # 1) 先试"只替换失败段"的 cmd 等价写法（保留 cd/dir 等 cmd 语义，
        #    对 cd /d ... && ls 这类混合链最有效）。
        repaired = _repair_cmd_command(command, token)
        if repaired and repaired != command:
            proc: Any = None
            try:
                proc = await asyncio.create_subprocess_shell(
                    repaired, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=workdir, env=env)
                so, se = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except (TimeoutError, OSError):
                await self._kill_quietly(proc)
            else:
                ro = {"stdout": decode_output(so).strip(),
                      "stderr": decode_output(se).strip(),
                      "exit_code": proc.returncode,
                      "shell_note": f"cmd.exe 不认识 {token}，已替换为 {_first_token(repaired)} 等价写法"}
                logger.info("terminal_cmd_repair", command=command[:120],
                            repaired=repaired[:120], exit_code=proc.returncode)
                return ToolResult(tool_name=self.name, success=proc.returncode == 0,
                                  output=ro, exit_code=proc.returncode,
                                  error=(ro["stderr"] or None) if proc.returncode != 0 else None,
                                  metadata={"shell_repair": repaired})

        bash = _git_bash()
        argv: list[str] | None = None
        how = ""
        cmd_low = command.lower()
        if any(marker in cmd_low for marker in _PS_MARKERS):
            # PowerShell 语义的命令：优先用系统 PowerShell（bash 不理解 $env:）
            import shutil

            ps = shutil.which("pwsh") or shutil.which("powershell")
            if ps:
                argv, how = [ps, "-NoProfile", "-NonInteractive", "-Command", command], "PowerShell"
        if argv is None and bash:
            argv, how = [bash, "-c", command], "Git Bash"
        if argv is None:
            ps_cmd = _to_powershell(command)
            if ps_cmd is not None:
                argv, how = (["powershell", "-NoProfile", "-NonInteractive",
                              "-Command", ps_cmd], "PowerShell")
        if argv is None:
            return None
        process: Any = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=workdir, env=env)
            so, se = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (TimeoutError, OSError):
            await self._kill_quietly(process)
            return None
        out = {"stdout": decode_output(so).strip(), "stderr": decode_output(se).strip(),
               "exit_code": process.returncode,
               "shell_note": f"cmd.exe 不认识 {token}，已自动改用 {how} 执行同一条命令"}
        logger.info("terminal_posix_fallback", shell=how, command=command[:120],
                    exit_code=process.returncode)
        return ToolResult(tool_name=self.name, success=process.returncode == 0, output=out,
                          exit_code=process.returncode,
                          error=(out["stderr"] or None) if process.returncode != 0 else None,
                          metadata={"shell_fallback": how})

    def _session_workdir(self, requested: Any) -> str:
        """命令的工作目录。会话开了目录级隔离时（v1.11.6）落在副本里。

        此前 terminal 一律在项目根执行：隔离模式下 ``npm run build`` / ``python gen.py``
        的产物直接写进项目，既绕过隔离、也不在副本快照里 ——「应用到项目」时就少了它们。
        现在：没指定 workdir → 副本根；指定的是项目根之内的目录 → 副本里的同一相对位置。
        """
        try:
            from automind.core.session_ctx import workspace
            ws = workspace()
        except Exception:
            ws = None
        if not ws:
            return requested or self.workdir
        ws_path = Path(ws).resolve()
        if not requested:
            return str(ws_path)
        from automind.core.workspace import remap_into_workspace
        try:
            p = Path(str(requested))
            p = (p if p.is_absolute() else ws_path / p).resolve()
            return str(remap_into_workspace(p, Path(self.workdir), ws_path))
        except (OSError, ValueError):
            return str(requested)

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs["command"]
        workdir = self._session_workdir(kwargs.get("workdir"))
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

        # v1.7.5：workdir 预校验。
        # 此前直接把 workdir 交给 create_subprocess_shell(cwd=...)：目录不可用时
        # 在 Windows 上只会抛出裸的 `[WinError 5] 拒绝访问。`，既没说哪个目录、
        # 也没说怎么办 —— 模型只能原样重试或换招数，实测整段预算就烧在这上面。
        wd_error = self._workdir_problem(workdir)
        if wd_error:
            return ToolResult(tool_name=self.name, success=False,
                              exit_code=-1, error=wd_error)

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
        process: Any = None
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
            stdout = decode_output(stdout_bytes).strip()
            stderr = decode_output(stderr_bytes).strip()

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
            if not result.success and os.name == "nt":
                alt = await self._posix_fallback(str(command), stderr, workdir, base_env, timeout)
                if alt is not None:
                    result = alt
            self._last_result = result
            return result

        except TimeoutError:
            # B-15 修复：超时后必须杀死子进程并回收，否则残留为僵尸进程。
            await self._kill_quietly(process)
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
        except asyncio.CancelledError:
            # v1.7.5：用户点「停止」/ 会话断开时，正在跑的命令**必须一起被杀掉**。
            # 此前这里只有 `except TimeoutError` 会 kill，取消则径直穿过 ——
            # 子进程被遗弃，一直活到它自己的超时（最长 1800s），而用户以为
            # 已经停了。取消信号必须继续上浮，不能在这里被吞掉。
            await self._kill_quietly(process)
            logger.warning("terminal_cancelled", command=str(command)[:120])
            raise
        except Exception as e:
            await self._kill_quietly(process)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=self._friendly_spawn_error(e, workdir),
                exit_code=-1,
            )
        finally:
            # 兜底：无论从哪条路径离开，都不留下孤儿进程
            # （成功路径上 returncode 已有值，这里不会重复 kill）
            if process is not None and process.returncode is None:
                await self._kill_quietly(process)

    async def _kill_quietly(self, process: Any) -> None:
        """杀掉子进程**及其整棵进程树**并尽力回收。

        为什么必须杀树：``create_subprocess_shell`` 起的是 ``cmd.exe /c …``，
        真正的命令是它的子进程。只 kill 那个 shell，孙进程会被**遗弃**继续跑
        （实测：取消后 ``python -c "time.sleep(60)"`` 仍在进程表里）——
        用户以为停了，实际还在跑，还占着端口/文件锁。

        清理失败绝不能覆盖原始异常，因此这里只吞清理相关的错误。
        """
        if process is None or getattr(process, "returncode", None) is not None:
            return
        # 用 run_blocking 而不是 asyncio.to_thread：taskkill 是同步阻塞调用，
        # 必须走项目统一的"把阻塞调用挪出事件循环"入口（扫描式护栏会检查这一点，
        # 它保证的是"任何同步 subprocess 都不会把事件循环一起等死"）。
        from automind.tools._toolkit import run_blocking

        with contextlib.suppress(Exception):
            await run_blocking(self._kill_tree, process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError, OSError):
            pass

    @staticmethod
    def _kill_tree(process: Any) -> None:
        """同步地终止整棵进程树（在线程里跑，避免阻塞事件循环）。"""
        pid = getattr(process, "pid", None)
        if pid:
            if os.name == "nt":
                # /T = 连同子进程；/F = 强制。用 DEVNULL 而非 capture_output，
                # 以免在受限环境里因创建管道失败而让清理本身失效。
                with contextlib.suppress(Exception):
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False, timeout=10)
            else:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
        # 兜底：树杀失败时至少把直接子进程收掉
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()

    def _workdir_problem(self, workdir: Any) -> str:
        """workdir 不可用时给一句**能照着做**的话，而不是抛裸异常。"""
        if not workdir:
            return ""
        try:
            p = Path(str(workdir))
        except (TypeError, ValueError):
            return (f"workdir 参数不是合法路径：{workdir!r}。"
                    f"请省略该参数以使用项目根目录。")
        if p.is_dir():
            return ""
        chain = self._missing_ancestor(p)
        return (f"命令工作目录「{p}」不可用（不存在，或存在但不是目录）。"
                + (f"缺失的最近一级是「{chain}」。" if chain else "")
                + "请**省略 workdir 参数**以使用项目根目录，"
                  "或改传一个已存在的相对路径。")

    @staticmethod
    def _missing_ancestor(p: Path) -> str:
        """从 p 往上找第一个存在的祖先，便于把"哪一级不存在"说清楚。"""
        cur = p
        for _ in range(40):                 # 深度上限，防止异常路径死循环
            parent = cur.parent
            if parent == cur:
                return ""
            if parent.is_dir():
                return str(parent)
            cur = parent
        return ""

    def _friendly_spawn_error(self, exc: Exception, workdir: Any) -> str:
        """把 spawn 阶段的原始异常翻译成可执行的指引。

        三平台里 Windows 最不友好：目录不可用、权限不足、命令不存在全都可能
        表现为 `[WinError 5] 拒绝访问。`，而这三种的处理方式完全不同。
        """
        raw = str(exc)
        low = raw.lower()
        hints = []
        if "winerror 5" in low or "errno 13" in low or "permission" in low:
            hints.append("权限不足：该目录或命令当前用户不可执行")
        if "winerror 2" in low or "winerror 3" in low or "no such file" in low:
            hints.append("找不到命令或路径")
        if "not found" in low or "不是内部或外部命令" in raw:
            hints.append("命令本身不存在，请先确认它已安装并在 PATH 中")
        guidance = ("；".join(hints) + "。") if hints else ""
        return (f"命令启动失败：{raw}。{guidance}"
                f"当前 workdir={workdir!r}。"
                f"可尝试：省略 workdir 使用项目根、换用项目内的相对路径、"
                f"或先用 terminal 跑 `cd` 之类的最小命令确认环境。")

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
