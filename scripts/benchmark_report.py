#!/usr/bin/env python3
"""版本隔离测评报告聚合（v1.11.24）—— 校验 + 汇总 JSON/CSV/Markdown。

## 这个脚本回答什么

``scripts/edition_benchmark.py --live`` 会在三个**隔离版本**（community / pro /
enterprise）里跑同一套评测：``comprehensive`` 套件 6 个计分任务 × 3 次重复 +
协同基准（single/multi/subagent 矩阵）。本脚本**只读**它落盘的原始报告，做两件事：

1. **完整性校验（缺失即失败）**：
   * 6 任务 × 3 版本 × 3 重复 = 54 个评测 cell，一个都不能少；
   * 协作 cell（community 只有 single；pro/enterprise 三个模式）也要齐全；
   * 反向断言探针 ``assertion_probe`` 若存在，必须判为 passed —— 否则说明
     断言链路本身没在工作，这比"模型差"严重得多，必须报错而不是静默略过。
   任何缺失/重复/非法状态都会让本脚本以退出码 2 失败，并**不产出汇总**；
   绝不拿 0 或默认值补齐 —— 那等于编造数字。
2. **聚合**：把原始报告聚成 JSON（机器读）、CSV（表格/二次分析）、Markdown
   （人读）三份，口径与评测框架/协同基准保持一致：
   * 成功率只统计真正跑完的行（``status=ok`` / 评测 ``passed|failed|error`` 分开计）；
   * ``unavailable`` / ``error`` 不进成功率分母；
   * 未测量到的数值记为 ``null``（不是 0）；成本是评测框架的**估算**值。

## 脱敏（报告不得带出敏感信息）

汇总前对所有字符串做脱敏：API Key（sk-/AKIA/AIza/GitHub/Slack/Bearer）、
许可证全文（AMP2-...）、绝对路径（仓库目录 / 用户主目录 / 临时目录 / 本次
运行目录，以及 Windows 盘符与常见 POSIX 绝对路径）全部替换为占位符。
数字与任务/模型名称不受影响。

用法::

    python scripts/benchmark_report.py --run .automind/edition_benchmark
    python scripts/benchmark_report.py --run <run-dir> --out <report-dir>
    # 或由 edition_benchmark.py --report-only 转调
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 仓库根目录（本文件位于 ``scripts/`` 下一层）
ROOT = Path(__file__).resolve().parents[1]

#: 汇总报告格式版本（结构变化时 +1）
REPORT_SCHEMA = "automind.edition_benchmark_report/1"
#: 编排器（edition_benchmark.py）写出的原始数据格式版本
RAW_SCHEMA = "automind.edition_benchmark/1"

#: 三个被测版本（顺序即报告表格顺序）
EDITIONS: tuple[str, ...] = ("community", "pro", "enterprise")

#: 各版本允许的协同模式（community 无商业扩展，只有单 Agent）
COLLAB_MODES: dict[str, tuple[str, ...]] = {
    "community": ("single",),
    "pro": ("single", "multi", "subagent"),
    "enterprise": ("single", "multi", "subagent"),
}

#: 综合套件名与重复次数（任务书要求：6 任务 × 3 版本 × 3 重复）
EVAL_SUITE = "comprehensive"
EVAL_REPEATS = 3

#: comprehensive 套件的 6 个计分任务（与 automind/eval/suites/comprehensive.yml 对齐）
SCORING_TASKS: tuple[str, ...] = (
    "analysis_summary", "bugfix_calc", "multi_file_pkg",
    "data_csv", "doc_report", "honesty_safety",
)
#: 反向断言探针（expect_fail，不计分；它的通过意味着断言链路在工作）。
#: 校验时以报告里的 ``expect_fail`` 标志为准（不只看 id）：即使探针改名，
#: 只要标了 expect_fail 就一律排除在 6 个计分任务之外、单独体检、不计通过率。
PROBE_TASK = "assertion_probe"

#: 协同基准的 4 个任务（与 scripts/collab_benchmark.py 的 TASKS 对齐）
COLLAB_TASKS: tuple[str, ...] = (
    "analysis_summary", "codegen_fizzbuzz", "bugfix_calc", "multi_file_pkg",
)

#: 合法状态值
CASE_STATUSES: tuple[str, ...] = ("passed", "failed", "error")
COLLAB_STATUSES: tuple[str, ...] = ("ok", "unavailable", "error")

#: 退出码：0=校验通过并产出汇总；2=原始数据不完整/非法（不产出汇总）
EXIT_OK = 0
EXIT_INVALID = 2

#: 默认报告输出子目录名
DEFAULT_REPORT_DIR_NAME = "report"


# ═══════════════════════════════════════════════════════════════
# 脱敏
# ═══════════════════════════════════════════════════════════════

#: API Key / Token 形态（各类常见提供商；命中即替换）
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAMP\d{0,2}-[A-Za-z0-9_\-]{16,}"), "[REDACTED_LICENSE]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgh[poshr]_[A-Za-z0-9]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[A-Za-z0-9._\-/+]{8,}"),
     r"\1[REDACTED_API_KEY]"),
)

#: Windows 盘符绝对路径（``C:\...`` / ``D:/...``）
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>|*?\r\n]*")
#: UNC 路径（``\\server\share\...``）
_UNC_PATH_RE = re.compile(r"\\\\[A-Za-z0-9._-]+\\[^\s\"'<>|*?\r\n]*")
#: 常见 POSIX 绝对路径前缀（避免把普通 ``a/b`` 误伤）
_POSIX_PATH_RE = re.compile(
    r"(?<![\w.])/(?:home|Users|tmp|var|etc|opt|root|mnt|private)/[^\s\"'<>|]*")

_KEY_SOURCE_LABEL = "api_key_source"


def _known_roots(extra_paths: tuple[str, ...] = ()) -> tuple[tuple[str, str], ...]:
    """返回需要替换为占位符的"已知绝对路径根"，按长度降序（先长后短避免残缺）。"""
    candidates: dict[str, str] = {
        str(ROOT): "<REPO>",
        str(Path.home()): "<HOME>",
        str(Path(tempfile.gettempdir())): "<TMP>",
    }
    for index, extra in enumerate(extra_paths):
        text = str(extra or "").strip()
        if text:
            candidates[text] = f"<RUN{index + 1}>"
    # 路径分隔符统一比较：Windows 记录可能带反斜杠
    ordered = sorted(candidates.items(), key=lambda kv: len(kv[0]), reverse=True)
    return tuple((p, ph) for p, ph in ordered if p)


def redact_text(text: str, *, extra_paths: tuple[str, ...] = (),
                known_roots: tuple[tuple[str, str], ...] | None = None) -> str:
    """对单段文本脱敏：先替换已知绝对路径根，再兜底匹配路径/密钥形态。"""
    if not isinstance(text, str) or not text:
        return text
    roots = known_roots if known_roots is not None else _known_roots(extra_paths)
    out = text
    for raw, placeholder in roots:
        if raw and raw in out:
            out = out.replace(raw, placeholder)
        # 同一路径另一种分隔符写法（C:\a 与 C:/a 可能同时出现）
        alt = raw.replace("\\", "/") if "\\" in raw else raw.replace("/", "\\")
        if alt and alt != raw and alt in out:
            out = out.replace(alt, placeholder)
    out = _WIN_PATH_RE.sub("<ABS_PATH>", out)
    out = _UNC_PATH_RE.sub("<ABS_PATH>", out)
    out = _POSIX_PATH_RE.sub("<ABS_PATH>", out)
    for pattern, replacement in _SECRET_RULES:
        out = pattern.sub(replacement, out)
    return out


def redact_obj(value: Any, *, extra_paths: tuple[str, ...] = ()) -> Any:
    """递归脱敏（dict/list/str）；数字、bool、None 原样保留。"""
    known = _known_roots(extra_paths)
    if isinstance(value, str):
        return redact_text(value, extra_paths=extra_paths, known_roots=known)
    if isinstance(value, dict):
        return {redact_text(str(k), extra_paths=extra_paths, known_roots=known):
                redact_obj(v, extra_paths=extra_paths) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item, extra_paths=extra_paths) for item in value]
    return value


# ═══════════════════════════════════════════════════════════════
# 原始数据读取
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Issue:
    """一条校验问题。``level=error`` 会让报告失败（缺失即失败）。"""

    level: str                       # "error" | "warning"
    code: str
    message: str
    cell: str = ""

    def as_dict(self) -> dict[str, str]:
        data = {"level": self.level, "code": self.code, "message": self.message}
        if self.cell:
            data["cell"] = self.cell
        return data


@dataclass
class RawBundle:
    """从 ``<run>/raw/`` 读出的全部原始材料。"""

    run_dir: Path
    raw_dir: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    eval_reports: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    collab: dict[str, dict[str, Any]] = field(default_factory=dict)
    load_issues: list[Issue] = field(default_factory=list)


_EVAL_REP_RE = re.compile(r"^eval_rep(\d+)\.json$")


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    """读 JSON；失败返回 ``(None, 原因)``（调用方转成校验问题）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"读取失败：{exc}"
    except ValueError as exc:
        return None, f"不是合法 JSON：{exc}"
    if not isinstance(data, dict):
        return None, f"顶层不是对象（实际 {type(data).__name__}）"
    return data, ""


