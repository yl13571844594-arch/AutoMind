"""运行时环境检测 — OS、Shell、Python 版本、已安装工具等。"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EnvironmentInfo:
    """运行时环境完整信息。"""

    os_name: str = ""
    os_version: str = ""
    architecture: str = ""
    shell: str = ""
    python_version: str = ""
    python_executable: str = ""
    pip_available: bool = False
    git_available: bool = False
    git_branch: str = ""
    git_root: str = ""
    node_available: bool = False
    docker_available: bool = False
    virtual_env: str = ""
    installed_packages: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    cwd: str = ""

    def to_prompt_context(self) -> str:
        """生成 LLM 系统提示用的环境信息文本。"""
        lines = [
            f"Operating System: {self.os_name} {self.os_version} ({self.architecture})",
            f"Shell: {self.shell}",
            f"Python: {self.python_version} ({self.python_executable})",
            f"Current Directory: {self.cwd}",
        ]
        if self.virtual_env:
            lines.append(f"Virtual Environment: {self.virtual_env}")
        if self.git_available and self.git_root:
            lines.append(f"Git Repository: {self.git_root}")
            if self.git_branch:
                lines.append(f"Git Branch: {self.git_branch}")
        lines.append(f"Tools: pip={self.pip_available}, git={self.git_available}, "
                      f"node={self.node_available}, docker={self.docker_available}")
        return "\n".join(lines)


class EnvironmentDetector:
    """检测当前运行时环境。"""

    @staticmethod
    def detect(project_root: str | Path = ".") -> EnvironmentInfo:
        """收集完整的运行时环境信息。

        v1.11.18：所有外部探活都必须以 ``project_root`` 为工作目录执行。
        此前它们继承**进程的 cwd** —— 在 Web 服务里恰好等于项目目录，看不出
        问题；但评测/工作流会先把 ``config.project_root`` 指到临时工作区，
        而进程 cwd 仍在仓库根，于是 ``git rev-parse`` 把**仓库根**写进系统
        提示（"Git Repository: …自动化"），与 "Current Directory: …Temp\\…"
        自相矛盾。实测模型因此跑去仓库里翻源码/断言定义，把"写一个中文文件"
        这种简单任务烧到 30 万 token。环境信息必须描述**任务工作区**，
        不是 agent 进程碰巧所在的位置。
        """
        info = EnvironmentInfo()
        root = Path(project_root).resolve()
        info.cwd = str(root)
        # 目录不存在时退化为"不指定 cwd"（探活本身仍要能回答"装没装"）
        workdir = str(root) if root.is_dir() else None

        # OS 信息
        # 注意：不要用 platform.system()/platform.release()/platform.machine() ——
        # Python 3.12 在 Windows 上它们会走 WMI 查询（wmic），而 WMI 在部分环境
        # （安全软件拦截、WMI 服务异常、精简系统）会**无限阻塞**，导致 Agent 构建/
        # 服务启动永久挂起（曾实测卡死 2 分钟+）。改用纯本地 API，零外部调用：
        #   · os.name / sys.platform —— 纯常量
        #   · sys.getwindowsversion() —— 纯 API，不走 WMI
        #   · PROCESSOR_ARCHITECTURE 环境变量 —— 纯读取（注意：3.12 的
        #     platform.machine() 在该变量缺失时也会 fallback 到 WMI，故不可用）
        if os.name == "nt":
            info.os_name = "Windows"
            try:
                wv = sys.getwindowsversion()
                info.os_version = f"{wv.major}.{wv.minor}.{wv.build}"
            except Exception:
                info.os_version = ""
        else:
            info.os_name = platform.system()
            info.os_version = platform.release()
        info.architecture = (
            os.environ.get("PROCESSOR_ARCHITECTURE")
            or os.environ.get("PROCESSOR_ARCHITEW6432")
            or "")

        # Shell
        info.shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown"))

        # Python
        info.python_version = sys.version.split()[0]
        info.python_executable = sys.executable
        info.virtual_env = os.environ.get("VIRTUAL_ENV", os.environ.get("CONDA_PREFIX", ""))

        # pip
        info.pip_available = EnvironmentDetector._check_command(
            [sys.executable, "-m", "pip", "--version"], cwd=workdir
        )

        # Git（必须在项目根探测：否则会把"进程所在仓库"误报成任务工作区）
        info.git_available = EnvironmentDetector._check_command(
            ["git", "--version"], cwd=workdir)
        if info.git_available:
            info.git_branch = EnvironmentDetector._run_command(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workdir
            )
            info.git_root = EnvironmentDetector._run_command(
                ["git", "rev-parse", "--show-toplevel"], cwd=workdir
            )

        # Node
        info.node_available = EnvironmentDetector._check_command(
            ["node", "--version"], cwd=workdir)

        # Docker
        info.docker_available = EnvironmentDetector._check_command(
            ["docker", "--version"], cwd=workdir)

        # 部分环境变量
        for key in ("HOME", "USER", "PATH", "LANG", "PYTHONPATH"):
            val = os.environ.get(key)
            if val:
                info.env_vars[key] = val

        return info

    @staticmethod
    def _check_command(cmd: list[str], cwd: str | None = None) -> bool:
        # 捕获面必须覆盖 **OSError 全家**，不能只列 FileNotFoundError：
        #   · Windows 上把一个**目录**放进 PATH 时 CreateProcess 抛
        #     PermissionError(WinError 5) —— 探活炸掉而不是回答"没有"；
        #   · 应用容器的令牌限制、杀软拦截同样抛 PermissionError/OSError。
        # 这些都不是"命令存在但出错"（那种情况 subprocess 会正常返回），
        # 而是"根本没能启动它" —— 探活要回答的正是这个，答案为否。
        # TimeoutExpired 不是 OSError 子类，单独列出。
        #
        # ``cwd`` 与 ``_run_command`` 同义：探活也要在任务工作区里进行，
        # 否则"这个项目里 git 可用吗"会被进程 cwd 的仓库悄悄回答成"可用"。
        kwargs: dict[str, Any] = {"capture_output": True, "timeout": 5, "check": False}
        if cwd:
            kwargs["cwd"] = cwd
        try:
            subprocess.run(cmd, **kwargs)
            return True
        except subprocess.TimeoutExpired:
            # 5 秒还没结束：命令存在但卡住（如首次运行的防病毒扫描）。
            # 视为不可用 —— 探活的意义是"现在能不能用"，不是"装没装"。
            return False
        except OSError:
            return False

    @staticmethod
    def _run_command(cmd: list[str], cwd: str | None = None) -> str:
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 5,
                                  "encoding": "utf-8", "errors": "replace"}
        if cwd:
            kwargs["cwd"] = cwd
        try:
            # Windows 中文环境默认 GBK，必须显式 UTF-8 以免工具输出解码失败
            result = subprocess.run(cmd, **kwargs)
            return result.stdout.strip()
        except Exception:
            return ""
