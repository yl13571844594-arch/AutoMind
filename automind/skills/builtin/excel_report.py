"""Excel 报告技能 —— 读取表格数据、做汇总统计、生成 Markdown 报告。

复用 openpyxl / csv，把"原始表格"变成"人看得懂的数据摘要"，适合周报、
统计表、口径核对等场景。
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class ExcelReportInput(BaseModel):
    source: str
    output: str = ""  # 报告输出路径（.md，留空则只返回文本）
    title: str = "数据报告"
    max_rows: int = 1000


class ExcelReportSkill(AbstractSkill):
    """读取 Excel/CSV，输出汇总统计报告。"""

    name = "excel_report"
    description = "Summarize an Excel/CSV table into a Markdown report (rows, columns, numeric stats)"

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        inp = ExcelReportInput(**input_data) if isinstance(input_data, dict) else input_data
        src = Path(inp.source).expanduser()
        try:
            headers, rows = self._load(src, inp.max_rows)
            report = self._build_report(inp.title, src, headers, rows)
            artifacts: list[str] = []
            if inp.output:
                out = Path(inp.output).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(report, encoding="utf-8")
                artifacts.append(str(out))
            return SkillResult(success=True, output=report, artifacts=artifacts)
        except Exception as e:
            return SkillResult(success=False, error=str(e))

    @staticmethod
    def _load(src: Path, max_rows: int) -> tuple[list[str], list[list[str]]]:
        if src.suffix.lower() == ".csv":
            with src.open("r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                headers = next(reader, [])
                rows = [list(r) for r in reader][:max_rows]
            return headers, rows
        # Excel 路径（可选依赖 openpyxl）
        from automind.tools._toolkit import need
        need("openpyxl")
        import openpyxl
        wb = openpyxl.load_workbook(str(src), read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        headers = [str(h) for h in (next(it, []) or [])]
        rows = [[str(c) if c is not None else "" for c in r] for r in it][:max_rows]
        wb.close()
        return headers, rows

    @staticmethod
    def _build_report(title: str, src: Path, headers: list[str], rows: list[list[str]]) -> str:
        lines = [f"# {title}", "", f"来源：`{src}` · 数据行数：{len(rows)} · 列数：{len(headers)}", ""]
        if not headers:
            return "\n".join(lines) + "\n（表格为空）\n"
        lines.append("## 列概览")
        lines.append("")
        lines.append("| 列 | 非空数 | 类型 | 最小值 | 最大值 | 均值 |")
        lines.append("|----|--------|------|--------|--------|------|")
        for i, h in enumerate(headers):
            col = [r[i] for r in rows if i < len(r) and str(r[i]).strip() != ""]
            nonempty = len(col)
            nums = []
            for v in col:
                try:
                    nums.append(float(str(v).replace(",", "")))
                except ValueError:
                    pass
            if nums and len(nums) >= len(col) * 0.8:
                typ = "数值"
                stats_cell = (f"{min(nums):g} | {max(nums):g} | "
                              f"{statistics.mean(nums):.2f}")
            else:
                typ = "文本"
                stats_cell = "— | — | —"
            lines.append(f"| {h or '(空)'} | {nonempty} | {typ} | {stats_cell} |")
        return "\n".join(lines) + "\n"