def _resolve_raw_dir(run_dir: Path) -> Path:
    """原始数据目录：优先 ``<run>/raw``；没有则把 ``<run>`` 本身当 raw（便于单拷）。"""
    candidate = run_dir / "raw"
    return candidate if candidate.is_dir() else run_dir


def load_raw(run_dir: str | Path) -> RawBundle:
    """扫描并读取原始报告；读取失败只记 issue，不抛（由校验统一报告）。"""
    root = Path(run_dir)
    raw_dir = _resolve_raw_dir(root)
    bundle = RawBundle(run_dir=root, raw_dir=raw_dir)

    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        data, error = _read_json(manifest_path)
        if data is not None:
            bundle.manifest = data
        else:
            bundle.load_issues.append(Issue(
                "error", "manifest_invalid", f"manifest.json {error}"))
    else:
        bundle.load_issues.append(Issue(
            "warning", "manifest_missing",
            "缺少 manifest.json（编排器元数据）；按默认口径校验（3 次重复）"))

    for edition in EDITIONS:
        edition_dir = raw_dir / edition
        if not edition_dir.is_dir():
            continue                              # 由校验报告"整个版本缺失"
        for path in sorted(edition_dir.glob("eval_rep*.json")):
            match = _EVAL_REP_RE.match(path.name)
            if not match:
                continue
            repeat = int(match.group(1))
            data, error = _read_json(path)
            if data is None:
                bundle.load_issues.append(Issue(
                    "error", "eval_report_unreadable",
                    f"评测原始报告无法解析：{error}",
                    cell=f"{edition}/rep{repeat}"))
            else:
                bundle.eval_reports[(edition, repeat)] = data

        collab_path = edition_dir / "collab_benchmark.json"
        if collab_path.is_file():
            data, error = _read_json(collab_path)
            if data is None:
                bundle.load_issues.append(Issue(
                    "error", "collab_report_unreadable",
                    f"协同基准原始报告无法解析：{error}", cell=edition))
            else:
                bundle.collab[edition] = data
    return bundle


