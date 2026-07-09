"""技能注册中心 — 自动发现、注册与调用分发。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.skills.skill_base import AbstractSkill, SkillResult


class SkillRegistry:
    """技能注册中心。

    自动发现:
        - 通过 entry_points "automind.skills" 自动发现
        - 通过目录扫描加载 .py 技能文件

    使用示例::

        registry = SkillRegistry()
        registry.register(MySkill())
        result = await registry.invoke("my_skill", input_data, agent)
    """

    def __init__(self) -> None:
        self._skills: dict[str, AbstractSkill] = {}

    def register(self, skill: AbstractSkill) -> None:
        """注册技能。"""
        if not skill.name:
            raise ValueError("Skill must have a name")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        """注销技能。"""
        self._skills.pop(name, None)

    def get(self, name: str) -> AbstractSkill:
        """获取技能。"""
        if name not in self._skills:
            raise KeyError(f"Skill '{name}' not found. Available: {self.list_names()}")
        return self._skills[name]

    def list_names(self) -> list[str]:
        """列出所有技能名称。"""
        return sorted(self._skills.keys())

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有技能元数据。"""
        return [s.to_dict() for s in self._skills.values()]

    async def invoke(self, name: str, input_data: Any, agent: Any = None) -> SkillResult:
        """调用技能。

        Args:
            name: 技能名称。
            input_data: 输入数据。
            agent: AutoMindAgent 实例 (可选)。

        Returns:
            SkillResult。
        """
        skill = self.get(name)
        try:
            return await skill.execute(input_data, agent)
        except Exception as e:
            return SkillResult(success=False, error=str(e))

    def discover_from_entry_points(self) -> int:
        """从 setuptools entry_points 发现技能。"""
        count = 0
        try:
            from importlib.metadata import entry_points
            eps = entry_points(group="automind.skills")
            for ep in eps:
                try:
                    skill_cls = ep.load()
                    skill = skill_cls()
                    self.register(skill)
                    count += 1
                except Exception:
                    pass
        except Exception:
            pass
        return count

    def discover_from_directory(self, directory: str | Path) -> int:
        """从目录加载 .py 技能文件。

        每个 .py 文件应该包含一个继承自 AbstractSkill 的类。
        """
        count = 0
        dir_path = Path(directory)
        if not dir_path.is_dir():
            return 0

        import importlib.util
        import sys

        for py_file in dir_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            try:
                module_name = f"automind_skill_{py_file.stem}"
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # 查找 AbstractSkill 子类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and
                            issubclass(attr, AbstractSkill) and
                            attr is not AbstractSkill):
                        skill = attr()
                        self.register(skill)
                        count += 1
            except Exception:
                pass
        return count

    def discover_skill_md(self, directory: str | Path, recursive: bool = True) -> int:
        """从目录加载 SKILL.md 格式技能（每个技能一个文件夹，含 SKILL.md）。

        Returns:
            成功注册的技能数量。
        """
        from automind.skills.markdown_skill import MarkdownSkill

        dir_path = Path(directory)
        if not dir_path.is_dir():
            return 0
        pattern = "**/SKILL.md" if recursive else "*/SKILL.md"
        count = 0
        seen: set[str] = set()
        for md in dir_path.glob(pattern):
            # 跳过依赖目录中的同名文件
            if any(p in {"node_modules", ".git", "__pycache__"} for p in md.parts):
                continue
            try:
                skill = MarkdownSkill(md)
                if not skill.name or skill.name in seen:
                    continue
                seen.add(skill.name)
                self.register(skill)
                count += 1
            except Exception:
                continue
        return count

    def discover_any(self, directory: str | Path) -> dict[str, int]:
        """同时尝试加载 .py（AbstractSkill）与 SKILL.md 技能。"""
        py = self.discover_from_directory(directory)
        md = self.discover_skill_md(directory)
        return {"py": py, "markdown": md, "total": py + md}

    def register_builtin_skills(self) -> int:
        """注册所有内置技能。"""
        from automind.skills.builtin.code_generator import CodeGeneratorSkill
        from automind.skills.builtin.dep_audit import DependencyAuditSkill
        from automind.skills.builtin.doc_generator import DocGeneratorSkill
        from automind.skills.builtin.log_analyzer import LogAnalyzerSkill
        from automind.skills.builtin.project_init import ProjectInitSkill
        from automind.skills.builtin.test_runner import TestRunnerSkill

        builtins = [
            ProjectInitSkill(), CodeGeneratorSkill(), TestRunnerSkill(),
            LogAnalyzerSkill(), DocGeneratorSkill(), DependencyAuditSkill(),
        ]
        count = 0
        for skill in builtins:
            self.register(skill)
            count += 1
        return count

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)
