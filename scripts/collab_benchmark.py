#!/usr/bin/env python3
"""协同质量基准（Collab Benchmark）—— single / multi / subagent 三种执行模式的同任务对比。

**要回答的问题**：多智能体协同到底比单 Agent 好在哪里、又贵在哪里？

把"感觉多智能体更强"变成可复跑的数字：同一批任务分别用三种模式执行，比较
成功率、耗时、Token、LLM 调用次数、步数、验证通过率、返工次数与人工介入次数。
结果写 JSON + CSV，CSV 里留一列 ``manual_rework`` 由人工填写"这次交付我手工返工了吗"。

三种模式：

* ``single``   —— 单 Agent「工作模式」：``agent.run(prompt)``（Plan-and-Execute）。
* ``multi``    —— 多智能体「工程协同」：``agent.run_multi(prompt)``（需专业版激活；
                  未激活时该组合记为 ``unavailable``，不计入成功率分母）。
* ``subagent`` —— 父 Agent 派发一个受限子任务：``agent.run_subtask(...)``
                  （显式给写/执行工具白名单，``max_iteration_extensions=0`` 不自动续步）。

**默认 dry-run**：只打印"任务 × 模式"矩阵，不调用任何模型、不需要 API Key；
``--live`` 才真实跑（会产生真实 Token 费用），且必须能检测到模型凭据，
否则退出码 2 —— 绝不把"没跑成"记成"协同失败"。

诚实边界：

* **本脚本不内置任何真实基准数字**。仓库里没有提交过 benchmark 结果；文档里
  也不会写"多智能体提升 X%"这类没有数据支撑的话。要结论就自己跑 ``--live``。
* 本机/CI **不要**把 ``--live`` 接进自动化（真实 API Key + 计费 + 时长不可控）；
  CI 只跑 ``tests/planning/test_collab_benchmark_metrics.py`` 的 fake 指标聚合用例。
* ``human_interventions`` 统计"中途人工插话 / 审批回调"次数；无人值守基准默认
  ``approve_all``，无人介入时如实记 0（不猜、不补）。
* ``verification_passed`` 只反映**Agent 自己的验证闭环**；``artifacts_ok`` 是
  benchmark 侧"要求的文件是否真的存在于工作区"的独立检查。两者分开记。

用法见 ``docs/COLLABORATION.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 仓库根目录（本文件位于 ``scripts/`` 下一层）
ROOT = Path(__file__).resolve().parents[1]

#: 三种执行模式（顺序即矩阵/控制台展示顺序）
MODES: tuple[str, ...] = ("single", "multi", "subagent")

#: 默认输出目录（``.automind/`` 已被 .gitignore 忽略，结果不会误提交）
DEFAULT_OUT = ROOT / ".automind" / "collab_benchmark"

#: JSON 报告格式版本（结构变化时 +1，方便下游消费方判断）
SCHEMA = "automind.collab_benchmark/1"

#: CSV 固定列（顺序稳定；``manual_rework`` 由人工填写）
CSV_COLUMNS: tuple[str, ...] = (
    "task_id", "category", "mode", "status", "success", "artifacts_ok",
    "duration_ms", "tokens", "llm_calls", "steps", "verification_passed",
    "rework_count", "human_interventions", "manual_rework",
    "stop_reason", "error", "notes",
)

#: 退出码：0=跑完（失败/成功的结论看 success_rate）；2=用法/凭据问题；3=进程被中断
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_ABORTED = 3


# ═══════════════════════════════════════════════════════════════
# 固定任务集
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BenchmarkTask:
    """一个基准任务：提示词 + 可选预置文件 + 期望产物（benchmark 侧检查）。"""

    id: str
    category: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    #: 期望存在的工作区文件（相对路径）；空 = 纯文本任务，不做产物检查
    expect_files: tuple[str, ...] = ()
    #: 是否期望"存在可运行的测试并跑通"（仅用于文档/矩阵展示；真正判定靠
    #: verification_passed 与 artifacts_ok 两个独立字段）
    expect_tests: bool = False
    description: str = ""


#: 固定任务集：覆盖 分析类 / 小代码生成 / 小 bug 修复 / 多文件 + 验证 四类。
#: 任务之间不依赖，每个 (任务, 模式) 组合都在一份独立工作区里跑。
TASKS: tuple[BenchmarkTask, ...] = (
    BenchmarkTask(
        id="analysis_summary",
        category="analysis",
        prompt=(
            "这是一个纯文本分析任务，不要创建或修改任何文件。"
            "请把下面这段需求改写成一页可执行的验收清单（验收维度 + 每维怎么判定 + 优先级）：\n"
            "『做一个本地 Agent 工作台，支持对话、执行命令、读写文件，并能在长任务中途"
            "断开后继续。』\n"
            "输出用 Markdown，控制在 300 字以内；不要调用任何写文件或执行命令的工具。"
        ),
        description="纯文本分析：比组织与取舍能力，不产生文件（不做 artifacts 检查）",
    ),
    BenchmarkTask(
        id="codegen_fizzbuzz",
        category="codegen",
        prompt=(
            "在当前目录创建 fizzbuzz.py：实现函数 fizzbuzz(n) 返回 1..n 的 FizzBuzz 列表"
            "（3 的倍数用 'Fizz'、5 的倍数用 'Buzz'、15 的倍数用 'FizzBuzz'、其余为字符串数字）。"
            "同时创建 test_fizzbuzz.py（用内置 unittest 或普通 assert，至少 3 个用例），"
            "并运行测试确认全部通过。不要修改其它文件。"
        ),
        expect_files=("fizzbuzz.py", "test_fizzbuzz.py"),
        expect_tests=True,
        description="小代码生成：要求产出实现 + 测试并跑通",
    ),
    BenchmarkTask(
        id="bugfix_calc",
        category="bugfix",
        prompt=(
            "当前目录的 calc.py 里 add() 有 bug，直接运行 test_calc.py 会失败。"
            "请阅读这两个文件，修复 calc.py（只改实现，不要改测试文件），"
            "然后运行测试确认全部通过。"
        ),
        files={
            "calc.py": (
                "def add(a, b):\n"
                "    # BUG: 这里写成了减法\n"
                "    return a - b\n"
                "\n"
                "\n"
                "def mul(a, b):\n"
                "    return a * b\n"
            ),
            "test_calc.py": (
                "from calc import add, mul\n"
                "\n"
                "\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
                "    assert add(-1, 1) == 0\n"
                "\n"
                "\n"
                "def test_mul():\n"
                "    assert mul(3, 4) == 12\n"
            ),
        },
        expect_files=("calc.py",),
        expect_tests=True,
        description="小 bug 修复：预置失败测试，要求定位并修复实现",
    ),
    BenchmarkTask(
        id="multi_file_pkg",
        category="multi_file",
        prompt=(
            "在当前目录创建一个最小 Python 包：\n"
            "- pkg/__init__.py：导出 greet\n"
            "- pkg/core.py：greet(name) 返回 'Hello, <name>!'\n"
            "- pkg/cli.py：python -m pkg --name X 输出 greeting\n"
            "- pkg/__main__.py：让 python -m pkg 可用\n"
            "- tests/test_pkg.py：覆盖 greet 的测试\n"
            "最后运行测试确认全部通过。只创建上述文件。"
        ),
        expect_files=(
            "pkg/__init__.py", "pkg/core.py", "pkg/cli.py",
            "pkg/__main__.py", "tests/test_pkg.py",
        ),
        expect_tests=True,
        description="多文件 + 验证：跨文件一致性、包结构与入口",
    ),
)


# ═══════════════════════════════════════════════════════════════
# 结果行与指标
# ═══════════════════════════════════════════════════════════════


@dataclass
class BenchRow:
    """一次 (任务, 模式) 执行的指标行。

    ``status`` 有三种，汇总时必须区分（"跑不了"不是"模型退化"）：

    * ``ok``          —— 真的跑完了，``success`` 有含义；
    * ``unavailable`` —— 该模式在当前环境不可用（如 multi 未激活专业版）；
    * ``error``       —— 执行器/环境异常（建不起 agent、超时、外部故障）。
    """

    task_id: str
    category: str
    mode: str
    status: str = "ok"
    success: bool = False
    #: benchmark 侧产物检查：None = 该任务不检查产物 / 没跑到可检查的阶段
    artifacts_ok: bool | None = None
    duration_ms: int = 0
    tokens: int = 0
    llm_calls: int = 0
    steps: int = 0
    #: Agent 自己的验证闭环结论：None = 该模式没有验证环节 / 未采集
    verification_passed: bool | None = None
    rework_count: int = 0
    #: 中途人工介入（插话 + 审批回调）次数；无人值守基准通常为 0
    human_interventions: int = 0
    stop_reason: str = ""
    error: str = ""
    notes: str = ""


def _mean(values: list[float]) -> float | None:
    """平均值；空列表返回 None（**不返回 0** —— 0 会被误读成"测得是 0"）。"""
    return round(sum(values) / len(values), 2) if values else None


def _rate(values: list[bool]) -> float | None:
    """通过率；空列表返回 None（理由同上：没测量 ≠ 通过率 0%）。"""
    return round(sum(1 for v in values if v) / len(values), 4) if values else None


def _metric_block(rows: list[BenchRow]) -> dict[str, Any]:
    """一组 ``ok`` 行的汇总块（按模式复用同一口径）。"""
    successes = [bool(r.success) for r in rows]
    verified = [bool(r.verification_passed) for r in rows
                if r.verification_passed is not None]
    artifacts = [bool(r.artifacts_ok) for r in rows if r.artifacts_ok is not None]
    durations = [float(r.duration_ms) for r in rows]
    return {
        "runs": len(rows),
        "successes": sum(1 for s in successes if s),
        "success_rate": _rate(successes),
        "avg_duration_ms": _mean(durations),
        "tokens_total": sum(int(r.tokens) for r in rows),
        "tokens_avg": _mean([float(r.tokens) for r in rows]),
        "llm_calls_total": sum(int(r.llm_calls) for r in rows),
        "steps_total": sum(int(r.steps) for r in rows),
        "steps_avg": _mean([float(r.steps) for r in rows]),
        "verification_measured": len(verified),
        "verification_pass_rate": _rate(verified),
        "artifacts_measured": len(artifacts),
        "artifacts_pass_rate": _rate(artifacts),
        "rework_total": sum(int(r.rework_count) for r in rows),
        "human_interventions_total": sum(int(r.human_interventions) for r in rows),
    }


def aggregate_rows(rows: list[BenchRow]) -> dict[str, Any]:
    """把结果行聚合成总表 + 分模式表。

    口径（写进 JSON 的 ``summary.notes``，避免读者误读）：

    * 成功率 / 平均耗时等只统计 ``status == "ok"`` 的行 —— ``unavailable``
      （如未激活专业版的 multi）与 ``error``（环境/执行器故障）不是模型能力；
    * 验证通过率 / 产物通过率的分母是"真实测量到的行"（``None`` 不计入），
      没测到就是 ``None``，不是 0% 也不是 100%；
    * ``manual_rework`` 是人工回填列，脚本不猜、不聚合。
    """
    ok_rows = [r for r in rows if r.status == "ok"]
    by_mode = {mode: _metric_block([r for r in ok_rows if r.mode == mode])
               for mode in MODES}
    return {
        "total_runs": len(rows),
        "ok_runs": len(ok_rows),
        "unavailable_runs": sum(1 for r in rows if r.status == "unavailable"),
        "error_runs": sum(1 for r in rows if r.status == "error"),
        "overall": _metric_block(ok_rows),
        "by_mode": by_mode,
        "notes": [
            "成功率/平均耗时只统计 status=ok 的执行；unavailable/error 单独计数。",
            "verification_pass_rate / artifacts_pass_rate 只统计真实测量的行，"
            "未测量记为 null（不是 0）。",
            "manual_rework 由人工回填 CSV 后自行统计；脚本不推断。",
            "human_interventions=0 表示本次无人介入（无人值守审批），不是统计缺失。",
        ],
    }


def build_matrix(tasks: tuple[BenchmarkTask, ...] = TASKS,
                 modes: tuple[str, ...] = MODES) -> list[dict[str, Any]]:
    """任务 × 模式矩阵（dry-run 打印/写入用，不含任何指标）。"""
    return [
        {
            "task_id": t.id,
            "category": t.category,
            "mode": m,
            "description": t.description,
            "expect_files": list(t.expect_files),
            "expect_tests": bool(t.expect_tests),
        }
        for t in tasks for m in modes
    ]


# ═══════════════════════════════════════════════════════════════
# 从不同模式的返回值提取指标（纯函数，便于离线测试）
# ═══════════════════════════════════════════════════════════════


def _usage_total(usage: Any) -> int:
    try:
        total = getattr(usage, "total", None)
        if total is not None:
            return int(total or 0)
        return int(getattr(usage, "prompt_tokens", 0) or 0) + int(
            getattr(usage, "completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _usage_calls(usage: Any) -> int:
    try:
        return int(getattr(usage, "calls", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _llm_usage(agent: Any) -> Any:
    return getattr(getattr(agent, "llm", None), "usage", None)


def _verification_from_agent(agent: Any) -> bool | None:
    """从单 Agent 的自主闭环证据里取"验收是否通过"；没有证据返回 None。

    口径与 v1.11.21 的 ``_verify_state`` 一致：``available=False``（验收设施
    不可用）不算通过、也不算失败 —— 这里返回 None，交给汇总按"未测量"处理。
    """
    state = getattr(agent, "_verify_state", None)
    if not isinstance(state, dict):
        return None
    verify = state.get("verify")
    if not isinstance(verify, dict) or "done" not in verify:
        return None
    if verify.get("available") is False:
        return None
    return bool(verify.get("done"))


def extract_single_metrics(agent: Any, result: Any) -> dict[str, Any]:
    """从 ``agent.run()`` 的 ``AgentResult`` 提取指标（字段缺失时退化为 agent 侧）。"""
    usage = getattr(result, "token_usage", None)
    tokens = _usage_total(usage)
    if not tokens:
        tokens = _usage_total(_llm_usage(agent))
    llm_calls = int(getattr(result, "llm_calls", 0) or 0) or _usage_calls(
        _llm_usage(agent))
    return {
        "success": bool(getattr(result, "success", False)),
        "tokens": tokens,
        "llm_calls": llm_calls,
        "steps": int(getattr(result, "steps_executed", 0) or 0),
        "verification_passed": _verification_from_agent(agent),
        "rework_count": 0,          # 单 Agent 的闭环修复轮不暴露"返工角色"计数
        "stop_reason": str(getattr(result, "stop_reason", "") or ""),
        "error": str(getattr(result, "error", "") or "")[:500],
    }


def extract_multi_metrics(result: Any) -> dict[str, Any]:
    """从 ``agent.run_multi()`` 的 dict 提取指标（v1.11.22 编排器会带 metrics/verification）。"""
    data = result if isinstance(result, dict) else {}
    usage = data.get("token_usage")
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    verification = data.get("verification")
    verification_passed: bool | None = None
    if isinstance(verification, dict) and "done" in verification:
        verification_passed = bool(verification.get("done"))
    tokens = _usage_total(usage) or int(metrics.get("tokens") or 0)
    llm_calls = (_usage_calls(usage) or int(metrics.get("llm_calls") or 0)
                 or int(data.get("llm_calls") or 0))
    steps = len(data.get("steps") or []) or int(data.get("steps_executed") or 0)
    return {
        "success": bool(data.get("success", False)),
        "tokens": tokens,
        "llm_calls": llm_calls,
        "steps": steps,
        "verification_passed": verification_passed,
        "rework_count": int(metrics.get("rework_count") or 0),
        "stop_reason": str(data.get("stop_reason") or ""),
        "error": str(data.get("error") or "")[:500],
    }


def extract_subagent_metrics(agent: Any, result: Any) -> dict[str, Any]:
    """从 ``agent.run_subtask()`` 的 dict 提取指标。"""
    data = result if isinstance(result, dict) else {}
    tokens = int(data.get("tokens") or 0) or _usage_total(_llm_usage(agent))
    llm_calls = int(data.get("llm_calls") or 0) or _usage_calls(_llm_usage(agent))
    return {
        "success": bool(data.get("success", False)),
        "tokens": tokens,
        "llm_calls": llm_calls,
        "steps": int(data.get("steps") or 0),
        # 子任务只跑 ReAct，不进入审查/验收闭环 —— 没有这个指标就是 None
        "verification_passed": None,
        "rework_count": 0,
        "stop_reason": str(data.get("stop_reason") or ""),
        "error": str(data.get("error") or "")[:500],
    }


def _human_interventions(agent: Any) -> int:
    """人工介入次数：中途插话(applied) + 审批回调调用次数（best-effort）。

    无人值守基准默认 ``approve_all``，审批回调不会被调用 → 记 0。这里**不**
    把"没有计数设施"写成非零估计：统计数据宁可少，不能编。
    """
    total = 0
    report = getattr(agent, "interjection_report", None)
    if callable(report):
        try:
            total += int((report() or {}).get("applied", 0) or 0)
        except Exception:                       # 计数故障不能影响基准主流程
            pass
    total += int(getattr(agent, "_bench_approval_calls", 0) or 0)
    return total


# ═══════════════════════════════════════════════════════════════
# 工作区 / 产物检查
# ═══════════════════════════════════════════════════════════════


def prepare_workspace(workspace: Path, task: BenchmarkTask) -> Path:
    """建一份干净的任务工作区并写入预置文件（同名目录先删，保证可复现）。"""
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    for rel, content in (task.files or {}).items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return workspace


def check_artifacts(workspace: Path, task: BenchmarkTask) -> bool | None:
    """benchmark 侧产物检查：期望文件是否真实存在（无期望时返回 None）。"""
    expected = [rel for rel in (task.expect_files or ()) if rel]
    if not expected:
        return None
    return all((workspace / rel).is_file() for rel in expected)


# ═══════════════════════════════════════════════════════════════
# 输出：JSON / CSV / 控制台
# ═══════════════════════════════════════════════════════════════


def _csv_value(value: Any) -> str:
    """CSV 单元格：布尔用小写 true/false，None 留空便于人工筛选。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_csv(path: Path, rows: list[BenchRow]) -> Path:
    """写 CSV（固定列 + 人工回填列 ``manual_rework`` 恒为空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            data["manual_rework"] = ""          # 由人工填写：是/否 + 原因
            writer.writerow({k: _csv_value(data.get(k)) for k in CSV_COLUMNS})
    return path


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def build_payload(rows: list[BenchRow], *, dry_run: bool, provider: str = "",
                  model: str = "", tasks: tuple[BenchmarkTask, ...] = TASKS,
                  modes: tuple[str, ...] = MODES) -> dict[str, Any]:
    """组装 JSON 报告（dry-run 时 ``results`` 为空、``matrix`` 给出待跑组合）。"""
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": bool(dry_run),
        "provider": provider,
        "model": model,
        "modes": list(modes),
        "tasks": [
            {"id": t.id, "category": t.category,
             "description": t.description}
            for t in tasks
        ],
        "matrix": build_matrix(tasks, modes),
        "results": [asdict(r) for r in rows],
        "summary": aggregate_rows(rows),
        "notes": [
            "本文件不含任何预置/示例指标；dry_run=true 时 results 为空。",
            "真实结果只在 --live 跑出来，未提交进仓库；不要把 API Key 或结果提交到版本库。",
            "CSV 的 manual_rework 列由人工填写（是否手工返工 + 原因），脚本不代填。",
        ],
    }


def write_artifacts(out_dir: Path, payload: dict[str, Any],
                    rows: list[BenchRow]) -> tuple[Path, Path]:
    """写 ``collab_benchmark.json`` + ``collab_benchmark.csv``，返回两个路径。"""
    out_dir = Path(out_dir)
    json_path = write_json(out_dir / "collab_benchmark.json", payload)
    csv_path = write_csv(out_dir / "collab_benchmark.csv", rows)
    return json_path, csv_path


def _print_matrix(matrix: list[dict[str, Any]]) -> None:
    print("[dry-run] 协同质量基准矩阵（未调用模型、无真实指标）")
    for item in matrix:
        expects = []
        if item["expect_files"]:
            expects.append(f"files={len(item['expect_files'])}")
        if item["expect_tests"]:
            expects.append("tests=yes")
        print(f"  - {item['task_id']:<22} {item['mode']:<9} "
              f"{item['category']:<10} {' '.join(expects)}")
    task_count = len({item["task_id"] for item in matrix})
    mode_count = len({item["mode"] for item in matrix})
    print(f"[dry-run] 共 {len(matrix)} 个组合（任务 {task_count} × "
          f"模式 {mode_count}）；真实运行请加 --live（会消耗 Token）。")


def _print_summary(summary: dict[str, Any]) -> None:
    print("\n汇总（仅统计 status=ok 的执行）：")
    for mode in MODES:
        block = summary["by_mode"].get(mode) or {}
        print(f"  {mode:<9} runs={block.get('runs', 0):<3} "
              f"success_rate={block.get('success_rate')} "
              f"avg_ms={block.get('avg_duration_ms')} "
              f"tokens_total={block.get('tokens_total', 0)} "
              f"llm_calls_total={block.get('llm_calls_total', 0)} "
              f"verification_pass_rate={block.get('verification_pass_rate')} "
              f"rework_total={block.get('rework_total', 0)}")


# ═══════════════════════════════════════════════════════════════
# 真实执行（--live 才会走到这里）
# ═══════════════════════════════════════════════════════════════


class BenchmarkConfigError(RuntimeError):
    """基准配置/凭据问题 —— 属于"跑不了"，不是"模型能力差"。"""


async def _run_one(task: BenchmarkTask, mode: str, workroot: Path,
                   args: argparse.Namespace) -> BenchRow:
    """执行一个 (任务, 模式) 组合，返回指标行；任何异常都转成行而不是崩场。"""
    from automind.agent import AutoMindAgent
    from automind.core.edition import FeatureNotAvailable
    from automind.core.types import InteractionMode
    from automind.eval.executors import make_agent_config

    row = BenchRow(task_id=task.id, category=task.category, mode=mode)
    workspace = prepare_workspace(workroot / f"{task.id}-{mode}", task)
    started = time.monotonic()
    agent: Any = None
    try:
        config = make_agent_config(workspace, args.provider, args.model,
                                   args.approval_mode)
        config.execution.max_task_seconds = args.timeout
        agent = AutoMindAgent(config)
        agent.session_id = f"bench:{task.id}:{mode}"
        try:
            agent._interaction = InteractionMode.WORK
        except Exception:
            pass
        # 审批回调计数（无人值守 approve_all 下通常不会触发；触发即记一次人工介入）
        cb = getattr(agent, "approval_callback", None)
        if cb is not None:
            async def _counted(*a: Any, **kw: Any) -> Any:
                agent._bench_approval_calls = int(
                    getattr(agent, "_bench_approval_calls", 0)) + 1
                return await cb(*a, **kw)
            agent.approval_callback = _counted

        if mode == "single":
            invocation = agent.run(task.prompt)
        elif mode == "multi":
            invocation = agent.run_multi(task.prompt)
        else:
            invocation = agent.run_subtask(
                task.prompt,
                allowed_tools=_subagent_tools(agent),
                max_steps=args.role_max_steps,
                max_tokens=args.role_max_tokens,
                # 角色预算不自动续步（与工程编排器的角色预算一致）
                max_iteration_extensions=0,
            )
        result = await asyncio.wait_for(invocation, timeout=args.timeout)

        if mode == "single":
            row_metrics = extract_single_metrics(agent, result)
        elif mode == "multi":
            row_metrics = extract_multi_metrics(result)
        else:
            row_metrics = extract_subagent_metrics(agent, result)

        for key, value in row_metrics.items():
            setattr(row, key, value)
        row.artifacts_ok = check_artifacts(workspace, task)
        row.human_interventions = _human_interventions(agent)
        row.notes = f"workspace={workspace}"
    except FeatureNotAvailable as exc:
        row.status = "unavailable"
        row.error = f"该模式需要专业版激活：{exc}"
    except TimeoutError:
        row.status = "error"
        row.error = f"任务超时（>{args.timeout:g}s）"
        row.artifacts_ok = check_artifacts(workspace, task)
    except asyncio.CancelledError:
        raise
    except Exception as exc:                    # 环境/执行器故障：如实记录，不判模型
        row.status = "error"
        row.error = f"{type(exc).__name__}: {exc}"[:500]
        row.artifacts_ok = check_artifacts(workspace, task)
    finally:
        row.duration_ms = int((time.monotonic() - started) * 1000)
        close = getattr(agent, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass
    return row


def _subagent_tools(agent: Any) -> list[str]:
    """子任务模式显式给工具白名单：要能写文件、跑测试，否则四类任务跑不动。"""
    preferred = (
        "file_read", "file_write", "file_edit", "file_multi_edit", "file_list",
        "file_search", "file_glob", "terminal", "pwsh", "python_sandbox",
        "present", "web_search", "web_fetch",
    )
    try:
        available = set(agent.tool_registry.list_names())
    except Exception:
        return []
    return [name for name in preferred if name in available]


async def run_live(tasks: tuple[BenchmarkTask, ...], modes: tuple[str, ...],
                   args: argparse.Namespace) -> tuple[list[BenchRow], str, str]:
    """真实跑矩阵（需模型凭据）。返回 (rows, provider, model)。"""
    from automind.eval.executors import detect_llm_target

    target = detect_llm_target(args.provider, args.model)
    if not target.available:
        raise BenchmarkConfigError(target.reason)
    workroot = Path(args.workdir) if args.workdir else (DEFAULT_OUT / "work")
    workroot.mkdir(parents=True, exist_ok=True)
    rows: list[BenchRow] = []
    total = len(tasks) * len(modes)
    for idx, task in enumerate(tasks, 1):
        for mode in modes:
            print(f"[live] ({idx}/{len(tasks)}) {task.id} · {mode} ...",
                  flush=True)
            row = await _run_one(task, mode, workroot, args)
            rows.append(row)
            flag = "OK" if row.status == "ok" and row.success else row.status
            print(f"[live]   -> {flag}  {row.duration_ms}ms  "
                  f"tokens={row.tokens}  calls={row.llm_calls}  "
                  f"steps={row.steps}", flush=True)
    if len(rows) != total:                      # 防御性：理论上不会发生
        print(f"[live] 警告：期望 {total} 个组合，实际 {len(rows)} 个",
              file=sys.stderr)
    return rows, target.provider, target.model


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def _configure_console() -> None:
    """Windows GBK 控制台不能编码的字符降级为转义，别让基准脚本因图标崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def _select_tasks(task_ids: list[str] | None) -> tuple[BenchmarkTask, ...]:
    """按 ``--task`` 过滤；未知 id 直接报错（拼错任务名不该静默变成空矩阵）。"""
    if not task_ids:
        return TASKS
    known = {t.id: t for t in TASKS}
    missing = [tid for tid in task_ids if tid not in known]
    if missing:
        raise BenchmarkConfigError(
            f"--task 里的任务 id 不存在：{missing}；可选：{sorted(known)}")
    return tuple(known[tid] for tid in task_ids)


