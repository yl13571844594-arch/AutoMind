"""项目记忆 — 学习代码风格、架构模式、命名约定。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.context.code_analyzer import StyleProfile


class ProjectMemory:
    """项目级记忆 — 持续从代码中学习并应用约定。

    学习内容:
        - 代码风格 (缩进、引号、行长)
        - 命名约定 (snake_case, camelCase, PascalCase)
        - 导入风格 (绝对导入 vs 相对导入)
        - 架构模式 (分层、模块职责)
        - 测试框架和模式
        - 常用依赖
    """

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self.style: StyleProfile = StyleProfile()
        self.architecture_notes: str = ""
        self.conventions: dict[str, str] = {}
        self.common_imports: list[str] = []
        self.test_framework: str = ""
        self._observations: list[dict[str, Any]] = []
        self._learned_patterns: dict[str, int] = {}

    async def learn_from_analysis(self, analysis: Any) -> None:
        """从 CodeAnalysis 结果学习项目风格和模式。"""
        # 风格
        self.style = analysis.style

        # 常见导入
        import_counter: dict[str, int] = {}
        for imp in analysis.imports:
            top = imp.module.split(".")[0]
            if top not in ("__future__", "typing", "os", "sys"):
                import_counter[top] = import_counter.get(top, 0) + 1
        self.common_imports = [
            mod for mod, _ in sorted(import_counter.items(), key=lambda x: -x[1])
        ][:20]

        # 命名约定
        for sym in analysis.symbols:
            pattern = self._classify_name(sym.name)
            self._learned_patterns[pattern] = self._learned_patterns.get(pattern, 0) + 1

        # 推断测试框架
        for imp in analysis.imports:
            if "pytest" in imp.module:
                self.test_framework = "pytest"
            elif "unittest" in imp.module:
                if not self.test_framework:
                    self.test_framework = "unittest"

    async def learn_patterns(self, file_paths: list[str] | None = None) -> StyleProfile:
        """扫描项目学习所有模式。"""
        from automind.context.code_analyzer import CodeAnalyzer
        analyzer = CodeAnalyzer(self.project_root)
        analysis = analyzer.analyze(file_paths)
        await self.learn_from_analysis(analysis)
        return self.style

    def get_style_guide(self) -> str:
        """生成可注入 LLM 提示的风格指南。"""
        return self.style.to_prompt()

    def get_convention_prompt(self) -> str:
        """生成包含所有项目约定的提示文本。"""
        parts = [self.get_style_guide()]

        if self.test_framework:
            parts.append(f"- Test framework: {self.test_framework}")
        if self.common_imports:
            parts.append(f"- Popular dependencies: {', '.join(self.common_imports[:10])}")
        if self._learned_patterns:
            top = sorted(self._learned_patterns.items(), key=lambda x: -x[1])
            parts.append(f"- Naming patterns: {', '.join(f'{k}({v})' for k, v in top[:5])}")

        return "\n".join(parts)

    def apply_style(self, code: str) -> str:
        """自动应用项目风格 (简单的文本替换)。"""
        if self.style.indent_type == "space":
            # Tab → spaces
            code = code.replace("\t", " " * self.style.indent_size)
        if self.style.quote_style == "double":
            # 保守处理：不自动替换，以免破坏字符串内容
            pass
        return code

    @staticmethod
    def _classify_name(name: str) -> str:
        """分类命名风格。"""
        if name.startswith("_"):
            return "private"
        if "_" in name and name.islower():
            return "snake_case"
        if name[0].isupper():
            return "PascalCase"
        if name[0].islower() and any(c.isupper() for c in name):
            return "camelCase"
        if name.isupper():
            return "UPPER_CASE"
        return "other"
