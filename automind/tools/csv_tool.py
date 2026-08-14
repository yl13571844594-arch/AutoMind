"""CSV 工具 —— CSV 与 JSON 的结构化读写、转换与合并（纯标准库，无第三方依赖）。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, err, ok
from automind.tools.base import AbstractTool


class CsvTool(AbstractTool):
    """读写 CSV，并与 JSON 互相转换、多文件合并。"""

    name = "csv_tool"
    description = (
        "Read, write and convert CSV files. Actions: read (rows as list of dicts), "
        "write (create/overwrite CSV from a list of rows, header from keys), "
        "to_json (CSV -> JSON), from_json (JSON list of objects -> CSV), "
        "merge (combine multiple CSV files sharing the same header into one)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "to_json", "from_json", "merge"]},
            "path": {"type": "string", "description": "CSV file path."},
            "rows": {
                "type": "array", "items": {"type": "object"},
                "description": "Rows for write (list of dicts).",
            },
            "data": {
                "type": "array", "items": {"type": "object"},
                "description": "JSON list of objects for from_json.",
            },
            "output": {"type": "string", "description": "Output path for to_json/merge."},
            "inputs": {
                "type": "array", "items": {"type": "string"},
                "description": "Input CSV paths for merge.",
            },
            "encoding": {"type": "string", "description": "File encoding (default utf-8)."},
            "delimiter": {"type": "string", "description": "Field delimiter (default ',')."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 20

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        encoding = str(kwargs.get("encoding", "utf-8"))
        delimiter = str(kwargs.get("delimiter", ",")) or ","
        try:
            if action == "read":
                path = self._path(kwargs.get("path"))
                if not path.exists():
                    return bad(self.name, f"文件不存在：{path}")
                with path.open("r", encoding=encoding, newline="") as f:
                    rows = list(csv.DictReader(f, delimiter=delimiter))
                return ok(self.name, action=action, path=str(path),
                          count=len(rows), rows=rows)

            if action == "write":
                path = self._path(kwargs.get("path"))
                rows = kwargs.get("rows") or []
                if not isinstance(rows, list) or not rows:
                    return bad(self.name, "write 需要提供 rows（字典列表）")
                if not all(isinstance(r, dict) for r in rows):
                    return bad(self.name, "rows 每项必须是对象（dict）")
                fieldnames = list(rows[0].keys())
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding=encoding, newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
                    w.writeheader()
                    w.writerows(rows)
                return ok(self.name, action=action, path=str(path),
                          count=len(rows), message=f"已写入 {len(rows)} 行")

            if action == "to_json":
                path = self._path(kwargs.get("path"))
                out = self._path(kwargs.get("output"))
                if not path.exists():
                    return bad(self.name, f"文件不存在：{path}")
                with path.open("r", encoding=encoding, newline="") as f:
                    rows = list(csv.DictReader(f, delimiter=delimiter))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                return ok(self.name, action=action, path=str(path), output=str(out),
                          count=len(rows))

            if action == "from_json":
                path = self._path(kwargs.get("path"))
                data = kwargs.get("data")
                if data is None:
                    if not path.exists():
                        return bad(self.name, f"文件不存在：{path}")
                    data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, list) or not data:
                    return bad(self.name, "data 必须是非空的对象列表")
                fieldnames = list(data[0].keys())
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding=encoding, newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter)
                    w.writeheader()
                    w.writerows(data)
                return ok(self.name, action=action, path=str(path), count=len(data))

            if action == "merge":
                inputs = [self._path(p) for p in (kwargs.get("inputs") or [])]
                out = self._path(kwargs.get("output"))
                if len(inputs) < 2 or not out:
                    return bad(self.name, "merge 需要 inputs（至少两个 CSV）与 output 路径")
                merged: list[dict] = []
                header: list[str] | None = None
                for p in inputs:
                    if not p.exists():
                        return bad(self.name, f"文件不存在：{p}")
                    with p.open("r", encoding=encoding, newline="") as f:
                        for row in csv.DictReader(f, delimiter=delimiter):
                            if header is None:
                                header = list(row.keys())
                            merged.append(row)
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.open("w", encoding=encoding, newline="") as f:
                    w = csv.DictWriter(f, fieldnames=header or [], delimiter=delimiter)
                    w.writeheader()
                    w.writerows(merged)
                return ok(self.name, action=action, output=str(out), count=len(merged))

            return bad(self.name, f"未知 action：{action}")
        except Exception as e:
            return err(self.name, e)

    @staticmethod
    def _path(raw: Any) -> Path:
        if not raw:
            return Path()
        return Path(str(raw)).expanduser()