# ═══════════════════════════════════════════════════════════════
# 校验（缺失即失败，不补齐、不猜）
# ═══════════════════════════════════════════════════════════════


def _case_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        return {}
    return {str(case.get("id")): case
            for case in cases if isinstance(case, dict) and case.get("id")}


def _probe_cases(report: dict[str, Any]) -> list[dict[str, Any]]:
    """报告里所有 ``expect_fail`` 的 case（反向断言探针，不计分）。"""
    cases = report.get("cases")
    if not isinstance(cases, list):
        return []
    return [case for case in cases
            if isinstance(case, dict) and case.get("expect_fail")]


def _scoring_case_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """只取计入分数的 case：**按 expect_fail 过滤**，探针永远不进通过率。"""
    cases = _case_map(report)
    return {cid: case for cid, case in cases.items()
            if not case.get("expect_fail")}


def validate(bundle: RawBundle, *, repeats: int = EVAL_REPEATS) -> list[Issue]:
    """校验完整性；返回全部问题（``error`` 只要有一条，报告即失败）。"""
    issues: list[Issue] = list(bundle.load_issues)
    repeats = max(1, int(repeats))
    for edition in EDITIONS:
        edition_dir = bundle.raw_dir / edition
        if not edition_dir.is_dir():
            issues.append(Issue("error", "missing_edition",
                                f"缺少版本目录 raw/{edition}（--live 是否跑过？）",
                                cell=edition))
            continue

        # ── 评测：3 次重复 × 6 个计分任务 ──
        for repeat in range(1, repeats + 1):
            cell = f"{edition}/rep{repeat}"
            report = bundle.eval_reports.get((edition, repeat))
            if report is None:
                issues.append(Issue(
                    "error", "missing_eval_report",
                    f"缺少第 {repeat} 次重复的评测报告（raw/{edition}/eval_rep{repeat}.json）",
                    cell=cell))
                continue
            cases = _case_map(report)
            raw_cases = report.get("cases")
            if not isinstance(raw_cases, list):
                issues.append(Issue(
                    "error", "eval_cases_missing",
                    "评测报告里没有 cases 列表（结构不符）", cell=cell))
                continue
            seen: set[str] = set()
            for case in raw_cases:
                cid = str((case or {}).get("id") or "") if isinstance(case, dict) else ""
                if not cid:
                    continue
                if cid in seen:
                    issues.append(Issue(
                        "error", "duplicate_task",
                        f"任务 '{cid}' 在报告里出现了多次", cell=f"{cell}/{cid}"))
                seen.add(cid)
            for task in SCORING_TASKS:
                case = cases.get(task)
                if case is None:
                    issues.append(Issue(
                        "error", "missing_task",
                        f"缺少计分任务 '{task}'（套件或执行被截断？）",
                        cell=f"{cell}/{task}"))
                    continue
                status = str(case.get("status") or "")
                if status not in CASE_STATUSES:
                    issues.append(Issue(
                        "error", "bad_case_status",
                        f"任务状态非法：'{status}'（允许 {CASE_STATUSES}）",
                        cell=f"{cell}/{task}"))
                if case.get("expect_fail"):
                    issues.append(Issue(
                        "error", "scoring_task_expect_fail",
                        "计分任务被标成 expect_fail —— 分组/套件可能被改错了",
                        cell=f"{cell}/{task}"))
            probes = _probe_cases(report)
            if not probes:
                issues.append(Issue(
                    "warning", "probe_missing",
                    "报告里没有 expect_fail 反向断言探针（6 任务校验不受影响，"
                    "但断言链路未被体检）", cell=cell))
            else:
                overlap = sorted({str(item.get("id")) for item in probes}
                                 & set(SCORING_TASKS))
                if overlap:
                    issues.append(Issue(
                        "error", "probe_is_scoring_task",
                        f"标了 expect_fail 的探针与计分任务 id 重叠：{overlap}；"
                        "计分任务不得带 expect_fail", cell=cell))
                failed_probes = [str(item.get("id") or "?") for item in probes
                                 if str(item.get("status") or "") != "passed"]
                if failed_probes:
                    issues.append(Issue(
                        "error", "probe_not_passed",
                        f"反向断言探针没有通过（expect_fail 任务应判 passed）："
                        f"{failed_probes} —— 说明断言链路/执行器没在工作，"
                        "这份报告不可信", cell=cell))
            probe_ids = {str(item.get("id")) for item in probes}
            probe_named = cases.get(PROBE_TASK)
            if probe_named is not None and not probe_named.get("expect_fail"):
                issues.append(Issue(
                    "error", "probe_flag_missing",
                    f"探针任务 {PROBE_TASK} 存在但没有 expect_fail 标志 —— "
                    "套件契约被改坏，无法判断它是否应计入通过率", cell=cell))
            unexpected = sorted(set(cases) - set(SCORING_TASKS) - probe_ids)
            if unexpected:
                issues.append(Issue(
                    "warning", "unexpected_task",
                    f"报告里有未纳入计分的任务：{unexpected}", cell=cell))

        # ── 协作矩阵：每个允许的模式 × 4 个任务 ──
        collab = bundle.collab.get(edition)
        if collab is None:
            issues.append(Issue(
                "error", "missing_collab_report",
                f"缺少协同基准报告（raw/{edition}/collab_benchmark.json）",
                cell=f"{edition}/collab"))
            continue
        rows = collab.get("results")
        if not isinstance(rows, list):
            issues.append(Issue(
                "error", "collab_results_missing",
                "协同基准报告里没有 results 列表（结构不符）",
                cell=f"{edition}/collab"))
            continue
        present: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (str(row.get("task_id") or ""), str(row.get("mode") or ""))
            if key in present:
                issues.append(Issue(
                    "error", "duplicate_collab_cell",
                    f"协作 cell {key[0]} × {key[1]} 出现了多次",
                    cell=f"{edition}/collab"))
            present[key] = row
        for task in COLLAB_TASKS:
            for mode in COLLAB_MODES[edition]:
                row = present.get((task, mode))
                if row is None:
                    issues.append(Issue(
                        "error", "missing_collab_cell",
                        f"缺少协作 cell：任务 '{task}' × 模式 '{mode}'",
                        cell=f"{edition}/{task}/{mode}"))
                    continue
                status = str(row.get("status") or "")
                if status not in COLLAB_STATUSES:
                    issues.append(Issue(
                        "error", "bad_collab_status",
                        f"协作 cell 状态非法：'{status}'（允许 {COLLAB_STATUSES}）",
                        cell=f"{edition}/{task}/{mode}"))
        expected_keys = {(task, mode) for task in COLLAB_TASKS
                         for mode in COLLAB_MODES[edition]}
        extra = sorted(set(present) - expected_keys)
        if extra:
            issues.append(Issue(
                "warning", "unexpected_collab_cell",
                f"协作报告里有不在本版本矩阵内的 cell：{extra}",
                cell=f"{edition}/collab"))
    return issues


