"""系统工具集 —— git 结构化操作 / 进程与端口管理 / 剪贴板读写。

git 与进程管理走 subprocess / psutil（psutil 为可选依赖），剪贴板走 pyperclip
（可选依赖），全部遵循缺库时返回安装命令的约定。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, err, need, ok
from automind.tools.base import AbstractTool


# ── git ───────────────────────────────────────────────────
class GitTool(AbstractTool):
    """结构化的 git 操作，带危险命令门控。"""

    name = "git_tool"
    description = (
        "Run structured git operations inside a repository. Actions: status, log, diff, "
        "add (stage files), commit (with message), branch, checkout, pull, push. "
        "Destructive actions (push --force, hard reset) are rejected unless explicitly "
        "allowed via force=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "log", "diff", "add", "commit", "branch", "checkout", "pull", "push"]},
            "repo": {"type": "string", "description": "Repository directory (default: project root)."},
            "message": {"type": "string", "description": "Commit message (for commit)."},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files to stage (for add; default '.')."},
            "branch": {"type": "string", "description": "Branch name (for branch/checkout)."},
            "remote": {"type": "string", "description": "Remote name (for push/pull, default origin)."},
            "max_log": {"type": "number", "description": "Number of log entries (default 20)."},
            "force": {"type": "boolean", "description": "Allow force push (default false)."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = str(Path(project_root).resolve())

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        repo = Path(str(kwargs.get("repo") or self.project_root)).expanduser()
        try:
            # core.quotepath=false：否则 git 会把非 ASCII 文件名转义成
            # "\344\275\277\347\224\250..." 这种八进制串 —— 中文项目里
            # status/diff 的输出等于没法看，模型也读不懂改的是哪个文件。
            cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false"]

            if action == "status":
                cmd += ["status", "--short", "--branch"]
            elif action == "log":
                cmd += ["log", "--oneline", f"-n{int(kwargs.get('max_log', 20) or 20)}"]
            elif action == "diff":
                cmd += ["diff", "--stat"]
            elif action == "add":
                files = kwargs.get("files") or ["."]
                cmd += ["add", "--", *[str(f) for f in files]]
            elif action == "commit":
                msg = str(kwargs.get("message") or "").strip()
                if not msg:
                    return bad(self.name, "commit 需要 message")
                cmd += ["commit", "-m", msg]
            elif action == "branch":
                cmd += ["branch", "--list"] if not kwargs.get("branch") else ["branch", str(kwargs["branch"])]
            elif action == "checkout":
                if not kwargs.get("branch"):
                    return bad(self.name, "checkout 需要 branch")
                cmd += ["checkout", str(kwargs["branch"])]
            elif action == "pull":
                cmd += ["pull", str(kwargs.get("remote") or "origin")]
            elif action == "push":
                force = bool(kwargs.get("force"))
                if force:
                    # 强制推送覆盖远端历史，属高危，需显式 force=true 才放行
                    cmd += ["push", "--force", str(kwargs.get("remote") or "origin")]
                else:
                    cmd += ["push", str(kwargs.get("remote") or "origin")]
            else:
                return bad(self.name, f"未知 action：{action}")

            # 必须显式指定 utf-8：`text=True` 用的是系统 ANSI 代码页，中文
            # Windows 上是 GBK —— 提交信息里只要有一个 GBK 表示不了的字符
            # （中文项目的提交信息几乎必然如此），读取线程就会以
            # UnicodeDecodeError 悄悄死掉，最终 output 变成空串而
            # exit_code 仍是 0：调用方看到的是"命令成功但什么也没输出"。
            proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False,
                                  encoding="utf-8", errors="replace")
            output = (proc.stdout or "") + (proc.stderr or "")
            return ok(self.name, action=action, exit_code=proc.returncode,
                      output=output.strip()[:8000],
                      success=proc.returncode == 0)
        except Exception as e:
            return err(self.name, e)


# ── 进程 / 端口 ───────────────────────────────────────────
class ProcessTool(AbstractTool):
    """进程列表 / 端口占用查询 / 结束进程。"""

    name = "process_tool"
    description = (
        "Inspect and manage OS processes. Actions: list (top processes by CPU/memory), "
        "ports (which process listens on which TCP/UDP port), kill (terminate a process "
        "by pid). kill is destructive and gated by the permission engine."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "ports", "kill"]},
            "limit": {"type": "number", "description": "Max entries for list (default 20)."},
            "pid": {"type": "number", "description": "Process id for kill."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 45

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        try:
            need("psutil")
            import psutil

            if action == "list":
                limit = int(kwargs.get("limit", 20) or 20)
                procs = []
                for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                    try:
                        info = p.info
                        procs.append({
                            "pid": info["pid"], "name": info["name"] or "",
                            "cpu": round(info["cpu_percent"] or 0, 1),
                            "mem_mb": round((info["memory_info"].rss if info.get("memory_info") else 0) / 1048576, 1),
                        })
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                procs.sort(key=lambda x: x["cpu"], reverse=True)
                return ok(self.name, action=action, count=len(procs), processes=procs[:limit])

            if action == "ports":
                conns = []
                for c in psutil.net_connections(kind="inet"):
                    if not c.laddr:
                        continue
                    pid = c.pid
                    name = ""
                    if pid is not None:
                        try:
                            name = psutil.Process(pid).name() or ""
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            name = ""
                    conns.append({
                        "protocol": "tcp" if c.type.name == "SOCK_STREAM" else "udp",
                        "local": f"{c.laddr.ip}:{c.laddr.port}",
                        "pid": pid, "process": name,
                        "status": getattr(c, "status", ""),
                    })
                return ok(self.name, action=action, count=len(conns), connections=conns)

            if action == "kill":
                pid = kwargs.get("pid")
                if not pid:
                    return bad(self.name, "kill 需要 pid")
                p = psutil.Process(int(pid))
                name = p.name() if p else ""
                p.terminate()
                return ok(self.name, action=action, pid=int(pid), name=name,
                          message=f"已发送终止信号给进程 {pid}（{name}）")

            return bad(self.name, f"未知 action：{action}")
        except Exception as e:
            return err(self.name, e)


# ── 剪贴板 ────────────────────────────────────────────────
class ClipboardTool(AbstractTool):
    """读写系统剪贴板文本。"""

    name = "clipboard_tool"
    description = (
        "Read or write the system clipboard (text). Actions: read (return clipboard "
        "text), write (set clipboard text). Requires 'pip install pyperclip'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write"]},
            "text": {"type": "string", "description": "Text to write (for write)."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 15

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        try:
            need("pyperclip")
            import pyperclip

            if action == "read":
                text = pyperclip.paste()
                return ok(self.name, action=action, text=text, length=len(text))
            if action == "write":
                text = str(kwargs.get("text") or "")
                pyperclip.copy(text)
                return ok(self.name, action=action, length=len(text),
                          message="已写入剪贴板")
            return bad(self.name, f"未知 action：{action}")
        except Exception as e:
            return err(self.name, e)
