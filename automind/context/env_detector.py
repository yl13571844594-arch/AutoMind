"""运行时环境检测 — OS、Shell、Python 版本、已安装工具等。"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


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
        """收集完整的运行时环境信息。"""
        info = EnvironmentInfo()
        info.cwd = str(Path(project_root).resolve())

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
            [sys.executable, "-m", "pip", "--version"]
        )

        # Git
        info.git_available = EnvironmentDetector._check_command(["git", "--version"])
        if info.git_available:
            info.git_branch = EnvironmentDetector._run_command(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"]
            )
            info.git_root = EnvironmentDetector._run_command(
                ["git", "rev-parse", "--show-toplevel"]
            )

        # Node
        info.node_available = EnvironmentDetector._check_command(["node", "--version"])

        # Docker
        info.docker_available = EnvironmentDetector._check_command(["docker", "--version"])

        # 部分环境变量
        for key in ("HOME", "USER", "PATH", "LANG", "PYTHONPATH"):
            val = os.environ.get(key)
            if val:
                info.env_vars[key] = val

        return info

    @staticmethod
    def _check_command(cmd: list[str]) -> bool:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5, check=False)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _run_command(cmd: list[str]) -> str:
        try:
            # Windows 中文环境默认 GBK，必须显式 UTF-8 以免工具输出解码失败
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                                    encoding="utf-8", errors="replace")
            return result.stdout.strip()
        except Exception:
            return ""
