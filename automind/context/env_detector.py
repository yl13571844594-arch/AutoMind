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
        info.os_name = platform.system()
        info.os_version = platform.release()
        info.architecture = platform.machine()

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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return result.stdout.strip()
        except Exception:
            return ""
