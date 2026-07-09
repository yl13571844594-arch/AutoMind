"""文件编辑工具 — 多文件协同修改、diff 追踪、原子回滚。"""

from __future__ import annotations

import difflib
import time
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool


class FileChangeJournal:
    """全局文件改动日志 — 记录每次写入/编辑的前像，支持撤销回滚。

    进程级单例（模块属性 ``JOURNAL``），跨 Agent 重建仍保留，
    供 Web 层「↩️ 撤销/回滚」功能使用：
        - ``record(path, before, tool)``：工具写入前调用；before=None 表示新建文件。
        - ``entries()``：最近的改动列表（新→旧），前像不外传（可能很大/含敏感内容）。
        - ``rollback(path)``：把该文件恢复到**最早记录**的前像（新建则删除）。
        - ``rollback_all()``：按时间倒序恢复全部记录的改动。
    容量上限 200 条，超限丢弃最旧记录。
    """

    MAX_ENTRIES = 200

    def __init__(self) -> None:
        self._entries: list[dict] = []  # {path, before, tool, time, created}

    def record(self, path: str, before: str | None, tool: str) -> None:
        self._entries.append({
            "path": path, "before": before, "tool": tool,
            "time": time.strftime("%H:%M:%S"), "ts": time.time(),
            "created": before is None,
        })
        del self._entries[:-self.MAX_ENTRIES]

    def entries(self) -> list[dict]:
        return [
            {"path": e["path"], "tool": e["tool"], "time": e["time"],
             "created": e["created"]}
            for e in reversed(self._entries)
        ]

    def _restore(self, entry: dict) -> bool:
        p = Path(entry["path"])
        try:
            if entry["created"]:
                if p.exists():
                    p.unlink()
            else:
                p.write_text(entry["before"], encoding="utf-8")
            return True
        except Exception:
            return False

    def rollback(self, path: str) -> bool:
        """恢复单个文件到本日志中最早的前像（即撤销全部已记录改动）。"""
        matches = [e for e in self._entries if e["path"] == path]
        if not matches:
            return False
        ok = self._restore(matches[0])  # 最早的前像 = 改动前的原始内容
        if ok:
            self._entries = [e for e in self._entries if e["path"] != path]
        return ok

    def rollback_all(self) -> int:
        """按时间倒序恢复全部改动；返回成功恢复的文件数。"""
        restored: set[str] = set()
        count = 0
        for e in reversed(self._entries):
            if e["path"] in restored:
                continue
            first = next(x for x in self._entries if x["path"] == e["path"])
            if self._restore(first):
                restored.add(e["path"])
                count += 1
        self._entries = [e for e in self._entries if e["path"] not in restored]
        return count

    def clear(self) -> None:
        self._entries.clear()


#: 进程级改动日志单例（Web 层与全部文件工具共享）
JOURNAL = FileChangeJournal()


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
    """文件读取工具 — 支持按行分段读取，大文件自动截断保护上下文。"""

    name = "file_read"
    description = (
        "Read the contents of a file. For large files, pass offset/limit "
        "(1-based line number + line count) to read a specific range; "
        "oversized reads are truncated with a note."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read."},
            "encoding": {"type": "string", "description": "File encoding (default: utf-8)."},
            "offset": {"type": "integer",
                       "description": "Start line (1-based). Use with limit for large files."},
            "limit": {"type": "integer",
                      "description": "Max number of lines to return from offset."},
        },
        "required": ["path"],
    }
    permission_tier = PermissionTier.SAFE

    #: 无显式范围时的自动截断阈值 —— 整读超大文件会撑爆模型上下文，
    #: 反而降低后续编辑的准确度；截断并提示用 offset/limit 分段读。
    MAX_CHARS = 120_000
    TRUNC_LINES = 1500

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
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        total_size = len(content)
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        out: dict[str, Any] = {"path": str(path), "size": total_size,
                               "total_lines": total_lines}

        offset = kwargs.get("offset")
        limit = kwargs.get("limit")
        if offset or limit:
            start = max(int(offset or 1) - 1, 0)
            count = int(limit) if limit else self.TRUNC_LINES
            picked = lines[start:start + max(count, 1)]
            out["content"] = "".join(picked)
            out["range"] = f"lines {start + 1}-{start + len(picked)} of {total_lines}"
        elif total_size > self.MAX_CHARS:
            picked = lines[:self.TRUNC_LINES]
            out["content"] = "".join(picked)
            out["truncated"] = True
            out["note"] = (
                f"File is large ({total_size} chars, {total_lines} lines); "
                f"showing first {len(picked)} lines. Use offset/limit to read "
                f"the rest in ranges."
            )
        else:
            out["content"] = content
        return ToolResult(tool_name=self.name, success=True, output=out)


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
            # 撤销支持：写入前记录前像（新建文件记 None，回滚时删除）。
            # 已存在但前像读取失败（二进制/编码问题）时不记录 —— 无法安全恢复。
            if existed:
                try:
                    JOURNAL.record(str(path), path.read_text(encoding=encoding), self.name)
                except Exception:
                    pass
            else:
                JOURNAL.record(str(path), None, self.name)
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
        if old == new:
            return ToolResult(
                tool_name=self.name, success=False,
                error="old_string and new_string are identical — no-op edit. "
                      "Provide the changed text as new_string.",
            )
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
            hint = self._nearest_match_hint(original, old)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_string not found in file: {old[:80]}...{hint}",
            )

        if not replace_all and count > 1:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_string found {count} times. Use replace_all=true or provide more context.",
            )

        modified = original.replace(old, new, -1 if replace_all else 1) if replace_all else original.replace(old, new, 1)

        # 备份原始内容（工具级回滚 + 全局撤销日志）
        self._backups[str(path)] = original
        JOURNAL.record(str(path), original, self.name)

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
    def _nearest_match_hint(original: str, old: str, context_lines: int = 2) -> str:
        """定位文件中与 old_string 最接近的片段，附进错误信息。

        精确匹配失败最常见的原因是缩进/空白/引号差异——把文件里真实的
        近似片段回显给模型，下一轮即可用确切文本重试，而非盲目猜测。
        """
        old_first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if not old_first:
            return ""
        orig_lines = original.splitlines()
        # 先做「去空白后相等」的快速定位，再退化到模糊匹配
        idx = next((i for i, ln in enumerate(orig_lines) if ln.strip() == old_first), -1)
        if idx < 0:
            stripped = [ln.strip() for ln in orig_lines]
            close = difflib.get_close_matches(old_first, stripped, n=1, cutoff=0.6)
            if close:
                idx = stripped.index(close[0])
        if idx < 0:
            return ""
        n_old = max(len(old.splitlines()), 1)
        lo = max(0, idx - context_lines)
        hi = min(len(orig_lines), idx + n_old + context_lines)
        snippet = "\n".join(f"{i + 1}\t{orig_lines[i]}" for i in range(lo, hi))
        return (
            "\nNearest match in the file (line-numbered; check exact "
            f"whitespace/indentation and retry with the exact text):\n{snippet}"
        )

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