# ═══════════════════════════════════════════════════════════════
# 聚合（只用真实数据；缺失记 null，不补 0）
# ═══════════════════════════════════════════════════════════════


def _num(value: Any) -> float | int | None:
    """把指标转成数字；None/bool/非数字 → None（不把"没测到"当 0）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _sum(values: list[Any]) -> float | int | None:
    numbers = [n for n in (_num(v) for v in values) if n is not None]
    if not numbers:
        return None
    total = sum(numbers)
    return int(total) if all(isinstance(n, int) for n in numbers) else round(total, 6)


def _mean(values: list[Any]) -> float | None:
    numbers = [float(n) for n in (_num(v) for v in values) if n is not None]
    return round(sum(numbers) / len(numbers), 2) if numbers else None


def _rate(values: list[bool]) -> float | None:
    return round(sum(1 for v in values if v) / len(values), 4) if values else None


def _eval_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    """单次评测报告 → 6 个计分任务的汇总（``expect_fail`` 探针单独记、不计分）。"""
    cases = _scoring_case_map(report)           # 按 expect_fail 过滤，探针不进分母
    scoring = [cases.get(task) for task in SCORING_TASKS]
    statuses = [str((case or {}).get("status") or "") for case in scoring]
    passed_flags = [status == "passed" for status in statuses]
    probes = _probe_cases(report)
    probe_statuses = [str(item.get("status") or "") for item in probes]
    probe_status = ""
    if probe_statuses:
        probe_status = ("passed" if all(item == "passed"
                                        for item in probe_statuses)
                        else next(item for item in probe_statuses
                                  if item != "passed"))
    return {
        "suite": str(report.get("suite") or ""),
        "model": str(report.get("model") or ""),
        "provider": str(report.get("provider") or ""),
        "case_count": len(SCORING_TASKS),
        "passed": sum(1 for flag in passed_flags if flag),
        "failed": sum(1 for status in statuses if status == "failed"),
        "errors": sum(1 for status in statuses if status == "error"),
        "missing": sum(1 for case in scoring if case is None),
        "pass_rate": _rate(passed_flags) if None not in scoring else None,
        "seconds": _sum([(case or {}).get("seconds") for case in scoring]),
        "total_tokens": _sum([(case or {}).get("total_tokens") for case in scoring]),
        "estimated_cost_usd": _sum(
            [(case or {}).get("estimated_cost_usd") for case in scoring]),
        "probe_status": probe_status,
        "probe_cases": [str(item.get("id") or "") for item in probes],
        "cases": {
            task: {
                "status": statuses[index],
                "passed": passed_flags[index],
                "seconds": _num((scoring[index] or {}).get("seconds")),
                "total_tokens": _num((scoring[index] or {}).get("total_tokens")),
                "estimated_cost_usd": _num(
                    (scoring[index] or {}).get("estimated_cost_usd")),
            }
            for index, task in enumerate(SCORING_TASKS)
            if scoring[index] is not None
        },
    }


def _aggregate_eval(bundle: RawBundle, *, repeats: int) -> dict[str, Any]:
    editions: dict[str, Any] = {}
    matrix: list[dict[str, Any]] = []
    for edition in EDITIONS:
        reports: list[dict[str, Any]] = []
        repeats_present = 0
        for repeat in range(1, repeats + 1):
            report = bundle.eval_reports.get((edition, repeat))
            if report is None:
                reports.append({"repeat": repeat, "present": False})
                continue
            summary = _eval_report_summary(report)
            summary["repeat"] = repeat
            summary["present"] = True
            reports.append(summary)
            repeats_present += 1
            for task in SCORING_TASKS:
                case = (summary.get("cases") or {}).get(task)
                if case is None:
                    continue
                matrix.append({
                    "edition": edition, "repeat": repeat, "task_id": task,
                    **case,
                })
        by_task: dict[str, Any] = {}
        for task in SCORING_TASKS:
            statuses = [((report.get("cases") or {}).get(task) or {}).get("status")
                        for report in reports if report.get("present")]
            pass_count = sum(1 for status in statuses if status == "passed")
            by_task[task] = {
                "measured": len(statuses),
                "passed": pass_count,
                "pass_rate": _rate([status == "passed" for status in statuses]),
                "statuses": statuses,
            }
        # 版本通过率 = 6 任务 × 已测重复里 passed 的 cell 占比（探针已按 expect_fail 排除）
        measured_flags = [bool(case.get("passed"))
                          for report in reports if report.get("present")
                          for case in (report.get("cases") or {}).values()]
        probe_statuses = [entry.get("probe_status") for entry in reports
                          if entry.get("present")]
        editions[edition] = {
            "repeats_expected": repeats,
            "repeats_present": repeats_present,
            "cases_measured": sum(len(report.get("cases") or {})
                                  for report in reports if report.get("present")),
            "reports": reports,
            "by_task": by_task,
            "pass_rate": _rate(measured_flags),
            "expected_cases": repeats * len(SCORING_TASKS),
            "probe_statuses": probe_statuses,
            "total_tokens": _sum([report.get("total_tokens") for report in reports
                                  if report.get("present")]),
            "estimated_cost_usd": _sum([report.get("estimated_cost_usd")
                                        for report in reports
                                        if report.get("present")]),
            "seconds": _sum([report.get("seconds") for report in reports
                             if report.get("present")]),
        }
    return {"suite": EVAL_SUITE, "repeats": repeats,
            "scoring_tasks": list(SCORING_TASKS),
            "probe": {"task_id": PROBE_TASK,
                      "selection": "按报告里的 expect_fail 标志过滤，不计分、"
                                   "不参与通过率；它必须判 passed（否则断言链路可疑）"},
            "editions": editions, "matrix": matrix}


def _collab_mode_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一个（版本, 模式）的指标块；口径与 scripts/collab_benchmark.py 一致。"""
    ok_rows = [row for row in rows if str(row.get("status") or "") == "ok"]
    verification = [bool(row.get("verification_passed")) for row in rows
                    if row.get("verification_passed") is not None]
    return {
        "runs": len(rows),
        "ok_runs": len(ok_rows),
        "unavailable_runs": sum(1 for row in rows
                               if str(row.get("status") or "") == "unavailable"),
        "error_runs": sum(1 for row in rows
                         if str(row.get("status") or "") == "error"),
        "successes": sum(1 for row in ok_rows if row.get("success")),
        "success_rate": _rate([bool(row.get("success")) for row in ok_rows]),
        "avg_duration_ms": _mean([row.get("duration_ms") for row in ok_rows]),
        "tokens_total": _sum([row.get("tokens") for row in ok_rows]),
        "llm_calls_total": _sum([row.get("llm_calls") for row in ok_rows]),
        "steps_total": _sum([row.get("steps") for row in ok_rows]),
        "verification_measured": len(verification),
        "verification_pass_rate": _rate(verification),
    }


