"""交付声明工具（v1.11.11）—— 让模型明确说"这些是交付物"。

与内置插件 ``delivery_manifest`` 的分工：
* ``delivery_manifest`` 是**自动记账**：按工具调用记录出清单，用于取证/防漏；
* ``present`` 是**模型显式声明**：把最重要的 1–2 个文件标成交付物，界面渲染
  成可点击打开的卡片。两者互补，不互相替代。

安全边界：只做路径与元数据检查，**不读取、不复制文件内容**；路径必须落在
项目目录或当前会话工作副本内（与文件工具的边界一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.core import session_ctx
from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, ok
from automind.tools.base import AbstractTool

logger = get_logger("automind.tool.present")

#: 每次最多声明几个交付物（再多用户不会看，也容易把重要文件淹没）
MAX_FILES = 8


def _normalize_files(files_in: Any) -> list[dict[str, Any]]:
    """把模型常见的几种 ``files`` 形态归一成 ``[{path, description?}]``。

    v1.11.13：实测模型会传 ``"['a.txt','b.txt']"`` 字符串、``["a.txt"]``、
    甚至 ``[{"file_path": ...}]`` —— 旧实现直接 ``第 1 个交付物不是对象``，
    一次本可成功的交付声明白跑。这里能确定的直接修好，确定不了的才报错。
    """
    import ast
    import json

    if isinstance(files_in, str):
        text = files_in.strip()
        if text.startswith("```"):
            import re
            text = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        parsed: Any = None
        if text[:1] in "[{":
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(text)
                    break
                except Exception:
                    continue
        files_in = parsed if parsed is not None else text
    if isinstance(files_in, dict):
        # {"files": [...]} 这种多包了一层
        files_in = files_in.get("files", [])
    if isinstance(files_in, str):
        files_in = [{"path": files_in}]
    if not isinstance(files_in, list):
        return []
    out: list[dict[str, Any]] = []
    for item in files_in:
        if isinstance(item, str):
            out.append({"path": item})
        elif isinstance(item, dict):
            path = ""
            for key in ("path", "file_path", "filepath", "file", "filename", "name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    path = value.strip()
                    break
            if not path:
                out.append(item)          # 保持原样，让下面统一报"缺少 path"
                continue
            entry = dict(item)
            entry["path"] = path
            desc = entry.get("description") or entry.get("desc")
            if desc:
                entry["description"] = str(desc)
            out.append(entry)
        else:
            out.append(item)
    return out


class PresentTool(AbstractTool):
    """声明已存在的文件为最终交付物。"""

    name = "present"
    description = (
        "Declare existing files as final deliverables for the user. Use it when "
        "the user needs a separate file (documents, spreadsheets, slide decks, "
        "reports, generated images); prefer your final response when that suffices. "
        "The tool records paths only — contents are neither read nor copied. "
        "Paths must be inside the project directory or the session workspace."
    )
    parameters = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": f"1-{MAX_FILES} deliverables, most important first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "File path (relative to project root/workspace)."},
                        "description": {"type": "string",
                                        "description": "One-line note shown to the user."},
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["files"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()

    def _roots(self) -> list[Path]:
        roots = [self.project_root]
        workspace = session_ctx.workspace()
        if workspace:
            try:
                roots.insert(0, Path(workspace).resolve())
            except Exception:                     # pragma: no cover - 防御性
                pass
        return roots

    def _resolve(self, raw: Any) -> tuple[Path | None, str]:
        """返回 ``(绝对路径, 错误说明)``；两者最多一个非空。"""
        text = str(raw or "").strip()
        if not text:
            return None, "files[].path 不能为空"
        p = Path(text).expanduser()
        roots = self._roots()
        if p.is_absolute():
            try:
                target = p.resolve()
            except OSError:
                return None, f"路径无法解析：{text}"
        else:
            target = None
            for root in roots:
                try:
                    candidate = (root / p).resolve()
                except OSError:
                    continue
                if candidate.exists():
                    target = candidate
                    break
            if target is None:
                return None, (f"找不到文件：{text}（相对路径按项目目录/会话工作副本解析；"
                              "请先用 file_write 等工具真实生成它）")
        if not any(target == r or r in target.parents for r in roots):
            return None, (f"路径超出允许范围：{target}。只能交付项目目录或当前会话"
                          "工作副本内的文件")
        if not target.exists():
            return None, f"文件不存在：{target}"
        if not target.is_file():
            return None, f"不是普通文件（目录/设备不能作为交付物）：{target}"
        return target, ""

    async def execute(self, **kwargs: Any) -> ToolResult:
        files_in = _normalize_files(kwargs.get("files"))
        if not files_in:
            return bad(self.name,
                       "files 必须是非空数组：可以传文件路径字符串，或 "
                       "[{path, description?}] 对象；例如 files=[\"报告.md\"]")
        if len(files_in) > MAX_FILES:
            return bad(self.name, f"一次最多声明 {MAX_FILES} 个交付物")
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for i, item in enumerate(files_in):
            if not isinstance(item, dict):
                return bad(self.name, f"第 {i + 1} 个交付物不是对象（应为 {{path, description?}}）")
            if not item.get("path"):
                return bad(self.name, f"第 {i + 1} 个交付物缺少 path")
            target, why = self._resolve(item.get("path"))
            if target is None:
                return bad(self.name, f"第 {i + 1} 个交付物无效：{why}")
            key = str(target)
            if key in seen:
                continue
            seen.add(key)
            entry = {"path": key, "name": target.name}
            desc = str(item.get("description") or "").strip()[:300]
            if desc:
                entry["description"] = desc
            out.append(entry)
        if not out:
            return bad(self.name, "没有有效的交付物（全部重复或无效）")
        # 事件失败不影响工具成功：声明已成立，界面下次拉取任务结果时仍能展示
        await session_ctx.emit_event({
            "type": "presented_files",
            "session_id": session_ctx.session_id() or "default",
            "files": out,
            "count": len(out),
        })
        return ok(self.name, files=out, count=len(out))

    def get_execution_plan(self, **kwargs: Any) -> str:
        files = kwargs.get("files")
        n = len(files) if isinstance(files, list) else 0
        return f"[present] 声明交付物（{n} 个文件）"
