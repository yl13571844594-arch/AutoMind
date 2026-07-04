"""文档生成技能（§14.8）— 从 Python 源码的 AST 提取 API 生成 Markdown 文档。

纯标准库实现（ast），无外部依赖，结果确定可测。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class DocGenInput(BaseModel):
    """文档生成输入。"""

    source: str                 # .py 文件或目录
    output_file: str = ""       # 可选：Markdown 输出路径
    include_private: bool = False  # 是否包含下划线开头的私有符号


class DocGeneratorSkill(AbstractSkill):
    """从 Python 源码提取模块/类/函数签名与 docstring，生成 Markdown 文档。"""

    name = "doc_generator"
    description = "Generate Markdown API docs from Python source (functions, classes, docstrings)."
    input_schema = DocGenInput

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:  # noqa: ARG002
        inp = DocGenInput(**input_data) if isinstance(input_data, dict) else input_data

        src = Path(inp.source)
        if not src.exists():
            return SkillResult(success=False, error=f"源路径不存在：{src}")

        files = [src] if src.is_file() else sorted(src.rglob("*.py"))
        files = [f for f in files if f.suffix == ".py" and "__pycache__" not in f.parts]
        if not files:
            return SkillResult(success=False, error="未找到 Python 源文件")

        sections: list[str] = []
        symbol_count = 0
        for f in files:
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            md, count = self._document_module(f.name, tree, inp.include_private)
            symbol_count += count
            if md:
                sections.append(md)

        markdown = "# API 文档\n\n" + "\n\n".join(sections) + "\n"

        artifacts: list[str] = []
        if inp.output_file:
            try:
                path = Path(inp.output_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(markdown, encoding="utf-8")
                artifacts.append(str(path))
            except OSError as e:
                return SkillResult(success=False, error=f"写入文档失败：{e}")

        return SkillResult(
            success=True,
            output={"markdown": markdown, "files": len(files), "symbols": symbol_count},
            artifacts=artifacts,
        )

    def _document_module(
        self, filename: str, tree: ast.Module, include_private: bool
    ) -> tuple[str, int]:
        lines = [f"## {filename}"]
        mod_doc = ast.get_docstring(tree)
        if mod_doc:
            lines.append(f"\n{mod_doc.strip()}\n")

        count = 0
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not include_private and node.name.startswith("_"):
                    continue
                lines.append(self._document_function(node, level=3))
                count += 1
            elif isinstance(node, ast.ClassDef):
                if not include_private and node.name.startswith("_"):
                    continue
                lines.append(f"### class `{node.name}`")
                cdoc = ast.get_docstring(node)
                if cdoc:
                    lines.append(f"\n{cdoc.strip()}\n")
                count += 1
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not include_private and item.name.startswith("_"):
                            continue
                        lines.append(self._document_function(item, level=4, method=True))
                        count += 1
        return "\n".join(lines), count

    @classmethod
    def _document_function(
        cls, node: ast.FunctionDef | ast.AsyncFunctionDef, level: int, method: bool = False
    ) -> str:
        prefix = "#" * level
        async_kw = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        sig = cls._signature(node)
        kind = "method" if method else "def"
        out = [f"{prefix} `{async_kw}{kind} {node.name}{sig}`"]
        doc = ast.get_docstring(node)
        if doc:
            out.append(f"\n{doc.strip()}\n")
        return "\n".join(out)

    @staticmethod
    def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = ", ".join(a.arg for a in node.args.args)
        returns = ""
        if node.returns is not None:
            try:
                returns = f" -> {ast.unparse(node.returns)}"
            except Exception:
                returns = ""
        return f"({args}){returns}"
