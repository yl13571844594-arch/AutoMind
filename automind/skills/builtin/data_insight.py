"""数据洞察技能 —— 读取 CSV/Excel，做描述性统计与相关性分析，输出结论。

在 ``excel_report``（汇总）的基础上更进一步：识别数值列，计算均值/中位数/
标准差/分位数，并对数值列两两算皮尔逊相关系数，找出最相关的列对，最后
输出"数据概览 + 关键发现"；可选调用 ``chart_tool`` 把第一对高相关列画成散点图。
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class DataInsightInput(BaseModel):
    path: str
    output: str = ""  # 结论报告路径（.md，留空则只返回文本）
    chart: str = ""  # 可选：散点图输出路径（.png）
    max_rows: int = 5000


class DataInsightSkill(AbstractSkill):
    """对表格数据做统计分析与相关性洞察。"""

    name = "data_insight"
    description = "Analyze CSV/Excel data: descriptive stats, correlations and key findings"

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        inp = DataInsightInput(**input_data) if isinstance(input_data, dict) else input_data
        src = Path(inp.path).expanduser()
        try:
            headers, rows = self._load(src, inp.max_rows)
            if not headers:
                return SkillResult(success=False, error="表格为空或无表头")
            report, top_pair = self._analyze(src, headers, rows)

            artifacts: list[str] = []
            if inp.chart and top_pair and agent is not None and getattr(agent, "tool_registry", None):
                c1, c2 = top_pair
                x = self._col(rows, headers.index(c1))
                y = self._col(rows, headers.index(c2))
                cr = await agent.tool_registry.dispatch(
                    "chart_tool", action="scatter", x=x, y=y,
                    xlabel=c1, ylabel=c2, title=f"{c1} vs {c2}",
                    output=str(Path(inp.chart).expanduser()))
                if cr.success:
                    artifacts.append((cr.output or {}).get("output") or inp.chart)

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
        import csv
        if src.suffix.lower() == ".csv":
            with src.open("r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                headers = next(reader, [])
                rows = [list(r) for r in reader][:max_rows]
            return headers, rows
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
    def _col(rows: list[list[str]], idx: int) -> list[float]:
        out = []
        for r in rows:
            if idx >= len(r):
                continue
            try:
                out.append(float(str(r[idx]).replace(",", "")))
            except ValueError:
                continue
        return out

    def _analyze(self, src: Path, headers: list[str], rows: list[list[str]]):
        num_cols: dict[str, list[float]] = {}
        for i, h in enumerate(headers):
            col = self._col(rows, i)
            if len(col) >= len(rows) * 0.6:
                num_cols[h] = col

        lines = [f"# 数据洞察：{src.name}", "", f"数据行数：{len(rows)} · 数值列：{len(num_cols)}", ""]
        lines.append("## 数值列统计")
        lines.append("")
        lines.append("| 列 | 均值 | 中位数 | 标准差 | 最小 | 最大 |")
        lines.append("|----|------|--------|--------|------|------|")
        for h, col in num_cols.items():
            mean = statistics.mean(col)
            stdev = statistics.stdev(col) if len(col) > 1 else 0.0
            lines.append(f"| {h} | {mean:.2f} | {statistics.median(col):.2f} | "
                         f"{stdev:.2f} | {min(col):g} | {max(col):g} |")

        # 相关性 Top-1
        top_pair = None
        if len(num_cols) >= 2:
            names = list(num_cols.keys())
            best = (-1.0, None)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    a, b = num_cols[names[i]], num_cols[names[j]]
                    n = min(len(a), len(b))
                    if n < 3:
                        continue
                    r = self._pearson(a[:n], b[:n])
                    if r is not None and abs(r) > best[0]:
                        best = (abs(r), (names[i], names[j], r))
            if best[1]:
                c1, c2, r = best[1]
                top_pair = (c1, c2)
                lines.append("")
                lines.append("## 关键发现")
                lines.append(f"- 相关性最强的列对：**{c1}** 与 **{c2}**（r = {r:.3f}）"
                             f"{'，显著正相关' if r > 0.5 else '，弱相关或负相关'}")
        return "\n".join(lines) + "\n", top_pair

    @staticmethod
    def _pearson(a: list[float], b: list[float]) -> float | None:
        n = len(a)
        ma, mb = statistics.mean(a), statistics.mean(b)
        cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        sa = (sum((x - ma) ** 2 for x in a)) ** 0.5
        sb = (sum((y - mb) ** 2 for y in b)) ** 0.5
        if sa == 0 or sb == 0:
            return None
        return cov / (sa * sb)
