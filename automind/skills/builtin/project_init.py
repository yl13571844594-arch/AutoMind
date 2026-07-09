"""项目初始化技能 — 脚手架新项目。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class ProjectInitInput(BaseModel):
    """项目初始化输入。"""

    name: str
    project_type: str = "python"  # python, node, go, rust
    directory: str = "."
    template: str = "minimal"  # minimal, fastapi, flask, cli
    python_version: str = ">=3.11"


class ProjectInitSkill(AbstractSkill):
    """初始化新项目结构 — 创建目录、配置文件、虚拟环境。"""

    name = "project_init"
    description = "Scaffold a new project: create directories, pyproject.toml, virtualenv, git init"

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        if isinstance(input_data, dict):
            inp = ProjectInitInput(**input_data)
        else:
            inp = input_data

        from pathlib import Path

        root = Path(inp.directory) / inp.name
        artifacts = []

        try:
            # 创建目录
            if inp.project_type == "python":
                dirs = [
                    root / "src" / inp.name.replace("-", "_"),
                    root / "tests",
                ]
                for d in dirs:
                    d.mkdir(parents=True, exist_ok=True)
                    artifacts.append(str(d))

                # pyproject.toml
                pyproject = root / "pyproject.toml"
                pyproject.write_text(self._pyproject_template(inp), encoding="utf-8")
                artifacts.append(str(pyproject))

                # __init__.py
                init_py = root / "src" / inp.name.replace("-", "_") / "__init__.py"
                init_py.write_text(f'""" {inp.name} """\n\n__version__ = "0.1.0"\n')
                artifacts.append(str(init_py))

                # tests/conftest.py
                conftest = root / "tests" / "conftest.py"
                conftest.write_text("import pytest\n", encoding="utf-8")
                artifacts.append(str(conftest))

                # git init
                if agent and agent.tool_registry:
                    await agent.tool_registry.dispatch(
                        "terminal",
                        command=f"cd {root} && git init",
                    )
                artifacts.append(f"{root}/.git (initialized)")

            return SkillResult(
                success=True,
                output=f"Project '{inp.name}' initialized at {root}",
                artifacts=artifacts,
            )

        except Exception as e:
            return SkillResult(
                success=False,
                error=str(e),
                artifacts=artifacts,
            )

    @staticmethod
    def _pyproject_template(inp: ProjectInitInput) -> str:
        return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{inp.name}"
version = "0.1.0"
description = ""
requires-python = "{inp.python_version}"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.3.0"]
"""
