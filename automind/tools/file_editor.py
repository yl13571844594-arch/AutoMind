"""文件编辑工具 — 多文件协同修改、diff 追踪、原子回滚。"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool


class _RootGuard:
    """路径穿越防护 — 将文件路径限定在 project_root 之内。

    安全语义：
        - ``project_root=None`` 时不做任何限制（向后兼容：库/测试中默认构造的
          工具行为与升级前完全一致）。
        - 设置 ``project_root`` 后，相对路径基于 root 解析，任何解析到 root 之外
          （含 ``..`` 穿越、符号链接逃逸）的路径都会抛出 ``PermissionError``。

    采用 ``resolved == root or root in resolved.parents`` 的严格包含判断，
    避免 ``str.startswith`` 的前缀碰撞漏洞（如 ``/srv/app`` 与 ``/srv/app-evil``）。
    """

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._root: Path | None = (
            Path(project_root).resolve() if project_root is not None else None
        )

    def resolve(self, path: str | Path) -> Path:
        """解析并校验路径；越界时抛出 PermissionError。"""
        p = Path(path)
        if self._root is None:
            return p
        if not p.is_absolute():
            p = self._root / p
        resolved = p.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise PermissionError(
                f"路径越界：'{path}' 解析到 project_root 之外，已拒绝访问。"
            )
        return resolved


class FileReadTool(AbstractTool):
    """文件读取工具。"""

    name = "file_read"
    description = "Read the contents of a file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read."},
            "encoding": {"type": "string", "description": "File encoding (default: utf-8)."},
        },
        "required": ["path"],
    }
    permission_tier = PermissionTier.SAFE

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._guard = _RootGuard(project_root)

    async def execute(self, **kwargs: Any) -> ToolResult:
        encoding = kwargs.get("encoding", "utf-8")
        try:
            path = self._guard.resolve(kwargs["path"])
        except PermissionError as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))
        try:
            content = path.read_text(encoding=encoding)
            return ToolResult(
                tool_name=self.name,
                success=True,
                output={"content": content, "path": str(path), "size": len(content)},
            )
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))


class FileWriteTool(AbstractTool):
    """文件写入工具。"""

    name = "file_write"
    description = "Write content to a file, creating or overwriting it."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write."},
            "content": {"type": "string", "description": "Content to write."},
            "encoding": {"type": "string", "description": "File encoding (default: utf-8)."},
        },
        "required": ["path", "content"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._guard = _RootGuard(project_root)

    async def execute(self, **kwargs: Any) -> ToolResult:
        content = kwargs["content"]
        encoding = kwargs.get("encoding", "utf-8")
        try:
            path = self._guard.resolve(kwargs["path"])
        except PermissionError as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            path.write_text(content, encoding=encoding)
            return ToolResult(
                tool_name=self.name,
                success=True,
                output={
                    "path": str(path),
                    "size": len(content),
                    "created": not existed,
                    "overwritten": existed,
                },
            )
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))


class FileEditTool(AbstractTool):
    """精确字符串替换编辑 — 支持 diff 追踪和回滚。

    使用精确匹配替换 (类似 Claude Code 的 Edit 工具)：
    提供 old_string 和 new_string，工具在文件中查找并替换。
    """

    name = "file_edit"
    description = (
        "Edit a file by replacing a specific string with another string. "
        "The old_string must match exactly once in the file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit."},
            "old_string": {"type": "string", "description": "The exact text to replace."},
            "new_string": {"type": "string", "description": "The text to replace it with."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default: false).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 45

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._guard = _RootGuard(project_root)
        self._backups: dict[str, str] = {}
        self._diff_history: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        old = kwargs["old_string"]
        new = kwargs["new_string"]
        replace_all = kwargs.get("replace_all", False)
        try:
            path = self._guard.resolve(kwargs["path"])
        except PermissionError as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        try:
            original = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        count = original.count(old)
        if count == 0:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_string not found in file: {old[:80]}...",
            )

        if not replace_all and count > 1:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_string found {count} times. Use replace_all=true or provide more context.",
            )

        modified = original.replace(old, new, -1 if replace_all else 1) if replace_all else original.replace(old, new, 1)

        # 备份原始内容
        self._backups[str(path)] = original

        # 计算 diff
        diff = self._compute_diff(original, modified, path.name)

        path.write_text(modified, encoding="utf-8")
        self._diff_history.append({
            "path": str(path),
            "diff": diff,
            "replacements": count if replace_all else 1,
        })

        return ToolResult(
            tool_name=self.name,
            success=True,
            output={
                "path": str(path),
                "diff": diff,
                "replacements": count if replace_all else 1,
            },
        )

    def rollback(self, path: str | Path) -> bool:
        """回滚文件到最后一次编辑前的状态。"""
        key = str(path)
        if key in self._backups:
            Path(key).write_text(self._backups[key], encoding="utf-8")
            del self._backups[key]
            return True
        return False

    def get_diff_history(self) -> list[dict[str, Any]]:
        return list(self._diff_history)

    @staticmethod
    def _compute_diff(original: str, modified: str, filename: str) -> str:
        diff_lines = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        ))
        return "".join(diff_lines)


class FileMultiEditTool(AbstractTool):
    """多文件协同编辑 — 在一次操作中修改多个文件。"""

    name = "file_multi_edit"
    description = "Edit multiple files in one operation. Each edit is a path + old_string + new_string triple."
    parameters = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
                "description": "List of file edits to apply.",
            },
        },
        "required": ["edits"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 50

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._edit_tool = FileEditTool(project_root)

    async def execute(self, **kwargs: Any) -> ToolResult:
        edits = kwargs["edits"]
        results = []
        all_success = True

        for edit in edits:
            result = await self._edit_tool.execute(**edit)
            results.append({
                "path": edit["path"],
                "success": result.success,
                "error": result.error,
                "output": result.output,
            })
            if not result.success:
                all_success = False

        return ToolResult(
            tool_name=self.name,
            success=all_success,
            output={"results": results, "total": len(edits), "succeeded": sum(1 for r in results if r["success"])},
            error=None if all_success else "Some edits failed",
        )

    def rollback_all(self) -> int:
        """回滚所有已编辑的文件。"""
        count = 0
        for path in list(self._edit_tool._backups.keys()):
            if self._edit_tool.rollback(path):
                count += 1
        return count