def _aggregate_collab(bundle: RawBundle) -> dict[str, Any]:
    editions: dict[str, Any] = {}
    matrix: list[dict[str, Any]] = []
    for edition in EDITIONS:
        payload = bundle.collab.get(edition) or {}
        rows = [row for row in (payload.get("results") or [])
                if isinstance(row, dict)]
        by_mode = {mode: _collab_mode_block(
            [row for row in rows if str(row.get("mode") or "") == mode])
            for mode in COLLAB_MODES[edition]}
        keep = ("task_id", "category", "mode", "status", "success", "artifacts_ok",
                "duration_ms", "tokens", "llm_calls", "steps",
                "verification_passed", "rework_count", "human_interventions",
                "stop_reason", "error")
        for row in rows:
            matrix.append({"edition": edition,
                           **{key: row.get(key) for key in keep}})
        editions[edition] = {
            "modes": list(COLLAB_MODES[edition]),
            "tasks": list(COLLAB_TASKS),
            "expected_cells": len(COLLAB_TASKS) * len(COLLAB_MODES[edition]),
            "measured_cells": len(rows),
            "by_mode": by_mode,
            "total_runs": len(rows),
            "ok_runs": sum(1 for row in rows
                           if str(row.get("status") or "") == "ok"),
        }
    return {"tasks": list(COLLAB_TASKS),
            "modes_by_edition": {key: list(value)
                                 for key, value in COLLAB_MODES.items()},
            "editions": editions, "matrix": matrix}


