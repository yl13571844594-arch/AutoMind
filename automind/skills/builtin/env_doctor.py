"""环境诊断技能 —— 检查 Python/依赖/端口/磁盘，输出一份环境体检报告。

不依赖外部服务，纯本地检查：Python 与平台版本、可选依赖是否就绪（版本号）、
常见端口是否被占用、磁盘剩余空间。用于"跑不起来 / 缺依赖 / 端口冲突"类
问题的一键定位。
"""

from __future__ import annotations

import importlib.metadata
import platform
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class EnvDoctorInput(BaseModel):
    directory: str = "."  # 磁盘检查的目标目录
    ports: list[int] = [8765, 8000, 5000, 3306, 5432, 6379, 8080]


class EnvDoctorSkill(AbstractSkill):
    """诊断运行环境（Python / 依赖 / 端口 / 磁盘）。"""

    name = "env_doctor"
    description = "Diagnose the runtime environment: Python version, optional deps, ports, disk space"

    #: 关注的可选依赖：模块名 -> 说明
    _INTERESTED = {
        "openpyxl": "Excel 读写", "docx": "Word 读写", "pypdf": "PDF",
        "pptx": "PowerPoint", "httpx": "HTTP/搜索", "PIL": "图像/截屏",
        "matplotlib": "图表", "psutil": "进程/端口", "pyperclip": "剪贴板",
        "pytesseract": "OCR", "mutagen": "音频", "tiktoken": "Token 计数",
        "anthropic": "Claude 后端", "google.generativeai": "Gemini 后端",
        "fastapi": "Web 服务", "uvicorn": "Web 服务",
    }

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        inp = EnvDoctorInput(**input_data) if isinstance(input_data, dict) else input_data
        try:
            lines = ["# 环境体检报告", ""]
            lines.append("## 系统")
            lines.append(f"- Python：{sys.version.split()[0]}（{platform.python_implementation()}）")
            lines.append(f"- 平台：{platform.system()} {platform.release()}（{platform.machine()}）")
            lines.append(f"- 可执行：{sys.executable}")

            lines.append("")
            lines.append("## 可选依赖")
            lines.append("")
            lines.append("| 模块 | 用途 | 状态 |")
            lines.append("|------|------|------|")
            for mod, purpose in self._INTERESTED.items():
                ver = self._version(mod)
                lines.append(f"| {mod} | {purpose} | {('✅ ' + ver) if ver else '❌ 未安装'} |")

            lines.append("")
            lines.append("## 端口占用")
            lines.append("")
            for port in inp.ports:
                state = self._port_state(port)
                lines.append(f"- `:{port}` {'⚠ 已被占用' if state else '空闲'}")

            lines.append("")
            lines.append("## 磁盘空间")
            for p in (inp.directory, str(Path.home())):
                try:
                    usage = shutil.disk_usage(p)
                    gb = usage.free / (1024 ** 3)
                    lines.append(f"- `{p}` 剩余 **{gb:.1f} GB**（总量 {usage.total / (1024 ** 3):.1f} GB）")
                except OSError:
                    continue

            return SkillResult(success=True, output="\n".join(lines) + "\n")
        except Exception as e:
            return SkillResult(success=False, error=str(e))

    @staticmethod
    def _version(module: str) -> str | None:
        """返回模块版本号；未安装返回 None。"""
        try:
            return importlib.metadata.version(module)
        except importlib.metadata.PackageNotFoundError:
            return None

    @staticmethod
    def _port_state(port: int) -> bool:
        """端口是否被监听（TCP 尝试连接）。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0
