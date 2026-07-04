"""测试运行技能 — 发现并运行测试，收集失败信息。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


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

        cmd = self._build_command(framework, inp)

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
                import subprocess
                proc = subprocess.run(
                    cmd, shell=True, cwd=inp.directory,
                    capture_output=True, text=True, timeout=120,
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
    def _build_command(framework: str, inp: TestRunInput) -> str:
        if framework == "pytest":
            cmd = f"python -m pytest {inp.pattern}"
            if inp.verbose:
                cmd += " -v"
            if inp.fail_fast:
                cmd += " -x"
            return cmd
        elif framework == "unittest":
            return f"python -m unittest discover -p '{inp.pattern}'"
        elif framework == "jest":
            return "npx jest" + (" --verbose" if inp.verbose else "")
        elif framework == "go_test":
            return "go test ./..."
        return f"python -m pytest {inp.pattern} -v"