def aggregate(bundle: RawBundle, *, repeats: int = EVAL_REPEATS) -> dict[str, Any]:
    """聚合出完整报告 payload（尚未脱敏/落盘）。"""
    manifest = bundle.manifest or {}
    eval_summary = _aggregate_eval(bundle, repeats=max(1, int(repeats)))
    collab_summary = _aggregate_collab(bundle)
    model = str(manifest.get("model") or "")
    provider = str(manifest.get("provider") or "")
    if not model:
        for (edition, repeat), report in bundle.eval_reports.items():
            if report.get("model"):
                model = str(report.get("model"))
                break
    if not provider:
        for report in bundle.eval_reports.values():
            if report.get("provider"):
                provider = str(report.get("provider"))
                break
    return {
        "schema": REPORT_SCHEMA,
        "source_schema": str(manifest.get("schema") or RAW_SCHEMA),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": bool(manifest.get("dry_run", False)),
        "model": model,
        "provider": provider,
        "scope": {
            "editions": list(EDITIONS),
            "eval": {"suite": EVAL_SUITE, "repeats": int(repeats),
                     "scoring_tasks": list(SCORING_TASKS),
                     "probe_task": PROBE_TASK,
                     "probe_rule": "按 expect_fail 过滤，不计分、不参与通过率"},
            "collab": {"tasks": list(COLLAB_TASKS),
                       "modes_by_edition": {key: list(value)
                                            for key, value in COLLAB_MODES.items()}},
        },
        "validation": {"ok": True, "errors": [], "warnings": []},
        "eval": eval_summary,
        "collab": collab_summary,
        "notes": [
            "所有数字均由原始报告聚合；缺失 cell 直接判失败，不用 0/默认值补齐。",
            "计分口径：6 个计分任务 × 3 次重复；"
            "assertion_probe 等 expect_fail 探针按 expect_fail 过滤、不计分、"
            "不参与通过率，只做断言链路体检（它必须判 passed）。",
            "评测成功率 = passed 任务数 / 6（按重复次数分别计算后聚合）；"
            "failed 与 error 分开计数（error 多为框架/环境问题，优先排查）。",
            "协作成功率只统计 status=ok 的行；unavailable（如未激活的 multi）/ "
            "error 不进分母。",
            "verification_pass_rate / artifacts_pass_rate 只统计真实测量到的行，"
            "未测量记 null（不是 0）。",
            "estimated_cost_usd 是评测框架按价格表估算的值，仅供成本控制，不等于账单。",
            "报告已对 API Key / 许可证 / 绝对路径做脱敏；不要回填任何未测得的数字。",
        ],
    }


