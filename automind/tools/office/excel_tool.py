"""Excel 工具 —— 读写 .xlsx/.xlsm 与 CSV 互转。

社区版动作：read / write / append / sheets / create / to_csv / from_csv
专业版动作（office_pro）：style（字体/填充/边框/列宽/冻结表头）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, delegate_pro, err, need, ok
from automind.tools.base import AbstractTool

#: 进阶动作（由专业版 office_pro 实现，社区版仅转交）
PRO_ACTIONS = {"style"}


class ExcelTool(AbstractTool):
    """读写 Excel 工作簿。"""

    name = "excel_tool"
    description = (
        "Read and write Excel workbooks (.xlsx/.xlsm) and convert to/from CSV. "
        "Actions: read (cells to rows), write (rows to a sheet), append (add rows), "
        "sheets (list sheet names), create (new workbook), to_csv, from_csv. "
        "The style action (cell formatting) requires the Pro edition."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "append", "sheets", "create",
                         "to_csv", "from_csv", "style"],
                "description": "Operation to perform.",
            },
            "path": {"type": "string", "description": "Workbook path (.xlsx/.xlsm)."},
            "sheet": {"type": "string", "description": "Sheet name (default: active sheet)."},
            "rows": {
                "type": "array",
                "description": "Rows to write/append, each row an array of cell values.",
                "items": {"type": "array"},
            },
            "cell_range": {
                "type": "string",
                "description": "Range like 'A1:D20' for read. Omit to read the whole sheet.",
            },
            "max_rows": {"type": "number", "description": "Cap on rows returned by read (default 500)."},
            "csv_path": {"type": "string", "description": "CSV path for to_csv/from_csv."},
        },
        "required": ["action", "path"],
    }
    # 会写盘 → SENSITIVE；纯读动作在 execute 里不会造成破坏，但工具级别按最高动作定档
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 35

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        path = Path(str(kwargs.get("path", ""))).expanduser()
        try:
            if action in PRO_ACTIONS:
                need("openpyxl")
                return ok(self.name, **delegate_pro("office_pro", self.name, action, kwargs))
            openpyxl = need("openpyxl")

            if action == "create":
                return self._create(openpyxl, path, kwargs)
            if action == "sheets":
                return self._sheets(openpyxl, path)
            if action == "read":
                return self._read(openpyxl, path, kwargs)
            if action in ("write", "append"):
                return self._write(openpyxl, path, kwargs, append=(action == "append"))
            if action == "to_csv":
                return self._to_csv(openpyxl, path, kwargs)
            if action == "from_csv":
                return self._from_csv(openpyxl, path, kwargs)
            return bad(self.name, f"不支持的 action：{action}")
        except Exception as e:
            return err(self.name, e)

    # ── 具体动作 ──────────────────────────────────────────

    def _create(self, openpyxl: Any, path: Path, kw: dict) -> ToolResult:
        wb = openpyxl.Workbook()
        ws = wb.active
        if kw.get("sheet"):
            ws.title = str(kw["sheet"])
        for row in kw.get("rows") or []:
            ws.append(list(row))
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return ok(self.name, path=str(path), sheet=ws.title, rows=ws.max_row,
                  message=f"已创建工作簿 {path.name}")

    def _sheets(self, openpyxl: Any, path: Path) -> ToolResult:
        if not path.is_file():
            return bad(self.name, f"文件不存在：{path}")
        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            return ok(self.name, path=str(path), sheets=list(wb.sheetnames))
        finally:
            wb.close()

    def _read(self, openpyxl: Any, path: Path, kw: dict) -> ToolResult:
        if not path.is_file():
            return bad(self.name, f"文件不存在：{path}")
        cap = int(kw.get("max_rows") or 500)
        # data_only=True 取公式的**缓存计算值**；文件从未被 Excel 打开过时该值为
        # None，这属于 openpyxl 的固有限制，如实说明而不是假装读到了。
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[kw["sheet"]] if kw.get("sheet") else wb.active
            rng = kw.get("cell_range")
            cells = ws[rng] if rng else ws.iter_rows()
            rows, truncated = [], False
            for i, row in enumerate(cells):
                if i >= cap:
                    truncated = True
                    break
                rows.append([c.value for c in row])
            return ok(self.name, path=str(path), sheet=ws.title, rows=rows,
                      row_count=len(rows), truncated=truncated,
                      note=("公式单元格返回的是 Excel 上次保存时的缓存值；"
                            "若显示 None，说明该文件尚未被 Excel 计算过。"))
        finally:
            wb.close()

    def _write(self, openpyxl: Any, path: Path, kw: dict, append: bool) -> ToolResult:
        rows = kw.get("rows")
        if not rows:
            return bad(self.name, "write/append 需要提供 rows")
        if append and path.is_file():
            wb = openpyxl.load_workbook(path)
            ws = wb[kw["sheet"]] if kw.get("sheet") and kw["sheet"] in wb.sheetnames else wb.active
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            if kw.get("sheet"):
                ws.title = str(kw["sheet"])
        for row in rows:
            ws.append(list(row))
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return ok(self.name, path=str(path), sheet=ws.title, total_rows=ws.max_row,
                  written=len(rows),
                  message=f"已{'追加' if append else '写入'} {len(rows)} 行到 {path.name}")

    def _to_csv(self, openpyxl: Any, path: Path, kw: dict) -> ToolResult:
        import csv
        if not path.is_file():
            return bad(self.name, f"文件不存在：{path}")
        out = Path(str(kw.get("csv_path") or path.with_suffix(".csv"))).expanduser()
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[kw["sheet"]] if kw.get("sheet") else wb.active
            out.parent.mkdir(parents=True, exist_ok=True)
            # newline="" 是 csv 模块的硬性要求，否则 Windows 上每行会多出空行
            with open(out, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                n = 0
                for row in ws.iter_rows(values_only=True):
                    w.writerow(["" if v is None else v for v in row])
                    n += 1
            return ok(self.name, path=str(out), rows=n,
                      message=f"已导出 {n} 行到 {out.name}")
        finally:
            wb.close()

    def _from_csv(self, openpyxl: Any, path: Path, kw: dict) -> ToolResult:
        import csv
        src = Path(str(kw.get("csv_path") or "")).expanduser()
        if not src.is_file():
            return bad(self.name, f"CSV 不存在：{src}")
        wb = openpyxl.Workbook()
        ws = wb.active
        if kw.get("sheet"):
            ws.title = str(kw["sheet"])
        # utf-8-sig 顺带吃掉 Excel 导出的 BOM，避免首列表头带上
        with open(src, encoding="utf-8-sig", newline="") as f:
            n = 0
            for row in csv.reader(f):
                ws.append(row)
                n += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return ok(self.name, path=str(path), rows=n,
                  message=f"已从 {src.name} 导入 {n} 行")
