"""版本隔离测评编排 + 报告聚合的离线回归（v1.11.24）。

## 边界（绝不真跑模型）

* ``scripts/edition_benchmark.py``：只测 **dry-run 计划/矩阵/依赖预检/隔离环境
  构造/临时许可证/扩展包装载**；``--live`` 的预检失败路径（缺 Key、缺参数）也
  只验证"明确失败且不启动服务"，不启动任何 server、不联网。
* ``scripts/benchmark_report.py``：用**合成原始报告**验证完整性校验（缺失 cell
  即失败）与聚合口径（探针不计分、未测量记 null、脱敏），并断言三份产出
  （JSON/CSV/Markdown）的结构。

真实测评一次会消耗真实 Token 且耗时几十分钟，绝不能进 CI —— 与
``tests/planning/test_collab_benchmark_metrics.py`` 同一原则。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    """把 ``scripts/<name>.py`` 当模块加载（scripts/ 不是 Python 包）。"""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"audit_{name}_under_test", path)
    assert spec and spec.loader, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclass 需要模块在 sys.modules
    spec.loader.exec_module(module)
    return module


benchmark = _load_script("edition_benchmark")
report = _load_script("benchmark_report")


# ═══════════════════════════════════════════════════════════════
# 合成原始报告
# ═══════════════════════════════════════════════════════════════


def _eval_report(*, statuses: dict[str, str] | None = None,
                 probe_status: str | None = "passed",
                 probe_expect_fail: bool = True,
                 omit: tuple[str, ...] = (),
                 extra_cases: list[dict] | None = None,
                 with_metrics: bool = True,
                 model: str = "deepseek-flash",
                 provider: str = "deepseek") -> dict:
    """构造一份 ``/api/eval/report`` 的 report 载荷（6 计分 + 探针）。"""
    statuses = statuses or {task: "passed" for task in report.SCORING_TASKS}
    cases: list[dict] = []
    for task in report.SCORING_TASKS:
        if task in omit:
            continue
        status = statuses.get(task, "failed")
        case = {
            "id": task, "status": status, "passed": status == "passed",
            "mode": "coding", "expect_fail": False,
            "output_preview": "", "error": "", "assertions": [],
        }
        if with_metrics:
            case.update({"seconds": 1.5, "total_tokens": 1000,
                         "estimated_cost_usd": 0.01})
        cases.append(case)
    if probe_status is not None:
        cases.append({"id": report.PROBE_TASK, "status": probe_status,
                      "passed": probe_status == "passed", "mode": "coding",
                      "expect_fail": probe_expect_fail, "assertions": []})
    cases.extend(extra_cases or [])
    return {"suite": "comprehensive", "model": model, "provider": provider,
            "total": len(cases), "cases": cases}


def _collab_report(edition: str, *, statuses: dict[tuple[str, str], str] | None = None,
                   omit: tuple[tuple[str, str], ...] = (),
                   duplicate: tuple[str, str] | None = None,
                   error_text: str = "") -> dict:
    """构造一份 ``collab_benchmark.json``（版本允许的模式 × 4 任务）。"""
    statuses = statuses or {}
    rows: list[dict] = []
    for task in report.COLLAB_TASKS:
        for mode in report.COLLAB_MODES[edition]:
            if (task, mode) in omit:
                continue
            status = statuses.get((task, mode), "ok")
            rows.append({
                "task_id": task, "category": "mixed", "mode": mode,
                "status": status, "success": status == "ok", "artifacts_ok": True,
                "duration_ms": 1200, "tokens": 500, "llm_calls": 3, "steps": 5,
                "verification_passed": status == "ok", "rework_count": 0,
                "human_interventions": 0, "stop_reason": "completed",
                "error": error_text if status != "ok" else "", "notes": "",
            })
    if duplicate is not None:
        rows.append(dict(next(row for row in rows
                              if (row["task_id"], row["mode"]) == duplicate)))
    return {"schema": "automind.collab_benchmark/1", "dry_run": False,
            "tasks": [{"id": task} for task in report.COLLAB_TASKS],
            "results": rows}


def _write_run(run_dir: Path, *, eval_overrides: dict[tuple[str, int], dict] | None = None,
               collab_overrides: dict[str, dict] | None = None,
               skip: tuple[tuple[str, int], ...] = (),
               skip_collab: tuple[str, ...] = (),
               manifest: dict | None = None) -> Path:
    """写一套完整的"三个版本 × 3 重复 + 协同"原始报告。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    payload_manifest = manifest if manifest is not None else {
        "schema": "automind.edition_benchmark/1", "dry_run": False,
        "provider": "deepseek", "model": "deepseek-flash",
        "api_key_source": "env:DEEPSEEK_API_KEY",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(payload_manifest, ensure_ascii=False), encoding="utf-8")
    raw = run_dir / "raw"
    for edition in report.EDITIONS:
        edition_dir = raw / edition
        edition_dir.mkdir(parents=True, exist_ok=True)
        for repeat in range(1, report.EVAL_REPEATS + 1):
            if (edition, repeat) in skip:
                continue
            payload = (eval_overrides or {}).get((edition, repeat)) \
                or _eval_report()
            (edition_dir / f"eval_rep{repeat}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if edition in skip_collab:
            continue
        collab = (collab_overrides or {}).get(edition) \
            or _collab_report(edition)
        (edition_dir / "collab_benchmark.json").write_text(
            json.dumps(collab, ensure_ascii=False), encoding="utf-8")
    return run_dir


# ═══════════════════════════════════════════════════════════════
# 1. dry-run：计划 / 矩阵 / 不联网 / 不启动服务 / 不泄露 Key
# ═══════════════════════════════════════════════════════════════


class TestDryRun:
    def test_default_cli_is_dry_run_and_writes_plan(self, tmp_path, monkeypatch,
                                                    capsys):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        out = tmp_path / "bench"
        code = benchmark.main(["--out", str(out)])
        assert code == benchmark.EXIT_OK, "默认必须是 dry-run 且不需要 Key"
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        assert plan["dry_run"] is True
        assert plan["schema"] == benchmark.SCHEMA
        assert plan["provider"] == "deepseek"
        assert plan["model"] == "deepseek-flash"
        assert not (out / "run").exists(), "dry-run 不能创建隔离运行目录"
        assert not (out / "raw").exists(), "dry-run 不能产生任何结果"
        captured = capsys.readouterr().out
        assert "dry-run" in captured and "--live" in captured

    def test_plan_matrix_has_three_editions_and_expected_cells(self, tmp_path,
                                                               monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        out = tmp_path / "bench"
        assert benchmark.main(["--out", str(out)]) == benchmark.EXIT_OK
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        editions = {entry["name"]: entry for entry in plan["editions"]}
        assert set(editions) == {"community", "pro", "enterprise"}
        assert [editions[name]["port"] for name in
                ("community", "pro", "enterprise")] == [18765, 18766, 18767]
        # community 强制社区版、不装扩展、只允许 single
        community = editions["community"]
        assert community["force_community"] is True
        assert community["license_tier"] is None
        assert community["collab_modes"] == ["single"]
        assert "none" in community["extensions"]
        # pro/enterprise 三个模式 + 临时许可证
        for name, tier in (("pro", "PRO"), ("enterprise", "ENT")):
            entry = editions[name]
            assert entry["force_community"] is False
            assert entry["license_tier"] == tier
            assert entry["collab_modes"] == ["single", "multi", "subagent"]
            assert "临时" in entry["license"]
            # 隔离路径只给占位符，不暴露绝对路径
            assert all(str(value).startswith("<RUN>")
                       for value in entry["isolation"].values())
        # 评测矩阵 6×3×3=54、协作矩阵 community 4 + pro 12 + ent 12=28
        assert plan["matrix"]["eval"]["cells"] == 54
        assert len(plan["matrix"]["eval"]["expected"]) == 54
        assert plan["matrix"]["collab"]["cells"] == 28
        assert len(plan["matrix"]["collab"]["expected"]) == 28
        assert len(set(tuple(sorted(cell.items()))
                       for cell in plan["matrix"]["eval"]["expected"])) == 54
        # exec 口径：探针不计分，按 expect_fail 过滤
        assert plan["matrix"]["eval"]["scoring_tasks"] == list(report.SCORING_TASKS)
        assert "expect_fail" in plan["matrix"]["eval"]["probe_rule"]

    def test_dry_run_never_starts_processes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        def _boom(*_args, **_kwargs):           # pragma: no cover - 触发即失败
            raise AssertionError("dry-run 不应该启动任何子进程")

        monkeypatch.setattr(benchmark.subprocess, "Popen", _boom)
        monkeypatch.setattr(benchmark.subprocess, "run", _boom)
        assert benchmark.main(["--out", str(tmp_path / "bench")]) == benchmark.EXIT_OK

    def test_plan_does_not_leak_api_key(self, tmp_path, monkeypatch, capsys):
        secret = "sk-offline-test-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
        out = tmp_path / "bench"
        assert benchmark.main(["--out", str(out)]) == benchmark.EXIT_OK
        plan_text = (out / "plan.json").read_text(encoding="utf-8")
        captured = capsys.readouterr()
        assert secret not in plan_text and secret not in captured.out
        plan = json.loads(plan_text)
        assert plan["api_key_source"] == "env:DEEPSEEK_API_KEY"
        assert plan["dependencies"]["ready_for_live"] is True

    def test_live_and_dry_run_are_mutually_exclusive(self, tmp_path, capsys):
        code = benchmark.main(["--live", "--dry-run", "--out", str(tmp_path / "x")])
        assert code == benchmark.EXIT_CONFIG
        assert "互斥" in capsys.readouterr().err

    def test_plan_surfaces_missing_dependencies_but_still_exits_zero(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setattr(benchmark, "_check_eval_suite", lambda *_a, **_k: {
            "name": "comprehensive", "path": "x", "exists": False, "cases": [],
            "scoring_tasks": [], "probe_cases": [],
            "error": "综合套件不存在（测试注入）"})
        monkeypatch.setattr(benchmark, "resolve_pro_wheel", lambda *_a, **_k: {
            "path": "", "version": "", "core_version": "", "version_match": False,
            "error": "找不到 staging Pro wheel（测试注入）"})
        monkeypatch.setattr(benchmark, "_port_conflict",
                            lambda port: f"127.0.0.1:{port} 已占用（测试注入）")
        out = tmp_path / "bench"
        assert benchmark.main(["--out", str(out)]) == benchmark.EXIT_OK
        plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
        deps = plan["dependencies"]
        assert deps["ready_for_live"] is False
        messages = " ".join(deps["errors"])
        assert "综合套件" in messages and "staging Pro wheel" in messages \
            and "端口" in messages


# ═══════════════════════════════════════════════════════════════
# 2. 矩阵 / 依赖预检（纯函数）
# ═══════════════════════════════════════════════════════════════


class TestMatrixAndDependencies:
    def test_editions_are_isolated_and_on_fixed_ports(self):
        specs = benchmark.build_editions()
        assert [spec.port for spec in specs] == [18765, 18766, 18767]
        assert [spec.expected_edition for spec in specs] == list(report.EDITIONS)
        assert specs[0].force_community is True
        assert specs[0].license_tier == ""
        assert [spec.license_tier for spec in specs] == ["", "PRO", "ENT"]
        assert specs[0].collab_modes == ("single",)
        assert specs[1].collab_modes == ("single", "multi", "subagent")

    def test_expected_cells_counts_and_uniqueness(self):
        eval_cells = benchmark.expected_eval_cells(3)
        assert len(eval_cells) == 54 == 3 * 3 * 6
        assert len({(cell["edition"], cell["repeat"], cell["task_id"])
                    for cell in eval_cells}) == 54
        collab_cells = benchmark.expected_collab_cells()
        assert len(collab_cells) == 4 + 12 + 12 == 28
        assert {(cell["edition"], cell["task_id"], cell["mode"])
                for cell in collab_cells} == set(
                    (cell["edition"], cell["task_id"], cell["mode"])
                    for cell in collab_cells)

    def test_scoring_tasks_are_six_distinct_from_probe(self):
        assert len(report.SCORING_TASKS) == 6
        assert len(set(report.SCORING_TASKS)) == 6
        assert report.PROBE_TASK not in report.SCORING_TASKS
        assert set(report.SCORING_TASKS) == {
            "analysis_summary", "bugfix_calc", "multi_file_pkg",
            "data_csv", "doc_report", "honesty_safety"}

    def test_real_suite_matches_the_contract(self):
        """真实 comprehensive.yml 必须恰好 6 个计分 + 1 个 expect_fail 探针。"""
        suite_path = ROOT / "automind" / "eval" / "suites" / "comprehensive.yml"
        if not suite_path.is_file():
            pytest.skip("综合套件尚未就绪（依赖 v1.11.24 套件任务）")
        info = benchmark._check_eval_suite()
        assert info["error"] == "", info["error"]
        assert len(info["scoring_tasks"]) == 6
        assert set(info["scoring_tasks"]) == set(report.SCORING_TASKS)
        assert len(info["probe_cases"]) >= 1
        assert report.PROBE_TASK in info["probe_cases"]

    def test_port_conflict_probe(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            assert benchmark._port_conflict(port), "占用端口应该被识别"
        assert benchmark._port_conflict(port) == ""

    def test_credentials_never_return_key_value(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        missing = benchmark.resolve_credentials("deepseek")
        assert missing["present"] is False
        assert "DEEPSEEK_API_KEY" in missing["error"]
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-value")
        present = benchmark.resolve_credentials("deepseek")
        assert present["present"] is True
        assert present["source"] == "env:DEEPSEEK_API_KEY"
        assert "sk-secret-value" not in json.dumps(present, ensure_ascii=False)
        explicit = benchmark.resolve_credentials("deepseek", api_key="k-123")
        assert explicit["source"] == "explicit"
        assert "k-123" not in json.dumps(explicit, ensure_ascii=False)
        assert benchmark.resolve_credentials("ollama")["present"] is True

    def test_pro_wheel_resolution(self, tmp_path):
        core = benchmark._read_core_version()
        good = tmp_path / f"automind_pro-{core}-py3-none-any.whl"
        good.write_bytes(b"fake")
        info = benchmark.resolve_pro_wheel(str(good))
        assert info["path"] == str(good)
        assert info["version_match"] is True and not info["error"]
        missing = benchmark.resolve_pro_wheel(str(tmp_path / "nope.whl"))
        assert missing["error"]
        # 仓库里 Lead 构建的 staging wheel 应能被自动发现
        existing = benchmark.resolve_pro_wheel("")
        if (ROOT / "pro" / "dist").is_dir() and any(
                (ROOT / "pro" / "dist").glob("automind_pro-*.whl")):
            assert existing["path"] and not existing["error"]
            assert existing["core_version"] == core

    def test_collect_dependencies_reports_every_missing_item(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(benchmark, "_check_eval_suite", lambda *_a, **_k: {
            "error": "套件缺失（测试注入）", "cases": [], "scoring_tasks": [],
            "probe_cases": []})
        monkeypatch.setattr(benchmark, "resolve_pro_wheel", lambda *_a, **_k: {
            "error": "wheel 缺失（测试注入）"})
        monkeypatch.setattr(benchmark, "_port_conflict",
                            lambda port: f"port {port} busy")
        deps = benchmark.collect_dependencies(editions=benchmark.build_editions())
        joined = " ".join(deps.errors)
        assert "套件缺失" in joined and "wheel 缺失" in joined and "busy" in joined
        assert len(deps.port_conflicts) == 3
        assert deps.as_dict()["ready_for_live"] is False


# ═══════════════════════════════════════════════════════════════
# 3. 隔离环境 / 扩展包装载 / 临时许可证
# ═══════════════════════════════════════════════════════════════


class TestIsolationHelpers:
    def test_prepare_runtime_isolates_home_data_project(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-parent-should-not-leak")
        monkeypatch.setenv("AUTOMIND_LICENSE", "AMP2-parent-license")
        monkeypatch.setenv("AUTOMIND_FORCE_COMMUNITY", "1")
        community = benchmark.build_editions()[0]
        runtime = benchmark.prepare_runtime(community, tmp_path / "run")
        assert runtime.home.is_dir() and runtime.data_dir.is_dir()
        assert runtime.project.is_dir()
        env = runtime.env
        assert env["HOME"] == str(runtime.home)
        assert env["USERPROFILE"] == str(runtime.home)
        assert env["AUTOMIND_DATA_DIR"] == str(runtime.data_dir)
        assert env["AUTOMIND_FORCE_COMMUNITY"] == "1"
        assert "AUTOMIND_LICENSE" not in env, "父进程许可证不能串进隔离版本"
        assert "DEEPSEEK_API_KEY" not in env, "Key 不能进 server 环境（只走 /api/config）"
        assert str(ROOT) in env["PYTHONPATH"].split(benchmark.os.pathsep)

    def test_pro_runtime_has_no_force_community_and_license_env_applied(
            self, tmp_path):
        spec = benchmark.build_editions()[1]
        runtime = benchmark.prepare_runtime(spec, tmp_path / "run")
        assert "AUTOMIND_FORCE_COMMUNITY" not in runtime.env
        benchmark._apply_license_env(runtime, {
            "license": "AMP2-fake-license", "public_key": "ab" * 32,
            "tier": "PRO", "expiry": "20991231", "source": "test"})
        assert runtime.env["AUTOMIND_LICENSE"] == "AMP2-fake-license"
        assert runtime.env["AUTOMIND_LICENSE_PUBLIC_KEY"] == "ab" * 32

    def test_install_pro_wheel_extracts_package(self, tmp_path):
        wheel = tmp_path / "automind_pro-1.11.24-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("automind_pro/__init__.py", "__version__='x'")
            archive.writestr("automind_pro/licensing/__init__.py", "")
            archive.writestr("automind_pro-1.11.24.dist-info/METADATA", "Name: x")
            archive.writestr("unrelated.txt", "ignore")
        target = tmp_path / "data" / "extensions"
        result = benchmark.install_pro_wheel(wheel, target)
        assert result["files"] == 3
        assert (target / "automind_pro" / "__init__.py").is_file()
        assert not (target / "unrelated.txt").exists()

    def test_install_pro_wheel_rejects_traversal_and_missing_package(self, tmp_path):
        bad = tmp_path / "automind_pro-bad.whl"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("automind_pro/__init__.py", "")
            archive.writestr("../evil.txt", "x")
        with pytest.raises(benchmark.EditionBenchmarkError, match="不安全路径"):
            benchmark.install_pro_wheel(bad, tmp_path / "ext")
        empty = tmp_path / "automind_pro-empty.whl"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("other/__init__.py", "")
        with pytest.raises(benchmark.EditionBenchmarkError, match="缺少 automind_pro"):
            benchmark.install_pro_wheel(empty, tmp_path / "ext2")

    def test_license_files_require_public_key(self, tmp_path):
        path = tmp_path / "license.txt"
        path.write_text("AMP2-demo", encoding="utf-8")
        with pytest.raises(benchmark.EditionBenchmarkError, match="public-key"):
            benchmark.load_license_files(str(path), "")
        info = benchmark.load_license_files(str(path), "ab" * 32)
        assert info["license"] == "AMP2-demo"
        assert info["public_key"] == "ab" * 32

    def test_generate_temp_license_is_ephemeral_and_verifiable(self, monkeypatch):
        pro_src = ROOT / "pro" / "automind_pro" / "licensing"
        if not pro_src.is_dir():
            pytest.skip("缺少 Pro 扩展包源码（社区版源码包）")
        info = benchmark.generate_temp_license("PRO", days=7)
        assert info["license"].startswith("AMP2-")
        assert len(info["public_key"]) == 64
        assert "ephemeral" in info["source"]
        # 只信任这把一次性公钥时许可证应验签通过；且不产生任何私钥/许可证文件
        monkeypatch.setenv("AUTOMIND_LICENSE_PUBLIC_KEY", info["public_key"])
        pro_root = str(ROOT / "pro")
        if pro_root not in sys.path:
            sys.path.insert(0, pro_root)
        try:
            from automind_pro.licensing import parse_license, parse_license_detail

            valid, code, detail = parse_license_detail(
                info["license"], check_machine=False, check_clock=False)
            assert valid is not None and code in ("ok", "not_yet_active"), detail
            assert parse_license(info["license"]) is not None
        finally:
            if pro_root in sys.path:
                sys.path.remove(pro_root)
        leaked = list(Path(tempfile.gettempdir()).glob("private_key.hex"))
        assert not leaked, f"临时许可证不得把私钥写到临时目录：{leaked}"


# ═══════════════════════════════════════════════════════════════
# 4. 报告：完整性校验（缺失即失败）
# ═══════════════════════════════════════════════════════════════


class TestValidation:
    def test_complete_fixture_passes_and_aggregates(self, tmp_path):
        run_dir = _write_run(tmp_path / "run")
        code, result = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_OK, result
        summary = json.loads(
            (tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
        assert summary["validation"]["ok"] is True
        assert summary["scope"]["eval"]["repeats"] == 3
        assert summary["scope"]["eval"]["scoring_tasks"] == list(report.SCORING_TASKS)
        assert len(summary["eval"]["matrix"]) == 54
        assert len(summary["collab"]["matrix"]) == 28
        assert summary["eval"]["probe"]["task_id"] == report.PROBE_TASK
        assert "expect_fail" in summary["eval"]["probe"]["selection"]
        # 三份产出 + Markdown
        for name in ("summary.json", "eval_matrix.csv",
                     "collab_matrix.csv", "report.md"):
            assert (tmp_path / "out" / name).is_file(), name
        markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
        assert "不计分" in markdown
        assert "估算" in markdown
        assert "expect_fail" in markdown

    def test_pass_rate_excludes_expect_fail_probe(self, tmp_path):
        """6 个计分任务里 5 pass + 探针 pass：分母是 18 个计分 cell（不是 21）。"""
        statuses = {task: "passed" for task in report.SCORING_TASKS}
        statuses["data_csv"] = "failed"
        payload = _eval_report(statuses=statuses, probe_status="passed")
        run_dir = _write_run(tmp_path / "run", eval_overrides={
            (edition, repeat): payload
            for edition in report.EDITIONS
            for repeat in range(1, 4)})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_OK
        summary = json.loads(
            (tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
        assert summary["eval"]["editions"]["community"]["pass_rate"] == round(15 / 18, 4)
        by_task = summary["eval"]["editions"]["community"]["by_task"]
        assert report.PROBE_TASK not in by_task
        assert len(by_task) == 6
        assert summary["eval"]["editions"]["community"]["cases_measured"] == 18

    def test_missing_eval_report_fails(self, tmp_path):
        run_dir = _write_run(tmp_path / "run", skip=(("pro", 2),))
        code, result = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_INVALID
        validation = json.loads(
            (tmp_path / "out" / "validation.json").read_text(encoding="utf-8"))
        assert any(item["code"] == "missing_eval_report"
                   for item in validation["errors"])
        assert not (tmp_path / "out" / "summary.json").exists(), \
            "缺失 cell 时不得产出汇总（不能拿 0 补齐）"

    def test_missing_scoring_task_cell_fails_with_cell_location(self, tmp_path):
        incomplete = _eval_report(omit=("doc_report",))
        run_dir = _write_run(tmp_path / "run", eval_overrides={
            ("enterprise", 3): incomplete})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_INVALID
        validation = json.loads(
            (tmp_path / "out" / "validation.json").read_text(encoding="utf-8"))
        cells = [item.get("cell", "") for item in validation["errors"]
                 if item["code"] == "missing_task"]
        assert "enterprise/rep3/doc_report" in cells

    def test_missing_collab_cell_fails(self, tmp_path):
        broken = _collab_report("pro", omit=(("bugfix_calc", "multi"),))
        run_dir = _write_run(tmp_path / "run",
                             collab_overrides={"pro": broken})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_INVALID
        validation = json.loads(
            (tmp_path / "out" / "validation.json").read_text(encoding="utf-8"))
        assert any(item["code"] == "missing_collab_cell"
                   for item in validation["errors"])

    def test_duplicate_entries_fail(self, tmp_path):
        dup_payload = _eval_report(extra_cases=[
            {"id": "analysis_summary", "status": "passed", "expect_fail": False}])
        dup_collab = _collab_report("community",
                                    duplicate=("analysis_summary", "single"))
        run_dir = _write_run(
            tmp_path / "run",
            eval_overrides={("community", 1): dup_payload},
            collab_overrides={"community": dup_collab})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_INVALID
        validation = json.loads(
            (tmp_path / "out" / "validation.json").read_text(encoding="utf-8"))
        codes = {item["code"] for item in validation["errors"]}
        assert "duplicate_task" in codes
        assert "duplicate_collab_cell" in codes

    def test_probe_must_pass_and_keep_expect_fail_flag(self, tmp_path):
        failed_probe = _eval_report(probe_status="failed")
        run_dir = _write_run(tmp_path / "run",
                             eval_overrides={("pro", 1): failed_probe})
        code, _ = report.run_report(run_dir, tmp_path / "pro-out")
        assert code == report.EXIT_INVALID
        validation = json.loads(
            (tmp_path / "pro-out" / "validation.json").read_text(encoding="utf-8"))
        assert any(item["code"] == "probe_not_passed"
                   for item in validation["errors"])

        unflagged = _eval_report(probe_status="passed",
                                 probe_expect_fail=False)
        run_dir2 = _write_run(tmp_path / "run2",
                              eval_overrides={("pro", 1): unflagged})
        code2, _ = report.run_report(run_dir2, tmp_path / "pro-out2")
        assert code2 == report.EXIT_INVALID
        validation2 = json.loads(
            (tmp_path / "pro-out2" / "validation.json").read_text(encoding="utf-8"))
        codes = {item["code"] for item in validation2["errors"]}
        assert "probe_flag_missing" in codes

    def test_scoring_task_marked_expect_fail_is_rejected(self, tmp_path):
        bad = _eval_report()
        for case in bad["cases"]:
            if case["id"] == "data_csv":
                case["expect_fail"] = True
        run_dir = _write_run(tmp_path / "run",
                             eval_overrides={("community", 2): bad})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_INVALID
        validation = json.loads(
            (tmp_path / "out" / "validation.json").read_text(encoding="utf-8"))
        assert any(item["code"] == "scoring_task_expect_fail"
                   for item in validation["errors"])


# ═══════════════════════════════════════════════════════════════
# 5. 报告：聚合口径（不编造数字）与脱敏
# ═══════════════════════════════════════════════════════════════


class TestAggregationAndRedaction:
    def test_missing_metrics_stay_null_not_zero(self, tmp_path):
        payload = _eval_report(with_metrics=False)     # 没有 seconds/tokens/cost
        run_dir = _write_run(tmp_path / "run", eval_overrides={
            (edition, repeat): payload
            for edition in report.EDITIONS
            for repeat in range(1, 4)})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_OK
        summary = json.loads(
            (tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
        community = summary["eval"]["editions"]["community"]
        assert community["total_tokens"] is None, "未测量必须是 null 而不是 0"
        assert community["estimated_cost_usd"] is None
        assert community["seconds"] is None

    def test_collab_unavailable_not_counted_in_success_rate(self, tmp_path):
        rows = {("analysis_summary", "multi"): "unavailable",
                ("bugfix_calc", "multi"): "error"}
        broken = _collab_report("pro", statuses=rows)
        run_dir = _write_run(tmp_path / "run", collab_overrides={"pro": broken})
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_OK
        summary = json.loads(
            (tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
        multi = summary["collab"]["editions"]["pro"]["by_mode"]["multi"]
        assert multi["runs"] == 4
        assert multi["unavailable_runs"] == 1 and multi["error_runs"] == 1
        assert multi["ok_runs"] == 2
        assert multi["success_rate"] == 1.0, "只在 ok 行里算成功率"

    def test_csv_outputs_and_markdown_tables(self, tmp_path):
        import csv as csv_mod

        run_dir = _write_run(tmp_path / "run")
        assert report.run_report(run_dir, tmp_path / "out")[0] == report.EXIT_OK
        with (tmp_path / "out" / "eval_matrix.csv").open(
                encoding="utf-8-sig", newline="") as handle:
            rows = list(csv_mod.DictReader(handle))
        assert len(rows) == 54
        assert tuple(rows[0]) == report._EVAL_CSV_COLUMNS
        with (tmp_path / "out" / "collab_matrix.csv").open(
                encoding="utf-8-sig", newline="") as handle:
            collab_rows = list(csv_mod.DictReader(handle))
        assert len(collab_rows) == 28
        markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
        for task in report.SCORING_TASKS:
            assert task in markdown
        for edition in report.EDITIONS:
            assert edition in markdown

    def test_report_redacts_keys_licenses_and_absolute_paths(self, tmp_path):
        secret = "sk-live-SECRET-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        license_text = "AMP2-abc123def456-" + "A" * 60
        abs_path = str(tmp_path.resolve() / "raw" / "private" / "workspace")
        collab = _collab_report(
            "community",
            statuses={("analysis_summary", "single"): "error"},
            error_text=f"401 key={secret} license={license_text} path={abs_path}")
        manifest = {"schema": "automind.edition_benchmark/1", "dry_run": False,
                    "provider": "deepseek", "model": "deepseek-flash",
                    "notes": [f"debug key={secret} path={abs_path}"]}
        run_dir = _write_run(tmp_path / "run",
                             collab_overrides={"community": collab},
                             manifest=manifest)
        code, _ = report.run_report(run_dir, tmp_path / "out")
        assert code == report.EXIT_OK
        summary_text = (tmp_path / "out" / "summary.json").read_text(
            encoding="utf-8")
        markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
        csv_text = (tmp_path / "out" / "collab_matrix.csv").read_text(
            encoding="utf-8-sig")
        for blob in (summary_text, markdown, csv_text):
            assert secret not in blob
            assert license_text not in blob
            assert abs_path not in blob
        assert "[REDACTED_API_KEY]" in summary_text
        assert "[REDACTED_LICENSE]" in summary_text
        # 正常内容不能被误伤
        assert "deepseek-flash" in summary_text
        assert "analysis_summary" in summary_text

    def test_redact_text_units(self, tmp_path):
        home = str(Path.home())
        fake_key = "sk-unit-ABCDEFGHIJKLMNOPQRSTUV"
        fake_license = "AMP2-deadbeef-" + "B" * 40
        raw = (f"key={fake_key} license={fake_license} "
               f"repo={ROOT / 'automind'} home={home}/x "
               f"win=C:\\Users\\Administrator\\secret.txt")
        cleaned = report.redact_text(raw)
        assert fake_key not in cleaned and fake_license not in cleaned
        assert "[REDACTED_API_KEY]" in cleaned
        assert "[REDACTED_LICENSE]" in cleaned
        assert "<REPO>" in cleaned and "<HOME>" in cleaned
        assert "C:\\Users" not in cleaned
        nested = report.redact_obj({"a": [fake_key, {"b": fake_license}]})
        assert fake_key not in json.dumps(nested)
        assert "[REDACTED_API_KEY]" in json.dumps(nested)


# ═══════════════════════════════════════════════════════════════
# 6. report-only / live 预检失败（不启动服务）
# ═══════════════════════════════════════════════════════════════


class TestEntryPoints:
    def test_report_only_cli_from_existing_raw(self, tmp_path):
        run_dir = _write_run(tmp_path / "run")
        out = tmp_path / "out"
        code = benchmark.main(["--report-only", "--run", str(run_dir),
                               "--out", str(out)])
        assert code == benchmark.EXIT_OK
        assert (out / "report" / "summary.json").is_file()
        # 等价于直接跑报告 CLI
        assert report.main(["--run", str(run_dir),
                            "--out", str(tmp_path / "out2")]) == report.EXIT_OK

    def test_report_only_missing_raw_fails(self, tmp_path):
        out = tmp_path / "empty"
        code = benchmark.main(["--report-only", "--run", str(out),
                               "--out", str(out)])
        assert code == benchmark.EXIT_CONFIG

    def test_live_without_key_fails_before_starting_servers(self, tmp_path,
                                                            monkeypatch, capsys):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("AUTOMIND_BENCH_API_KEY", raising=False)

        def _boom(*_args, **_kwargs):           # pragma: no cover - 触发即失败
            raise AssertionError("依赖预检失败时绝不能启动服务")

        monkeypatch.setattr(benchmark.subprocess, "Popen", _boom)
        out = tmp_path / "live"
        code = benchmark.main(["--live", "--out", str(out)])
        assert code == benchmark.EXIT_CONFIG
        err = capsys.readouterr().err
        assert "API Key" in err
        assert not (out / "raw").exists()

    def test_live_license_file_requires_public_key(self, tmp_path, monkeypatch,
                                                   capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        license_path = tmp_path / "license.txt"
        license_path.write_text("AMP2-demo", encoding="utf-8")
        out = tmp_path / "live"
        code = benchmark.main([
            "--live", "--out", str(out), "--license-file", str(license_path)])
        assert code == benchmark.EXIT_CONFIG
        assert "license-public-key" in capsys.readouterr().err