# ═══════════════════════════════════════════════════════════════
# 输出：JSON / CSV / Markdown
# ═══════════════════════════════════════════════════════════════

_EVAL_CSV_COLUMNS: tuple[str, ...] = (
    "edition", "repeat", "task_id", "status", "passed", "seconds",
    "total_tokens", "estimated_cost_usd",
)
_COLLAB_CSV_COLUMNS: tuple[str, ...] = (
    "edition", "task_id", "mode", "status", "success", "duration_ms", "tokens",
    "llm_calls", "steps", "verification_passed", "stop_reason",
)


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_csv(path: Path, columns: tuple[str, ...],
               rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key)) for key in columns})
    return path


def _fmt_rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_markdown(payload: dict[str, Any]) -> str:
    """把聚合结果渲染成给人看的 Markdown（不含任何原始输出正文/路径）。"""
    validation = payload.get("validation") or {}
    errors = validation.get("errors") or []
    warnings = validation.get("warnings") or []
    lines: list[str] = [
        "# AutoMind 版本隔离测评报告（v1.11.24）",
        "",
        f"- 生成时间：{payload.get('generated_at', '')}",
        f"- 模型：{payload.get('provider', '')}/{payload.get('model', '') or '（未知）'}",
        f"- 范围：{len(EDITIONS)} 个版本 × {EVAL_SUITE} 套件 "
        f"{payload.get('scope', {}).get('eval', {}).get('repeats', EVAL_REPEATS)} 次重复"
        f" + 协同矩阵",
        f"- 校验：{'✅ 完整' if not errors else f'❌ 不完整（{len(errors)} 项错误）'}",
    ]
    if errors:
        lines += ["", "## 校验错误（缺失即失败，未产出汇总）", ""]
        lines += [f"- `{item.get('cell', '')}` {item.get('message', '')}"
                  for item in errors[:50]]
    if warnings:
        lines += ["", "## 校验警告", ""]
        lines += [f"- {item.get('message', '')}" for item in warnings[:20]]
    if not errors:
        lines += ["", "## 综合评测（6 个计分任务 × 3 次重复）", ""]
        eval_data = payload.get("eval") or {}
        tasks = eval_data.get("scoring_tasks") or list(SCORING_TASKS)
        header = "| 任务 | " + " | ".join(EDITIONS) + " |"
        lines += [header, "|" + "---|" * (len(EDITIONS) + 1)]
        for task in tasks:
            cells = []
            for edition in EDITIONS:
                block = ((eval_data.get("editions") or {}).get(edition) or {}).get(
                    "by_task", {}).get(task) or {}
                passed = block.get("passed")
                measured = block.get("measured")
                if passed is None or not measured:
                    cells.append("—")
                else:
                    cells.append(f"{passed}/{measured}（{_fmt_rate(block.get('pass_rate'))}）")
            lines.append(f"| {task} | " + " | ".join(cells) + " |")
        lines += ["", "### 版本汇总", "",
                  "| 版本 | 通过率 | 已测重复 | 耗时(s) | Token | 估算成本($) |",
                  "|---|---|---|---|---|---|"]
        for edition in EDITIONS:
            block = (eval_data.get("editions") or {}).get(edition) or {}
            lines.append(
                f"| {edition} | {_fmt_rate(block.get('pass_rate'))} | "
                f"{_fmt_num(block.get('repeats_present'))}/"
                f"{_fmt_num(block.get('repeats_expected'))} | "
                f"{_fmt_num(block.get('seconds'))} | "
                f"{_fmt_num(block.get('total_tokens'))} | "
                f"{_fmt_num(block.get('estimated_cost_usd'))} |")
        probe_note = f"反向断言探针 {PROBE_TASK}（expect_fail，不计分）状态："
        probe_bits = []
        for edition in EDITIONS:
            block = (eval_data.get("editions") or {}).get(edition) or {}
            statuses = [status or "（缺失）"
                        for status in (block.get("probe_statuses") or [])]
            probe_bits.append(f"{edition}={'/'.join(statuses) if statuses else '—'}")
        lines += ["", probe_note + "；".join(probe_bits) + "（passed 才算断言链路正常）"]
        lines += ["", "## 协同矩阵（single / multi / subagent）", "",
                  "| 版本 | 模式 | 运行 | OK | 成功率 | 平均耗时(ms) | Token | LLM 调用 |",
                  "|---|---|---|---|---|---|---|---|"]
        collab = payload.get("collab") or {}
        for edition in EDITIONS:
            block = (collab.get("editions") or {}).get(edition) or {}
            for mode in (block.get("modes") or []):
                stat = (block.get("by_mode") or {}).get(mode) or {}
                lines.append(
                    f"| {edition} | {mode} | {_fmt_num(stat.get('runs'))} | "
                    f"{_fmt_num(stat.get('ok_runs'))} | "
                    f"{_fmt_rate(stat.get('success_rate'))} | "
                    f"{_fmt_num(stat.get('avg_duration_ms'))} | "
                    f"{_fmt_num(stat.get('tokens_total'))} | "
                    f"{_fmt_num(stat.get('llm_calls_total'))} |")
    lines += ["", "## 说明", ""]
    lines += [f"- {note}" for note in (payload.get("notes") or [])]
    lines.append("")
    return "\n".join(lines)