def _select_modes(raw: str | None) -> tuple[str, ...]:
    """按 ``--mode`` 过滤（逗号分隔）；未知模式报错。"""
    if not raw:
        return MODES
    wanted = [x.strip() for x in raw.split(",") if x.strip()]
    unknown = [m for m in wanted if m not in MODES]
    if unknown:
        raise BenchmarkConfigError(
            f"--mode 里的模式不存在：{unknown}；可选：{list(MODES)}")
    return tuple(m for m in MODES if m in set(wanted))   # 保持固定顺序


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python scripts/collab_benchmark.py",
        description="协同质量基准：single / multi / subagent 同任务对比"
                    "（默认 dry-run，不调用模型）")
    ap.add_argument("--live", action="store_true",
                    help="真实调用模型（消耗 Token；需要可用凭据）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印任务×模式矩阵、不执行（默认行为；与 --live 互斥）")
    ap.add_argument("--mode", default="",
                    help="只跑这些模式（逗号分隔）：single,multi,subagent")
    ap.add_argument("--task", default="",
                    help="只跑这些任务 id（逗号分隔）：见 --dry-run 输出的矩阵")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="JSON/CSV 输出目录（默认 .automind/collab_benchmark）")
    ap.add_argument("--workdir", default="",
                    help="工作区根目录（默认 <out>/work）")
    ap.add_argument("--json", action="store_true",
                    help="把报告 JSON 打到 stdout")
    ap.add_argument("--provider", default="", help="覆盖模型提供商")
    ap.add_argument("--model", default="", help="覆盖模型名")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="单个组合的超时秒数（默认 300）")
    ap.add_argument("--approval-mode", choices=["approve_all", "auto", "ask"],
                    default="approve_all",
                    help="无人值守审批模式（默认 approve_all，避免审批被拒导致假失败）")
    ap.add_argument("--role-max-steps", type=int, default=20,
                    help="subagent 模式子任务最大步数（默认 20）")
    ap.add_argument("--role-max-tokens", type=int, default=60_000,
                    help="subagent 模式子任务 token 预算（默认 60000）")
    return ap


