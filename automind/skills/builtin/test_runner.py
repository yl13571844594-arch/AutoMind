"""测试运行技能 — 发现并运行测试，收集失败信息。"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult

# pattern 由模型填写，此前被直接拼进 shell 字符串并以 shell=True 执行 ——
# 传 `x; rm -rf ~` 即可注入任意命令，且因为拼出来的整条命令不长得像危险命令，
# terminal 工具的高危正则也拦不住。这里只放行"看起来确实是文件通配符"的内容：
# 字母数字与 . _ - * ? [ ] /，不含空格、引号、分号、管道、$、反引号等任何
# 具有 shell 语义的字符。命令一律以 argv 列表构造，不再经过 shell。
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9_.\-*?\[\]/]{1,120}$")


class UnsafePatternError(ValueError):
    """pattern 含 shell 元字符或过长，拒绝构造命令。"""


class TestRunInput(BaseModel):
    """测试运行输入。"""

    directory: str = "."
    framework: str = "auto"  # auto, pytest, unittest, jest, go_test
    pattern: str = "test_*.py"
    verbose: bool = True
    fail_fast: bool = False


class TestRunnerSkill(AbstractSkill):
    """运行测试并生成报告。支持 pytest, unittest, 和自定义框架。"""

    name = "test_runner"
    description = "Discover and run tests, collect failures, generate reports"

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        if isinstance(input_data, dict):
            inp = TestRunInput(**input_data)
        else:
            inp = input_data

        framework = inp.framework
        if framework == "auto":
            framework = self._detect_framework(inp.directory)

        # pattern 非法时**不执行任何命令**，直接把原因回给模型
        try:
            argv = self._build_argv(framework, inp)
        except UnsafePatternError as e:
            return SkillResult(success=False, error=str(e))
        cmd = self._quote(argv)

        try:
            if agent and agent.tool_registry:
                result = await agent.tool_registry.dispatch(
                    "terminal",
                    command=cmd,
                    workdir=inp.directory,
                )
                return SkillResult(
                    success=result.success,
                    output={
                        "framework": framework,
                        "command": cmd,
                        "stdout": result.output.get("stdout", "") if isinstance(result.output, dict) else str(result.output),
                        "exit_code": result.exit_code,
                    },
                    error=result.error,
                )
            else:
                # shell=False + argv 列表：即便 pattern 校验被绕过，
                # 参数也只会被当作 pytest 的一个参数，而不是新的一条命令。
                proc = subprocess.run(
                    argv, shell=False, cwd=inp.directory,
                    capture_output=True, text=True, timeout=120,
                    encoding="utf-8", errors="replace",
                )
                return SkillResult(
                    success=proc.returncode == 0,
                    output={
                        "framework": framework,
                        "command": cmd,
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "exit_code": proc.returncode,
                    },
                    error=proc.stderr if proc.returncode != 0 else "",
                )
        except Exception as e:
            return SkillResult(success=False, error=str(e))

    @staticmethod
    def _detect_framework(directory: str) -> str:
        """自动检测测试框架。"""
        root = Path(directory)
        if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
            content = (root / "pyproject.toml").read_text() if (root / "pyproject.toml").exists() else ""
            if "pytest" in content:
                return "pytest"
        if (root / "jest.config.js").exists() or (root / "jest.config.ts").exists():
            return "jest"
        if list(root.glob("*_test.go")):
            return "go_test"
        return "pytest"  # 默认

    @staticmethod
    def _build_argv(framework: str, inp: TestRunInput) -> list[str]:
        """构造 argv 列表 —— 不拼 shell 字符串，从根上消灭命令注入。"""
        pattern = inp.pattern or "test_*.py"
        if not _SAFE_PATTERN.match(pattern):
            raise UnsafePatternError(
                f"pattern 含非法字符或过长，已拒绝：{pattern!r}（只允许字母数字与 . _ - * ? [ ] /）")

        if framework == "unittest":
            return ["python", "-m", "unittest", "discover", "-p", pattern]
        if framework == "jest":
            return ["npx", "jest"] + (["--verbose"] if inp.verbose else [])
        if framework == "go_test":
            return ["go", "test", "./..."]
        # pytest 与兜底分支
        argv = ["python", "-m", "pytest", pattern]
        if inp.verbose:
            argv.append("-v")
        if inp.fail_fast:
            argv.append("-x")
        return argv

    @staticmethod
    def _quote(argv: list[str]) -> str:
        """仅用于必须传字符串的 terminal 工具；各段已经过白名单校验。"""
        return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