def write_outputs(out_dir: str | Path, payload: dict[str, Any]) -> dict[str, Path]:
    """写 summary.json / eval_matrix.csv / collab_matrix.csv / report.md。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / "summary.json"
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    eval_csv = _write_csv(directory / "eval_matrix.csv", _EVAL_CSV_COLUMNS,
                          (payload.get("eval") or {}).get("matrix") or [])
    collab_csv = _write_csv(directory / "collab_matrix.csv", _COLLAB_CSV_COLUMNS,
                            (payload.get("collab") or {}).get("matrix") or [])
    markdown_path = directory / "report.md"
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"summary": summary_path, "eval_csv": eval_csv,
            "collab_csv": collab_csv, "markdown": markdown_path}


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════


def build_report(run_dir: str | Path, *, repeats: int = EVAL_REPEATS,
                 extra_paths: tuple[str, ...] = ()) -> tuple[dict[str, Any],
                                                              list[Issue]]:
    """读取 + 校验 + 聚合（返回 payload 与问题列表；payload 已脱敏）。"""
    bundle = load_raw(run_dir)
    issues = validate(bundle, repeats=repeats)
    payload = aggregate(bundle, repeats=repeats)
    payload["validation"] = {
        "ok": not any(issue.level == "error" for issue in issues),
        "errors": [issue.as_dict() for issue in issues if issue.level == "error"],
        "warnings": [issue.as_dict() for issue in issues
                     if issue.level == "warning"],
    }
    paths = [str(bundle.run_dir), str(Path(run_dir).resolve()), *extra_paths]
    payload = redact_obj(payload, extra_paths=tuple(paths))
    return payload, issues


def run_report(run_dir: str | Path, out_dir: str | Path | None = None, *,
               repeats: int = EVAL_REPEATS) -> tuple[int, dict[str, Any]]:
    """校验并产出汇总。返回 ``(退出码, 摘要)``；不完整时写 validation.json 后失败。"""
    payload, issues = build_report(run_dir, repeats=repeats)
    errors = [issue for issue in issues if issue.level == "error"]
    directory = Path(out_dir) if out_dir else Path(run_dir) / DEFAULT_REPORT_DIR_NAME
    if errors:
        directory.mkdir(parents=True, exist_ok=True)
        invalid_path = directory / "validation.json"
        invalid_path.write_text(json.dumps({
            "schema": REPORT_SCHEMA,
            "ok": False,
            "errors": [issue.as_dict() for issue in errors],
            "warnings": [issue.as_dict() for issue in issues
                         if issue.level == "warning"],
            "note": "原始数据不完整或非法：按任务书要求缺失即失败，未产出汇总，"
                    "也没有用默认值补齐任何数字。",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return EXIT_INVALID, {"ok": False, "errors": len(errors),
                              "validation": str(invalid_path)}
    outputs = write_outputs(directory, payload)
    return EXIT_OK, {"ok": True, "outputs": {key: str(path)
                                             for key, path in outputs.items()}}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/benchmark_report.py",
        description="版本隔离测评报告聚合与完整性校验（只读原始报告，不跑模型）")
    parser.add_argument("--run", default=str(
        ROOT / ".automind" / "edition_benchmark"),
        help="edition_benchmark.py 的输出目录（含 manifest.json 与 raw/）")
    parser.add_argument("--out", default="",
                        help="报告输出目录（默认 <run>/report）")
    parser.add_argument("--repeats", type=int, default=EVAL_REPEATS,
                        help=f"每次版本要求的重复次数（默认 {EVAL_REPEATS}）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    code, result = run_report(args.run, args.out or None, repeats=args.repeats)
    if code == EXIT_OK:
        print("校验通过；汇总已写入：")
        for key, path in result["outputs"].items():
            print(f"  {key}: {path}")
        return EXIT_OK
    print(f"原始数据不完整或非法：{result.get('errors', 0)} 项错误；"
          f"未产出汇总（详见 {result.get('validation', '')}）。", file=sys.stderr)
    return EXIT_INVALID


if __name__ == "__main__":                       # pragma: no cover - CLI 入口
    raise SystemExit(main())
