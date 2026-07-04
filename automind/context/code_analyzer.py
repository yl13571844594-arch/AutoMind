"""代码分析器 — AST 解析提取符号、导入、调用关系、代码风格。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Symbol:
    """代码符号。"""

    name: str
    kind: str  # function, class, method, variable, module
    file_path: str
    line: int
    column: int = 0
    docstring: str = ""
    decorators: list[str] = field(default_factory=list)
    parent_class: str = ""
    has_annotations: bool = False  # B-17：函数/方法是否含类型注解（参数或返回值）


@dataclass
class ImportInfo:
    """导入信息。"""

    module: str
    names: list[str] = field(default_factory=list)
    is_from_import: bool = False
    alias: str = ""
    line: int = 0


@dataclass
class CallInfo:
    """函数调用信息。"""

    caller: str
    callee: str
    file_path: str
    line: int


@dataclass
class StyleProfile:
    """代码风格特征。"""

    indent_size: int = 4
    indent_type: str = "space"  # space, tab
    quote_style: str = "double"  # double, single
    max_line_length: int = 88
    naming_convention: str = "snake_case"  # snake_case, camelCase, PascalCase
    import_style: str = "absolute"  # absolute, relative
    type_annotations_used: bool = False
    docstring_style: str = "google"  # google, numpy, sphinx, none

    def to_prompt(self) -> str:
        """生成注入 LLM 提示的风格指南。"""
        return (
            f"Follow these code style conventions:\n"
            f"- Indentation: {self.indent_size} {self.indent_type}s\n"
            f"- Quotes: {self.quote_style} quotes\n"
            f"- Max line length: {self.max_line_length}\n"
            f"- Naming: {self.naming_convention}\n"
            f"- Imports: {self.import_style}\n"
            f"- Type annotations: {'required' if self.type_annotations_used else 'optional'}\n"
        )


@dataclass
class CodeAnalysis:
    """代码分析结果。"""

    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    calls: list[CallInfo] = field(default_factory=list)
    style: StyleProfile = field(default_factory=StyleProfile)
    file_count: int = 0
    total_lines: int = 0

    def to_summary(self) -> str:
        """生成分析摘要。"""
        symbols_by_kind: dict[str, int] = {}
        for s in self.symbols:
            symbols_by_kind[s.kind] = symbols_by_kind.get(s.kind, 0) + 1

        deps = set(imp.module.split(".")[0] for imp in self.imports)
        return (
            f"Analyzed {self.file_count} files ({self.total_lines} lines)\n"
            f"Symbols: {', '.join(f'{v} {k}s' for k, v in symbols_by_kind.items())}\n"
            f"External dependencies: {', '.join(sorted(deps)) if deps else 'none'}\n"
        )


class CodeAnalyzer:
    """Python 代码静态分析器。

    Note: 使用标准库 ast 模块进行 Python 代码分析。
    对于其他语言，可通过 tree-sitter 扩展。
    """

    PYTHON_EXTENSIONS = {".py", ".pyi"}

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()

    def analyze(self, file_paths: list[str] | None = None) -> CodeAnalysis:
        """分析指定文件或整个项目的 Python 代码。

        Args:
            file_paths: 文件路径列表。为 None 时分析所有 Python 文件。

        Returns:
            CodeAnalysis 实例。
        """
        if file_paths is None:
            file_paths = self._find_python_files()

        analysis = CodeAnalysis()
        for fp in file_paths:
            path = Path(fp)
            if not path.is_absolute():
                path = self.project_root / path
            if not path.exists() or path.suffix not in self.PYTHON_EXTENSIONS:
                continue

            try:
                source = path.read_text(encoding="utf-8")
                analysis.total_lines += source.count("\n") + 1
                analysis.file_count += 1
                self._parse_file(source, str(path), analysis)
            except (SyntaxError, UnicodeDecodeError):
                continue

        analysis.style = self._detect_style(analysis)
        return analysis

    def _find_python_files(self) -> list[str]:
        py_files = []
        for path in self.project_root.rglob("*.py"):
            if ".venv" in str(path) or "node_modules" in str(path):
                continue
            if "__pycache__" in str(path):
                continue
            py_files.append(str(path))
        return py_files

    def _parse_file(self, source: str, file_path: str, analysis: CodeAnalysis) -> None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            # 函数/方法
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent_class = ""
                # 简单检测：如果是方法，父类是哪个
                all_args = (
                    node.args.args + node.args.posonlyargs + node.args.kwonlyargs
                )
                has_ann = node.returns is not None or any(
                    a.annotation is not None for a in all_args
                )
                sym = Symbol(
                    name=node.name,
                    kind="method" if self._is_method(node, tree) else "function",
                    file_path=file_path,
                    line=node.lineno,
                    decorators=[self._get_decorator_name(d) for d in node.decorator_list],
                    docstring=ast.get_docstring(node) or "",
                    has_annotations=has_ann,
                )
                analysis.symbols.append(sym)

            # 类
            elif isinstance(node, ast.ClassDef):
                sym = Symbol(
                    name=node.name,
                    kind="class",
                    file_path=file_path,
                    line=node.lineno,
                    decorators=[self._get_decorator_name(d) for d in node.decorator_list],
                    docstring=ast.get_docstring(node) or "",
                )
                analysis.symbols.append(sym)

            # 导入
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    analysis.imports.append(ImportInfo(
                        module=alias.name,
                        names=[alias.name],
                        alias=alias.asname or "",
                        line=node.lineno,
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    analysis.imports.append(ImportInfo(
                        module=module,
                        names=[alias.name],
                        is_from_import=True,
                        alias=alias.asname or "",
                        line=node.lineno,
                    ))

            # 函数调用
            elif isinstance(node, ast.Call):
                callee = self._get_call_name(node)
                if callee:
                    analysis.calls.append(CallInfo(
                        caller="",
                        callee=callee,
                        file_path=file_path,
                        line=node.lineno,
                    ))

    def _detect_style(self, analysis: CodeAnalysis) -> StyleProfile:
        """从分析结果推断代码风格。"""
        profile = StyleProfile()
        if analysis.symbols:
            # Naming convention
            snake = sum(1 for s in analysis.symbols if "_" in s.name and s.name.islower())
            camel = sum(1 for s in analysis.symbols if s.name[0].islower()
                        and any(c.isupper() for c in s.name))
            pascal = sum(1 for s in analysis.symbols if s.name[0].isupper())
            if snake > camel and snake > pascal:
                profile.naming_convention = "snake_case"
            elif camel > snake:
                profile.naming_convention = "camelCase"
            else:
                profile.naming_convention = "PascalCase"

            # Type annotations —— B-17 修复：真正统计带注解的函数占比，
            # 而非"只要有函数就判定为已使用注解"。
            funcs = [s for s in analysis.symbols if s.kind in ("function", "method")]
            if funcs:
                annotated = sum(1 for s in funcs if s.has_annotations)
                profile.type_annotations_used = (annotated / len(funcs)) > 0.5

        return profile

    @staticmethod
    def _is_method(node: ast.FunctionDef | ast.AsyncFunctionDef, tree: ast.Module) -> bool:
        """检查函数是否是类方法。"""
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for item in n.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name == node.name and item.lineno == node.lineno:
                            return True
        return False

    @staticmethod
    def _get_decorator_name(decorator: ast.expr) -> str:
        if isinstance(decorator, ast.Name):
            return decorator.id
        if isinstance(decorator, ast.Attribute):
            return f"{ast.unparse(decorator.value)}.{decorator.attr}"
        return ast.unparse(decorator)

    @staticmethod
    def _get_call_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return f"{ast.unparse(node.func.value)}.{node.func.attr}"
        return ""
