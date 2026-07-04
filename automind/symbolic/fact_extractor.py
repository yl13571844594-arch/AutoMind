"""事实抽取器 — 从代码 AST 提取 Datalog 事实。"""

from __future__ import annotations

import ast
from pathlib import Path

from automind.symbolic.datalog_engine import DatalogEngine


class FactExtractor:
    """从代码中提取逻辑事实。

    提取的事实类型:
        - file(path) — 文件存在
        - defines(file, symbol, kind) — 文件定义了符号
        - imports(file, module) — 文件导入了模块
        - calls(file, caller, callee) — 文件中存在函数调用
        - inherits(class_a, class_b) — 类继承关系
        - depends_on(file_a, file_b) — 文件依赖关系
    """

    def __init__(self) -> None:
        self.engine = DatalogEngine()

    def extract_from_file(self, file_path: str | Path) -> int:
        """从单个 Python 文件提取事实。

        Returns:
            提取的事实数量。
        """
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            return 0

        count = 0
        rel_path = str(path)

        # 文件存在
        self.engine.assert_fact("file", rel_path)
        count += 1

        for node in ast.walk(tree):
            # 函数/方法定义
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.engine.assert_fact("defines", rel_path, node.name, "function")
                count += 1
                # 装饰器
                for dec in node.decorator_list:
                    dec_name = self._get_name(dec)
                    if dec_name:
                        self.engine.assert_fact("decorates", dec_name, node.name)
                        count += 1

            # 类定义
            elif isinstance(node, ast.ClassDef):
                self.engine.assert_fact("defines", rel_path, node.name, "class")
                count += 1
                # 继承关系
                for base in node.bases:
                    base_name = self._get_name(base)
                    if base_name:
                        self.engine.assert_fact("inherits", node.name, base_name)
                        count += 1

            # 导入
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.engine.assert_fact("imports", rel_path, alias.name)
                    count += 1
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    full = f"{module}.{alias.name}" if module else alias.name
                    self.engine.assert_fact("imports", rel_path, full)
                    count += 1

            # 函数调用
            elif isinstance(node, ast.Call):
                callee = self._get_name(node.func)
                if callee:
                    self.engine.assert_fact("calls", rel_path, "?", callee)
                    count += 1

        return count

    def extract_from_directory(self, directory: str | Path) -> int:
        """从目录中所有 Python 文件提取事实。"""
        total = 0
        dir_path = Path(directory)
        for py_file in dir_path.rglob("*.py"):
            if ".venv" in str(py_file) or "__pycache__" in str(py_file):
                continue
            total += self.extract_from_file(py_file)
        return total

    def to_engine(self) -> DatalogEngine:
        """返回填充了事实的推理引擎。"""
        return self.engine

    @staticmethod
    def _get_name(node: ast.expr) -> str:
        """从 AST 节点提取名称。"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{FactExtractor._get_name(node.value)}.{node.attr}"
        return ""
