"""代码沙箱 — 安全执行用户提供的 Python 代码或 Shell 命令。"""

from __future__ import annotations

import asyncio
import builtins
import sys
from io import StringIO
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool


class PythonSandboxTool(AbstractTool):
    """Python 代码沙箱执行工具。

    使用受限的 exec 环境，限制:
        - 禁止导入某些危险模块 (os, subprocess, shutil, sys)
        - 限制执行时间
        - 捕获 stdout/stderr
    """

    name = "python_sandbox"
    description = (
        "Execute Python code in a sandboxed environment. "
        "The code runs with restricted globals and has a time limit. "
        "Returns stdout output and any errors."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default: 30).",
            },
        },
        "required": ["code"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 60

    # 允许的安全内置函数和模块
    SAFE_BUILTINS: set[str] = {
        "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable",
        "chr", "complex", "dict", "dir", "divmod", "enumerate", "filter",
        "float", "format", "frozenset", "getattr", "hasattr", "hash",
        "hex", "int", "isinstance", "issubclass", "iter", "len",
        "list", "map", "max", "min", "next", "object", "oct", "ord",
        "pow", "print", "range", "repr", "reversed", "round",
        "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
        "True", "False", "None", "Exception", "ValueError", "TypeError",
        "KeyError", "IndexError", "StopIteration",
    }

    ALLOWED_IMPORTS: set[str] = {
        "math", "json", "re", "datetime", "collections", "itertools",
        "functools", "typing", "dataclasses", "enum", "copy",
        "textwrap", "string", "decimal", "fractions", "statistics",
        "random", "hashlib", "base64", "uuid", "pathlib",
        "unittest", "doctest",
    }

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def execute(self, **kwargs: Any) -> ToolResult:
        code = kwargs["code"]
        timeout = kwargs.get("timeout", self.timeout)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, code),
                timeout=timeout,
            )
        except TimeoutError:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Code execution timed out after {timeout}s",
            )

    def _execute_sync(self, code: str) -> ToolResult:
        stdout_capture = StringIO()
        stderr_capture = StringIO()

        # 构建受限的全局命名空间。
        # 注意：模块被 import 时 `__builtins__` 是 dict 而非 module，对其用
        # getattr 会抛 AttributeError 导致沙箱在生产中必然崩溃。这里显式引用
        # `builtins` 模块以保证白名单内置函数稳定可取。
        safe_builtins: dict[str, Any] = {}
        for name in self.SAFE_BUILTINS:
            if hasattr(builtins, name):
                safe_builtins[name] = getattr(builtins, name)
        # 注入受控的 __import__：import 语句经此走白名单，非白名单模块被拒，
        # 同时让 import 语句对放行模块真正可用（而非依赖预导入的全局名）。
        safe_builtins["__import__"] = self._safe_import

        restricted_globals: dict[str, Any] = {"__builtins__": safe_builtins}

        # 预导入允许的模块（保留：即便不写 import 也能直接使用模块名）
        for mod_name in self.ALLOWED_IMPORTS:
            try:
                restricted_globals[mod_name] = __import__(mod_name)
            except ImportError:
                pass

        try:
            # 替换 stdout/stderr
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            try:
                compiled = compile(code, "<sandbox>", "exec")
                exec(compiled, restricted_globals)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

            stdout_text = stdout_capture.getvalue()
            stderr_text = stderr_capture.getvalue()

            return ToolResult(
                tool_name=self.name,
                success=True,
                output={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "result": restricted_globals.get("result"),
                },
                error=stderr_text if stderr_text else None,
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"{type(e).__name__}: {e}",
                output={
                    "stdout": stdout_capture.getvalue(),
                    "stderr": stderr_capture.getvalue(),
                },
            )

    def _safe_import(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """安全的 import 函数 — 只允许白名单模块。"""
        top_level = name.split(".")[0]
        if top_level not in self.ALLOWED_IMPORTS:
            raise ImportError(f"Import of '{name}' is not allowed in sandbox")
        return __import__(name, *args, **kwargs)
