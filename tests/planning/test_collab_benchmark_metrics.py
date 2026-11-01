"""协同质量基准 ``scripts/collab_benchmark.py`` 的指标聚合回归测试（v1.11.22）。

这里**不调用真实模型**：只验证纯函数（任务矩阵、指标提取、汇总口径、JSON/CSV
输出）与 dry-run CLI。真实基准跑一次要花真实 Token，绝不能进 CI。

重点断言（对应任务书的验收点）：

* 固定任务集覆盖 分析 / 代码生成 / bug 修复 / 多文件验证 四类，模式恰好 3 种；
* 成功率只统计 ``status=ok`` 的行 —— ``unavailable`` / ``error`` 不计入分母
  （"没跑成"不等于"模型能力差"，这是评测脚本最容易撒谎的地方）；
* ``verification_passed`` / ``artifacts_ok`` 为 ``None`` 时**不**计入分母，
  空集合的通过率是 ``None`` 而不是 0%；
* CSV 列固定且 ``manual_rework`` 恒为空（由人工回填，脚本不猜）；
* dry-run 只写矩阵 + 空结果，不产生任何伪造指标。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = ROOT / "scripts" / "collab_benchmark.py"


def _load_benchmark_module():
    """把 ``scripts/collab_benchmark.py`` 当模块加载（scripts/ 不是 Python 包）。"""
    spec = importlib.util.spec_from_file_location(
        "automind_collab_benchmark_under_test", _MODULE_PATH)
    assert spec and spec.loader, f"无法加载 {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    # 先注册再执行：dataclass 在 Python 3.12 下需要能通过 __module__ 找到模块命名空间
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark_module()


def _row(**kwargs) -> object:
    """构造一行指标（默认值对标"跑完且成功"的一次执行）。"""
    defaults = dict(
        task_id="codegen_fizzbuzz", category="codegen", mode="single",
        status="ok", success=True, artifacts_ok=True, duration_ms=1000,
        tokens=1000, llm_calls=5, steps=8, verification_passed=True,
        rework_count=0, human_interventions=0, stop_reason="completed",
        error="", notes="",
    )
    defaults.update(kwargs)
    return benchmark.BenchRow(**defaults)


# ═══════════════════════════════════════════════════════════════
# 任务集 / 矩阵
# ═══════════════════════════════════════════════════════════════


def test_tasks_cover_required_categories():
    """至少 4 个任务，覆盖简报要求的四类场景。"""
    tasks = benchmark.TASKS
    assert len(tasks) >= 4
    categories = {t.category for t in tasks}
    assert {"analysis", "codegen", "bugfix", "multi_file"} <= categories
    # 每个任务都有提示词与说明；代码类任务都声明了期望产物
    for task in tasks:
        assert task.prompt.strip(), f"{task.id} 缺少 prompt"
        assert task.description.strip(), f"{task.id} 缺少 description"
    for task in tasks:
        if task.category in ("codegen", "bugfix", "multi_file"):
            assert task.expect_files, f"{task.id} 是代码/文件任务却没有期望产物"


def test_modes_are_the_three_required():
    assert benchmark.MODES == ("single", "multi", "subagent")


def test_matrix_covers_every_task_mode_pair_once():
    """矩阵 = 任务 × 模式，且 (task_id, mode) 不重复。"""
    matrix = benchmark.build_matrix()
    assert len(matrix) == len(benchmark.TASKS) * len(benchmark.MODES)
    pairs = [(item["task_id"], item["mode"]) for item in matrix]
    assert len(pairs) == len(set(pairs))
    for item in matrix:
        assert item["category"]
        assert isinstance(item["expect_files"], list)
        assert isinstance(item["expect_tests"], bool)


# ═══════════════════════════════════════════════════════════════
# 汇总口径
# ═══════════════════════════════════════════════════════════════


def test_aggregate_empty_rows_uses_none_not_zero():
    """空结果：通过率是 None（未测量），不是 0%（全失败）也不是 100%。"""
    summary = benchmark.aggregate_rows([])
    assert summary["total_runs"] == 0
    assert summary["ok_runs"] == 0
    overall = summary["overall"]
    assert overall["runs"] == 0
    assert overall["success_rate"] is None
    assert overall["avg_duration_ms"] is None
    assert overall["verification_pass_rate"] is None
    assert overall["artifacts_pass_rate"] is None
    assert overall["tokens_total"] == 0
    for mode in benchmark.MODES:
        assert summary["by_mode"][mode]["runs"] == 0
        assert summary["by_mode"][mode]["success_rate"] is None


def test_aggregate_success_rate_and_averages():
    rows = [
        _row(mode="single", success=True, duration_ms=1000, tokens=100,
             llm_calls=2, steps=4),
        _row(mode="single", success=True, duration_ms=3000, tokens=300,
             llm_calls=4, steps=6),
        _row(mode="single", success=False, duration_ms=2000, tokens=200,
             llm_calls=3, steps=5),
    ]
    summary = benchmark.aggregate_rows(rows)
    assert summary["total_runs"] == 3
    assert summary["ok_runs"] == 3
    single = summary["by_mode"]["single"]
    assert single["runs"] == 3
    assert single["successes"] == 2
    assert single["success_rate"] == round(2 / 3, 4)
    assert single["avg_duration_ms"] == 2000.0
    assert single["tokens_total"] == 600
    assert single["llm_calls_total"] == 9
    assert single["steps_total"] == 15
    # 其它模式没有数据：runs=0、比率为 None
    assert summary["by_mode"]["multi"]["runs"] == 0
    assert summary["by_mode"]["multi"]["success_rate"] is None


def test_aggregate_excludes_error_and_unavailable_from_success_rate():
    """unavailable / error 不是模型能力，不能拉低成功率。"""
    rows = [
        _row(mode="single", success=True),
        _row(mode="multi", status="unavailable", success=False),
        _row(mode="multi", status="error", success=False),
        _row(mode="subagent", success=False),
    ]
    summary = benchmark.aggregate_rows(rows)
    assert summary["total_runs"] == 4
    assert summary["ok_runs"] == 2
    assert summary["unavailable_runs"] == 1
    assert summary["error_runs"] == 1
    assert summary["overall"]["success_rate"] == 0.5
    assert summary["by_mode"]["multi"]["runs"] == 0
    assert summary["by_mode"]["multi"]["success_rate"] is None
    assert summary["by_mode"]["subagent"]["success_rate"] == 0.0


def test_aggregate_verification_and_artifacts_only_measured_rows():
    """None 表示"该模式没这个环节/没测到"，不参与分母。"""
    rows = [
        _row(verification_passed=True, artifacts_ok=True),
        _row(verification_passed=False, artifacts_ok=False),
        _row(verification_passed=None, artifacts_ok=None),   # subagent 常态
    ]
    block = benchmark.aggregate_rows(rows)["overall"]
    assert block["verification_measured"] == 2
    assert block["verification_pass_rate"] == 0.5
    assert block["artifacts_measured"] == 2
    assert block["artifacts_pass_rate"] == 0.5


def test_aggregate_rework_and_interventions_are_summed():
    rows = [
        _row(rework_count=1, human_interventions=2),
        _row(rework_count=0, human_interventions=0),
        _row(rework_count=2, human_interventions=1),
    ]
    block = benchmark.aggregate_rows(rows)["overall"]
    assert block["rework_total"] == 3
    assert block["human_interventions_total"] == 3


# ═══════════════════════════════════════════════════════════════
# 指标提取（fake 返回值，不依赖真实 Agent）
# ═══════════════════════════════════════════════════════════════


class _FakeUsage:
    def __init__(self, prompt=100, completion=50, calls=3):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.calls = calls

    @property
    def total(self):
        return self.prompt_tokens + self.completion_tokens


class _FakeLLM:
    def __init__(self, usage):
        self.usage = usage


class _FakeAgent:
    def __init__(self, usage=None, verify_state=None):
        self.llm = _FakeLLM(usage or _FakeUsage())
        self._verify_state = verify_state or {}

    def interjection_report(self):
        return {"accepted": 0, "applied": 0, "dropped": 0, "pending": 0}


class _FakeResult:
    """模拟 AgentResult 的最小字段面。"""

    def __init__(self, success=True, tokens=150, llm_calls=3, steps=7,
                 stop_reason="completed", error=""):
        self.success = success
        self.token_usage = _FakeUsage(prompt=tokens - 50, completion=50,
                                      calls=llm_calls)
        self.llm_calls = llm_calls
        self.steps_executed = steps
        self.stop_reason = stop_reason
        self.error = error


def test_extract_single_metrics_reads_agent_result():
    agent = _FakeAgent(verify_state={"verify": {"done": True, "available": True}})
    metrics = benchmark.extract_single_metrics(agent, _FakeResult())
    assert metrics["success"] is True
    assert metrics["tokens"] == 150
    assert metrics["llm_calls"] == 3
    assert metrics["steps"] == 7
    assert metrics["verification_passed"] is True
    assert metrics["rework_count"] == 0
    assert metrics["stop_reason"] == "completed"


def test_extract_single_metrics_verification_unavailable_is_none():
    """验收设施不可用：既不记通过也不记失败（None = 未测量）。"""
    agent = _FakeAgent(verify_state={"verify": {"done": False,
                                                "available": False}})
    metrics = benchmark.extract_single_metrics(agent, _FakeResult())
    assert metrics["verification_passed"] is None


def test_extract_multi_metrics_reads_orchestrator_payload():
    """v1.11.22 编排器的聚合 token_usage / metrics / verification。"""
    result = {
        "success": False,
        "stop_reason": "verification_failed",
        "error": "测试未通过",
        "token_usage": _FakeUsage(prompt=900, completion=100, calls=12),
        "steps": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
        "metrics": {"rework_count": 1, "llm_calls": 12, "tokens": 1000},
        "verification": {"tests": {"passed": False}, "done": False},
    }
    metrics = benchmark.extract_multi_metrics(result)
    assert metrics["success"] is False
    assert metrics["tokens"] == 1000
    assert metrics["llm_calls"] == 12
    assert metrics["steps"] == 3
    assert metrics["verification_passed"] is False
    assert metrics["rework_count"] == 1
    assert metrics["stop_reason"] == "verification_failed"


def test_extract_multi_metrics_tolerates_legacy_shape():
    """旧文本流水线不返回 metrics/verification 时：字段缺失 → None/0，不伪造。"""
    metrics = benchmark.extract_multi_metrics({"success": True, "steps": []})
    assert metrics["success"] is True
    assert metrics["verification_passed"] is None
    assert metrics["rework_count"] == 0


def test_extract_subagent_metrics_reads_run_subtask_dict():
    agent = _FakeAgent()
    result = {
        "success": True,
        "tokens": 432,
        "llm_calls": 6,
        "steps": 4,
        "stop_reason": "no_more_tools",
        "error": "",
    }
    metrics = benchmark.extract_subagent_metrics(agent, result)
    assert metrics["success"] is True
    assert metrics["tokens"] == 432
    assert metrics["llm_calls"] == 6
    assert metrics["steps"] == 4
    # 子任务没有验证闭环 —— 必须是 None 而不是 False（否则会被误读成验收失败）
    assert metrics["verification_passed"] is None


def test_human_interventions_counts_applied_interjections_and_approvals():
    agent = _FakeAgent()
    agent._bench_approval_calls = 2

    def _report():
        return {"accepted": 3, "applied": 1, "dropped": 0, "pending": 0}

    agent.interjection_report = _report
    assert benchmark._human_interventions(agent) == 3


# ═══════════════════════════════════════════════════════════════
# 产物检查 / JSON / CSV
# ═══════════════════════════════════════════════════════════════


def test_check_artifacts_missing_file_is_false(tmp_path: Path):
    task = benchmark.TASKS[1]                       # codegen_fizzbuzz
    workspace = benchmark.prepare_workspace(tmp_path / "ws", task)
    assert benchmark.check_artifacts(workspace, task) is False
    for rel in task.expect_files:
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text("x", encoding="utf-8")
    assert benchmark.check_artifacts(workspace, task) is True


def test_check_artifacts_pure_text_task_is_none(tmp_path: Path):
    analysis = benchmark.TASKS[0]
    assert analysis.expect_files == ()
    assert benchmark.check_artifacts(tmp_path, analysis) is None


def test_write_csv_header_and_manual_rework_left_blank(tmp_path: Path):
    rows = [
        _row(mode="single", success=True, verification_passed=True),
        _row(mode="multi", success=False, verification_passed=None,
             stop_reason="verification_failed"),
    ]
    path = benchmark.write_csv(tmp_path / "out.csv", rows)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == benchmark.CSV_COLUMNS
        data = list(reader)
    assert len(data) == 2
    # 人工回填列恒为空：脚本不替人做判断
    assert data[0]["manual_rework"] == ""
    assert data[1]["manual_rework"] == ""
    # 布尔序列化成小写 true/false；None 留空
    assert data[0]["success"] == "true"
    assert data[0]["verification_passed"] == "true"
    assert data[1]["verification_passed"] == ""
    assert data[1]["stop_reason"] == "verification_failed"


def test_write_artifacts_dry_run_has_matrix_and_empty_results(tmp_path: Path):
    payload = benchmark.build_payload([], dry_run=True)
    json_path, csv_path = benchmark.write_artifacts(tmp_path / "out", payload, [])
    assert json_path.is_file() and csv_path.is_file()
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["schema"] == benchmark.SCHEMA
    assert loaded["dry_run"] is True
    assert loaded["results"] == []
    assert len(loaded["matrix"]) == len(benchmark.TASKS) * len(benchmark.MODES)
    assert loaded["summary"]["total_runs"] == 0
    # CSV 只有表头：dry-run 不产生任何伪造数据行
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        assert len(list(csv.reader(fh))) == 1


# ═══════════════════════════════════════════════════════════════
# dry-run CLI（绝不触发真实模型）
# ═══════════════════════════════════════════════════════════════


def test_cli_dry_run_writes_plan_without_calling_models(tmp_path, capsys):
    """默认就是 dry-run：只写矩阵与空结果，不需要 API Key。"""
    out = tmp_path / "bench"
    assert benchmark.main(["--dry-run", "--out", str(out)]) == 0

    captured = capsys.readouterr().out
    assert "dry-run" in captured
    assert "共" in captured and "个组合" in captured
    # 矩阵覆盖所有任务 × 模式
    assert str(len(benchmark.TASKS) * len(benchmark.MODES)) in captured

    loaded = json.loads((out / "collab_benchmark.json").read_text(encoding="utf-8"))
    assert loaded["dry_run"] is True
    assert len(loaded["matrix"]) == len(benchmark.TASKS) * len(benchmark.MODES)
    assert loaded["results"] == []
    # 没有任何真实指标被写出来
    assert loaded["summary"]["overall"]["success_rate"] is None


def test_cli_task_and_mode_filters(tmp_path):
    out = tmp_path / "bench"
    assert benchmark.main([
        "--dry-run", "--out", str(out),
        "--task", "bugfix_calc", "--mode", "single,multi",
    ]) == 0
    loaded = json.loads((out / "collab_benchmark.json").read_text(encoding="utf-8"))
    assert len(loaded["matrix"]) == 2
    assert {item["task_id"] for item in loaded["matrix"]} == {"bugfix_calc"}
    assert {item["mode"] for item in loaded["matrix"]} == {"single", "multi"}


def test_cli_rejects_unknown_task_or_mode(tmp_path, capsys):
    assert benchmark.main([
        "--dry-run", "--out", str(tmp_path / "x"), "--task", "nope",
    ]) == benchmark.EXIT_CONFIG
    assert "不存在" in capsys.readouterr().err
    assert benchmark.main([
        "--dry-run", "--out", str(tmp_path / "x"), "--mode", "nope",
    ]) == benchmark.EXIT_CONFIG


def test_cli_rejects_live_plus_dry_run(tmp_path, capsys):
    assert benchmark.main([
        "--live", "--dry-run", "--out", str(tmp_path / "x"),
    ]) == benchmark.EXIT_CONFIG
    assert "互斥" in capsys.readouterr().err
