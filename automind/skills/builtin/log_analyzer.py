"""日志分析技能（§14.8）— 解析日志、按级别统计、提取错误与高频模式。

纯标准库实现，无外部依赖，结果确定可测。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult

_LEVEL_RE = re.compile(
    r"\b(CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE)\b", re.IGNORECASE
)
_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning))\b")
# 归一化：去掉数字/十六进制/时间戳/引号内容，便于聚类同类日志
_NORMALIZE_SUBS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T][\d:.,]+"), "<TS>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b\d+\b"), "<N>"),
    (re.compile(r"([\"']).*?\1"), "<STR>"),
]


class LogAnalyzeInput(BaseModel):
    """日志分析输入。"""

    log_file: str = ""
    log_text: str = ""
    max_examples: int = 5
    report_file: str = ""  # 可选：输出 Markdown 报告路径


class LogAnalyzerSkill(AbstractSkill):
    """分析日志文本/文件，输出级别统计、错误样本与高频模式。"""

    name = "log_analyzer"
    description = "Analyze logs: level counts, error samples, top recurring patterns."
    input_schema = LogAnalyzeInput

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:  # noqa: ARG002
        inp = LogAnalyzeInput(**input_data) if isinstance(input_data, dict) else input_data

        text = inp.log_text
        if not text and inp.log_file:
            try:
                text = Path(inp.log_file).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return SkillResult(success=False, error=f"读取日志失败：{e}")
        if not text.strip():
            return SkillResult(success=False, error="日志内容为空")

        lines = text.splitlines()
        level_counts: Counter[str] = Counter()
        exception_counts: Counter[str] = Counter()
        pattern_counts: Counter[str] = Counter()
        error_examples: list[str] = []

        for line in lines:
            if not line.strip():
                continue
            m = _LEVEL_RE.search(line)
            level = self._canon_level(m.group(1)) if m else "OTHER"
            level_counts[level] += 1

            for ex in _EXCEPTION_RE.findall(line):
                exception_counts[ex] += 1

            if level in ("CRITICAL", "ERROR") and len(error_examples) < inp.max_examples:
                error_examples.append(line.strip()[:300])

            pattern_counts[self._normalize(line)] += 1

        total = sum(level_counts.values())
        errors = level_counts.get("ERROR", 0) + level_counts.get("CRITICAL", 0)
        warnings = level_counts.get("WARNING", 0)
        top_patterns = [
            {"pattern": p[:200], "count": c}
            for p, c in pattern_counts.most_common(inp.max_examples)
        ]

        summary = {
            "total_lines": total,
            "levels": dict(level_counts),
            "error_count": errors,
            "warning_count": warnings,
            "error_rate_pct": round(errors / max(total, 1) * 100, 1),
            "top_exceptions": [
                {"name": n, "count": c} for n, c in exception_counts.most_common(5)
            ],
            "error_examples": error_examples,
            "top_patterns": top_patterns,
        }

        artifacts: list[str] = []
        if inp.report_file:
            try:
                report = self._render_report(summary)
                path = Path(inp.report_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(report, encoding="utf-8")
                artifacts.append(str(path))
            except OSError as e:
                return SkillResult(success=False, error=f"写入报告失败：{e}", output=summary)

        return SkillResult(success=True, output=summary, artifacts=artifacts)

    @staticmethod
    def _canon_level(raw: str) -> str:
        u = raw.upper()
        if u == "FATAL":
            return "CRITICAL"
        if u == "WARN":
            return "WARNING"
        return u

    @staticmethod
    def _normalize(line: str) -> str:
        s = line.strip()
        for pat, repl in _NORMALIZE_SUBS:
            s = pat.sub(repl, s)
        return s

    @staticmethod
    def _render_report(summary: dict) -> str:
        lines = ["# 日志分析报告", "", f"- 总行数：{summary['total_lines']}",
                 f"- 错误：{summary['error_count']}（{summary['error_rate_pct']}%）",
                 f"- 警告：{summary['warning_count']}", "", "## 级别分布"]
        for lvl, c in summary["levels"].items():
            lines.append(f"- {lvl}: {c}")
        if summary["top_exceptions"]:
            lines += ["", "## 高频异常"]
            lines += [f"- {e['name']}: {e['count']}" for e in summary["top_exceptions"]]
        if summary["error_examples"]:
            lines += ["", "## 错误样本"]
            lines += [f"- `{ex}`" for ex in summary["error_examples"]]
        return "\n".join(lines) + "\n"