def run_command(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.live and args.dry_run:
        print("--live 与 --dry-run 互斥：默认就是 dry-run。", file=sys.stderr)
        return EXIT_CONFIG
    try:
        tasks = _select_tasks([x.strip() for x in args.task.split(",")
                               if x.strip()] if args.task else None)
        modes = _select_modes(args.mode or None)
    except BenchmarkConfigError as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    out_dir = Path(args.out)

    if not args.live:
        matrix = build_matrix(tasks, modes)
        _print_matrix(matrix)
        payload = build_payload([], dry_run=True, tasks=tasks, modes=modes)
        json_path, csv_path = write_artifacts(out_dir, payload, [])
        print(f"[dry-run] 矩阵已写入 {json_path}")
        print(f"[dry-run] CSV 模板（仅表头）已写入 {csv_path}")
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"[live] 模型凭据检测中 … provider={args.provider or '(配置默认)'} "
          f"model={args.model or '(配置默认)'}")
    try:
        rows, provider, model = asyncio.run(run_live(tasks, modes, args))
    except BenchmarkConfigError as exc:
        # 关键行为：明确区分"跑不了"与"模型退化"，并给配置退出码
        print(f"无法运行基准：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return EXIT_ABORTED

    payload = build_payload(rows, dry_run=False, provider=provider,
                            model=model, tasks=tasks, modes=modes)
    json_path, csv_path = write_artifacts(out_dir, payload, rows)
    _print_summary(payload["summary"])
    print(f"\n结果已写入 {json_path}")
    print(f"结果已写入 {csv_path}（manual_rework 列请人工填写后再统计）")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    try:
        return run_command(argv)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return EXIT_ABORTED


if __name__ == "__main__":                       # pragma: no cover - CLI 入口
    raise SystemExit(main())
