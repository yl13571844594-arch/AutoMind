"""验收门禁脚本 ``scripts/acceptance.py`` 的回归测试（v1.11.19）。

这里**不真的执行** pytest / ruff 子进程：门禁选择、报告聚合、退出码、版本检查、
残留分类器与清理安全性都用纯函数 + monkeypatch 验证。唯一会启动的子进程是
「超时行为」那条用例（一个 0.5s 的 sleep），用来证明超时会被如实判 fail。

关键断言（对应任务书的验收点）：
* profile → 门禁选择正确（static 不含 tests；fast 含治理冒烟集）；
* 报告聚合与退出码（有 fail → 1；全 pass/skip/warn → 0）；
* 残留分类器命中已知临时产物，**绝不误判源码 / tests 正常文件 / docs / dist**；
* 清理只删「白名单 + 未跟踪」，dry-run 一个文件都不删；
* 版本检查能定位到具体不一致项；JSON / Markdown 报告结构稳定。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = ROOT / "scripts" / "acceptance.py"


def _load_acceptance_module():
    """把 ``scripts/acceptance.py`` 当模块加载（scripts/ 不是 Python 包）。"""
    spec = importlib.util.spec_from_file_location("automind_acceptance_under_test",
                                                  _MODULE_PATH)
    assert spec and spec.loader, f"无法加载 {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    # 先注册再执行：dataclass 在 Python 3.12 下需要能通过 __module__ 找到模块命名空间
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acceptance = _load_acceptance_module()


# ═══════════════════════════════════════════════════════════════
# 测试夹具：构造一个"像仓库"的临时目录
# ═══════════════════════════════════════════════════════════════


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_repo(tmp_path: Path, *, version: str = "1.2.3") -> Path:
    """写出一套版本完全一致的仓库骨架（check_version 应判 pass）。"""
    _write(tmp_path / "automind" / "__init__.py",
           f'"""x"""\n\n__version__ = "{version}"\n')
    _write(tmp_path / "pyproject.toml", f"""
[project]
name = "demo"
version = "{version}"

[tool.setuptools.packages.find]
include = ["automind", "automind.*"]
""".lstrip())
    _write(tmp_path / "web" / "package.json",
           json.dumps({"name": "web", "version": version}))
    _write(tmp_path / "desktop" / "installer.iss",
           f'#define AppName "AutoMind"\n#define AppVersion "{version}"\n')
    _write(tmp_path / "CHANGELOG.md", f"# 更新日志\n\n## [{version}] - 2026-01-01\n\n- x\n")
    _write(tmp_path / "automind" / "static" / "manual.html", f"<p>v{version}</p>")
    _write(tmp_path / ".gitignore", ".automind_license\n.license-private/\nprivate_key.hex\n")
    return tmp_path


# ═══════════════════════════════════════════════════════════════
# 1) profile → 门禁选择
# ═══════════════════════════════════════════════════════════════


class TestProfileSelection:
    def test_static_has_no_tests_but_hygiene_and_web(self):
        gates = acceptance.select_gates("static")
        assert "tests" not in gates
        assert gates == ["version", "lint", "web", "hygiene"]

    def test_fast_includes_governance_smoke_tests(self):
        gates = acceptance.select_gates("fast")
        assert "tests" in gates
        assert "pro" not in gates
        # 冒烟集确实覆盖治理/可靠性相关文件
        assert "tests/core/test_v164_governance.py" in acceptance.FAST_TEST_PATHS
        assert "tests/tools/test_permissions.py" in acceptance.FAST_TEST_PATHS
        assert "tests/planning/test_tool_reliability.py" in acceptance.FAST_TEST_PATHS

    def test_full_and_release_include_pro(self):
        for profile in ("full", "release"):
            gates = acceptance.select_gates(profile)
            assert gates == ["version", "lint", "tests", "pro", "web", "hygiene"]

    def test_skip_flags_only_remove_gates(self):
        assert "web" not in acceptance.select_gates("static", skip_web=True)
        gates = acceptance.select_gates("full", skip_web=True, skip_pro=True)
        assert gates == ["version", "lint", "tests", "hygiene"]

    def test_every_gate_has_a_runner(self):
        for profile in acceptance.PROFILES:
            for gate in acceptance.select_gates(profile):
                assert gate in acceptance.GATE_RUNNERS, f"{gate} 没有注册执行器"

    def test_unknown_profile_rejected(self):
        with pytest.raises(ValueError):
            acceptance.select_gates("turbo")


# ═══════════════════════════════════════════════════════════════
# 2) 报告聚合 / 退出码 / 结构稳定性
# ═══════════════════════════════════════════════════════════════


def _result(name: str, status: str, detail: str = "") -> object:
    return acceptance.GateResult(name=name, status=status, detail=detail, duration_ms=12.0)


class TestReportAggregation:
    def test_exit_code_zero_when_no_failure(self):
        payload = acceptance.build_payload(
            "fast", "t0", "t1",
            [_result("version", "pass"), _result("lint", "skip"), _result("hygiene", "warn")])
        assert payload["ok"] is True
        assert acceptance.payload_exit_code(payload) == 0
        assert payload["summary"] == {"pass": 1, "fail": 0, "skip": 1, "warn": 1}

    def test_exit_code_one_on_any_failure(self):
        payload = acceptance.build_payload(
            "fast", "t0", "t1",
            [_result("version", "pass"), _result("tests", "fail", "boom")])
        assert payload["ok"] is False
        assert acceptance.payload_exit_code(payload) == 1
        assert payload["summary"]["fail"] == 1

    def test_json_payload_structure_is_stable(self):
        payload = acceptance.build_payload(
            "static", "t0", "t1",
            [_result("version", "pass"), _result("hygiene", "warn")])
        assert set(payload) == {"profile", "started_at", "finished_at", "gates",
                                "summary", "ok"}
        assert payload["profile"] == "static"
        for gate in payload["gates"]:
            assert set(gate) == {"name", "status", "detail", "duration_ms"}
            assert gate["status"] in acceptance.STATUSES
        assert set(payload["summary"]) == set(acceptance.STATUSES)
        assert json.loads(json.dumps(payload, ensure_ascii=False))["ok"] is True

    def test_markdown_contains_table_and_failure_details(self):
        results = [_result("version", "pass"), _result("lint", "fail", "找到了 2 个问题")]
        payload = acceptance.build_payload("static", "t0", "t1", results)
        markdown = acceptance.render_markdown(payload, results, root=ROOT)
        assert "# AutoMind 验收报告" in markdown
        assert "| 门禁 | 状态 | 耗时 | 详情 |" in markdown
        assert "`version`" in markdown and "`lint`" in markdown
        assert "## 失败详情" in markdown
        assert "找到了 2 个问题" in markdown
        assert "## 复现" in markdown
        assert "❌" in markdown

    def test_markdown_skips_failure_section_when_green(self):
        results = [_result("version", "pass")]
        payload = acceptance.build_payload("static", "t0", "t1", results)
        markdown = acceptance.render_markdown(payload, results, root=ROOT)
        assert "## 失败详情" not in markdown

    def test_run_collects_gates_and_writes_reports(self, tmp_path, monkeypatch, capsys):
        """run() 端到端（门禁执行器被替换，不启动真实 pytest/ruff）。"""
        for name in acceptance.GATE_NAMES:
            monkeypatch.setitem(acceptance.GATE_RUNNERS, name,
                                lambda _ctx: acceptance.GateResult("x", "pass", "假通过"))
        json_path = tmp_path / "out" / "acceptance.json"
        report_path = tmp_path / "out" / "acceptance.md"
        args = acceptance.build_parser().parse_args([
            "--profile", "static",
            "--json", str(json_path),
            "--report", str(report_path),
        ])
        payload = acceptance.run(args, root=tmp_path)
        assert payload["ok"] is True
        assert [gate["name"] for gate in payload["gates"]] == \
            acceptance.select_gates("static")
        assert json.loads(json_path.read_text(encoding="utf-8"))["ok"] is True
        assert "# AutoMind 验收报告" in report_path.read_text(encoding="utf-8")
        assert "JSON 报告" in capsys.readouterr().out

    def test_no_keep_going_stops_after_first_failure(self, tmp_path, monkeypatch):
        def fake_fail(ctx):
            return acceptance.GateResult("version", "fail", "故意失败")

        def fake_pass(ctx):
            return acceptance.GateResult("lint", "pass", "不该被调用")

        monkeypatch.setitem(acceptance.GATE_RUNNERS, "version", fake_fail)
        monkeypatch.setitem(acceptance.GATE_RUNNERS, "lint", fake_pass)
        monkeypatch.setitem(acceptance.GATE_RUNNERS, "web", fake_pass)
        monkeypatch.setitem(acceptance.GATE_RUNNERS, "hygiene", fake_pass)
        args = acceptance.build_parser().parse_args(
            ["--profile", "static", "--no-keep-going"])
        payload = acceptance.run(args, root=tmp_path)
        assert payload["ok"] is False
        assert len(payload["gates"]) == 1
        assert payload["gates"][0]["name"] == "version"

    def test_list_mode_prints_matrix_without_running(self, capsys):
        assert acceptance.main(["--list"]) == 0
        output = capsys.readouterr().out
        assert "version" in output and "hygiene" in output
        assert "fast profile" in output


# ═══════════════════════════════════════════════════════════════
# 3) 版本一致性检查（能定位具体不一致项）
# ═══════════════════════════════════════════════════════════════


class TestVersionCheck:
    def test_consistent_repo_passes(self, tmp_path):
        repo = _make_repo(tmp_path, version="9.8.7")
        problems, _notes = acceptance.check_version(repo)
        assert problems == [], problems

    def test_detects_web_package_mismatch(self, tmp_path):
        repo = _make_repo(tmp_path, version="9.8.7")
        _write(repo / "web" / "package.json", json.dumps({"version": "9.8.6"}))
        problems, _notes = acceptance.check_version(repo)
        assert any("web/package.json" in problem and "9.8.6" in problem
                   for problem in problems), problems

    def test_detects_changelog_missing_entry(self, tmp_path):
        repo = _make_repo(tmp_path, version="9.8.7")
        _write(repo / "CHANGELOG.md", "# 更新日志\n\n## [9.8.5]\n")
        problems, _notes = acceptance.check_version(repo)
        assert any("CHANGELOG.md" in problem and "9.8.7" in problem for problem in problems)

    def test_detects_multiple_mismatches_at_once(self, tmp_path):
        repo = _make_repo(tmp_path, version="9.8.7")
        _write(repo / "pyproject.toml", '[project]\nname = "demo"\nversion = "1.0.0"\n')
        _write(repo / "desktop" / "installer.iss", '#define AppVersion "1.0.0"\n')
        problems, _notes = acceptance.check_version(repo)
        joined = "\n".join(problems)
        assert "pyproject.toml" in joined and "desktop/installer.iss" in joined

    def test_missing_optional_files_are_notes_not_failures(self, tmp_path):
        repo = _make_repo(tmp_path, version="9.8.7")
        (repo / "desktop" / "installer.iss").unlink()
        problems, notes = acceptance.check_version(repo)
        assert problems == [], problems
        assert any("installer.iss" in note for note in notes)

    def test_gate_reports_failure_with_repro(self, tmp_path):
        repo = _make_repo(tmp_path, version="9.8.7")
        _write(repo / "web" / "package.json", json.dumps({"version": "0.0.1"}))
        ctx = acceptance.RunContext(
            root=repo, profile="static", timeout=30, skip_web=False, skip_pro=False,
            clean=False, clean_dry_run=False, data_dir=tmp_path / "data",
            home_dir=tmp_path / "home")
        result = acceptance.gate_version(ctx)
        assert result.status == "fail"
        assert "web/package.json" in result.detail
        assert "复现" in result.detail


# ═══════════════════════════════════════════════════════════════
# 4) 残留分类器：命中已知临时产物，但绝不误伤源码
# ═══════════════════════════════════════════════════════════════


class TestResidueMatcher:
    def test_hits_known_temporary_artifacts(self, tmp_path):
        _write(tmp_path / "_perfcheck" / "probe.py")
        _write(tmp_path / "probe_chart.png")
        _write(tmp_path / "probe_script.py")
        _write(tmp_path / "probe_data.json")
        _write(tmp_path / "probe_note.txt")
        _write(tmp_path / "a.txt")
        _write(tmp_path / "output.txt")
        _write(tmp_path / "out.txt")
        _write(tmp_path / "err.txt")
        _write(tmp_path / "pytest_out.txt")
        _write(tmp_path / "pytest_123.log")
        _write(tmp_path / ".workflow_test_tmp" / "wf.yaml")
        _write(tmp_path / "connector-verify-abc" / "result.json")
        _write(tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids")
        _write(tmp_path / ".ruff_cache" / "0.15" / "hash")
        _write(tmp_path / "pip" / "cache" / "http")
        _write(tmp_path / ".dsh_session_tmp")

        matched = {match.rel_path for match in acceptance.match_residue(tmp_path)}
        expected = {
            "_perfcheck", "probe_chart.png", "probe_script.py", "probe_data.json",
            "probe_note.txt", "a.txt", "output.txt", "out.txt", "err.txt",
            "pytest_out.txt", "pytest_123.log", ".workflow_test_tmp",
            "connector-verify-abc", ".pytest_cache", ".ruff_cache", "pip",
            ".dsh_session_tmp",
        }
        assert expected <= matched, f"漏识别：{expected - matched}"

    def test_never_flags_normal_source_files(self, tmp_path):
        """关键断言：源码 / tests / docs / dist / scripts 正常文件不得被误判。"""
        for rel in (
            "automind/core/edition.py",
            "automind/static/dist/index.html",
            "automind/static/dist/assets/index-abc.js",
            "automind/static/manual.html",
            "scripts/acceptance.py",
            "tests/test_acceptance_gate.py",
            "tests/conftest.py",
            "tests/core/test_edition.py",
            "tests/a.txt",                       # 子目录同名文件：根规则不应命中
            "tests/probe_helpers.py",            # 子目录探针：根规则不应命中
            "docs/ACCEPTANCE.md",
            "docs/images/output.txt",            # 子目录同名文件
            "web/src/main.tsx",
            "demo/output.txt",
            ".github/workflows/ci.yml",
            "examples/06-workflow/sample-access.log",
            "CHANGELOG.md",
            "README.md",
            "pro/automind_pro/__init__.py",
        ):
            _write(tmp_path / rel)
        matches = acceptance.match_residue(tmp_path)
        assert matches == [], [match.rel_path for match in matches]

    def test_protected_dirs_are_pruned_when_expanding(self, tmp_path):
        _write(tmp_path / "_perfcheck" / "report.txt")
        _write(tmp_path / "_perfcheck" / ".git" / "config")
        _write(tmp_path / "_perfcheck" / "node_modules" / "x" / "index.js")
        matches = acceptance.match_residue(tmp_path)
        entry = next(match for match in matches if match.rel_path == "_perfcheck")
        files = acceptance.expand_residue_files(tmp_path, entry)
        assert files == ["_perfcheck/report.txt"]


class TestResidueClassifier:
    def test_tracked_files_are_never_deletable(self, tmp_path):
        _write(tmp_path / "a.txt")
        _write(tmp_path / "probe_chart.png")
        matches = acceptance.match_residue(tmp_path)
        entries = acceptance.classify_residue(tmp_path, matches, tracked={"a.txt"})
        by_path = {entry.rel_path: entry for entry in entries}
        assert by_path["a.txt"].deletable is False
        assert "已跟踪" in by_path["a.txt"].reason
        assert by_path["probe_chart.png"].deletable is True

    def test_without_git_nothing_is_deletable(self, tmp_path):
        _write(tmp_path / "a.txt")
        matches = acceptance.match_residue(tmp_path)
        entries = acceptance.classify_residue(tmp_path, matches, tracked=set(), git_ok=False)
        assert entries and all(not entry.deletable for entry in entries)
        assert all("git 不可用" in entry.reason for entry in entries)

    def test_expand_dir_files_lists_untracked_tree(self, tmp_path):
        _write(tmp_path / "_perfcheck" / "a.txt")
        _write(tmp_path / "_perfcheck" / "sub" / "b.txt")
        matches = acceptance.match_residue(tmp_path)
        entry = next(match for match in matches if match.rel_path == "_perfcheck")
        assert acceptance.expand_residue_files(tmp_path, entry) == [
            "_perfcheck/a.txt", "_perfcheck/sub/b.txt"]


class TestCleanupSafety:
    def test_dry_run_deletes_nothing(self, tmp_path):
        _write(tmp_path / "a.txt")
        _write(tmp_path / "_perfcheck" / "probe.py")
        matches = acceptance.match_residue(tmp_path)
        entries = acceptance.classify_residue(tmp_path, matches, tracked=set())
        result = acceptance.apply_cleanup(tmp_path, entries, dry_run=True)
        assert result.deleted and not result.failed
        assert (tmp_path / "a.txt").is_file()
        assert (tmp_path / "_perfcheck" / "probe.py").is_file()

    def test_real_clean_deletes_only_untracked_residue(self, tmp_path):
        _write(tmp_path / "a.txt")
        _write(tmp_path / "_perfcheck" / "tmp.txt")
        _write(tmp_path / "tests" / "test_keep.py", "def test_x():\n    assert True\n")
        _write(tmp_path / "automind" / "core" / "edition.py")
        matches = acceptance.match_residue(tmp_path)
        entries = acceptance.classify_residue(tmp_path, matches, tracked=set())
        result = acceptance.apply_cleanup(tmp_path, entries, dry_run=False)
        assert sorted(result.deleted) == ["_perfcheck/tmp.txt", "a.txt"]
        assert not (tmp_path / "a.txt").exists()
        assert not (tmp_path / "_perfcheck").exists()          # 空目录一并移除
        assert (tmp_path / "tests" / "test_keep.py").is_file()  # 源码/测试文件毫发无损
        assert (tmp_path / "automind" / "core" / "edition.py").is_file()

    def test_clean_refuses_directory_with_tracked_file(self, tmp_path):
        _write(tmp_path / "_perfcheck" / "keep.py")
        _write(tmp_path / "_perfcheck" / "tmp.txt")
        _write(tmp_path / "a.txt")
        matches = acceptance.match_residue(tmp_path)
        entries = acceptance.classify_residue(
            tmp_path, matches, tracked={"_perfcheck/keep.py"})
        result = acceptance.apply_cleanup(tmp_path, entries, dry_run=False)
        assert (tmp_path / "_perfcheck" / "keep.py").is_file()
        assert (tmp_path / "_perfcheck" / "tmp.txt").is_file()  # 整条命中被保护
        assert any("_perfcheck" in path for path in result.protected)
        assert not (tmp_path / "a.txt").exists()                 # 其余安全项照常清理

    def test_format_residue_entries_marks_protected(self, tmp_path):
        _write(tmp_path / "a.txt")
        matches = acceptance.match_residue(tmp_path)
        entries = acceptance.classify_residue(tmp_path, matches, tracked={"a.txt"})
        text = acceptance.format_residue_entries(entries)
        assert "a.txt" in text
        assert "排除" in text and "已跟踪" in text

    def test_clean_mode_fails_when_whitelist_hits_tracked_file(self, tmp_path, monkeypatch):
        """误判防护：清理模式下白名单命中已跟踪文件 → 拒绝删除并判 fail（给出手册）。"""
        ctx = acceptance.RunContext(
            root=tmp_path, profile="static", timeout=30, skip_web=False, skip_pro=False,
            clean=True, clean_dry_run=False, data_dir=tmp_path / "data",
            home_dir=tmp_path / "home")
        protected = acceptance.ResidueEntry(
            rel_path="a.txt", pattern="scratch-a-txt", kind="file", description="测试",
            files=["a.txt"], deletable=False,
            reason="包含已跟踪文件（a.txt），绝不删除")
        monkeypatch.setattr(acceptance, "scan_residue", lambda _ctx: [protected])
        problems, _warnings, _checks = acceptance.run_residue_hygiene(ctx)
        assert problems and "安全闸" in problems[0]
        assert "RESIDUE_PATTERNS" in problems[0]

    def test_real_git_clean_protects_tracked_file(self, tmp_path):
        """端到端（真实 git）：已跟踪的 a.txt 绝不删除，未跟踪临时产物正常清理。"""
        git = shutil.which("git")
        if not git:                                 # pragma: no cover - 环境缺 git
            pytest.skip("本机没有 git，跳过真实 git 分类验证")
        _run = [git, "-C", str(tmp_path)]
        subprocess.run([*_run, "init", "-q"], check=True)
        _write(tmp_path / "a.txt", "tracked content")
        subprocess.run([*_run, "add", "a.txt"], check=True)
        _write(tmp_path / "probe_x.png", "x")
        _write(tmp_path / "_perfcheck" / "t.txt", "t")
        _write(tmp_path / "automind" / "core" / "edition.py", "x = 1")
        ctx = acceptance.RunContext(
            root=tmp_path, profile="static", timeout=30, skip_web=False, skip_pro=False,
            clean=True, clean_dry_run=False, data_dir=tmp_path / "data",
            home_dir=tmp_path / "home")
        problems, _warnings, _checks = acceptance.run_residue_hygiene(ctx)
        assert problems, "白名单命中已跟踪文件时必须 fail（安全闸）"
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "tracked content"
        assert not (tmp_path / "probe_x.png").exists()
        assert not (tmp_path / "_perfcheck").exists()
        assert (tmp_path / "automind" / "core" / "edition.py").is_file()


# ═══════════════════════════════════════════════════════════════
# 5) 子进程超时 / 产物完整性 / 私钥候选
# ═══════════════════════════════════════════════════════════════


class TestCommandExecution:
    def test_parse_pytest_counts_from_summary_line(self):
        """pytest -q 的汇总行（-qq 时会被吞掉，所以命令里不能重复 -q）。"""
        text = (
            "..........F\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_x.py::test_y\n"
            "1 failed, 9 passed, 2 skipped in 0.08s\n"
        )
        assert acceptance.parse_pytest_counts(text) == {
            "failed": 1, "passed": 9, "skipped": 2}
        assert acceptance.format_pytest_counts({"passed": 3, "skipped": 1}) == "通过 3 / 跳过 1"

    def test_timeout_is_reported_as_failure(self, tmp_path):
        result = acceptance.run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path, timeout=0.5)
        assert result.timed_out is True
        assert result.ok is False
        assert 0 <= result.duration_s < 10

    def test_normal_command_captures_output(self, tmp_path):
        result = acceptance.run_command(
            [sys.executable, "-c", "print('ACCEPTANCE-OK')"], cwd=tmp_path, timeout=60)
        assert result.ok and result.returncode == 0
        assert "ACCEPTANCE-OK" in result.output

    def test_tests_gate_marks_timeout_as_fail(self, tmp_path, monkeypatch):
        ctx = acceptance.RunContext(
            root=tmp_path, profile="fast", timeout=0.3, skip_web=False, skip_pro=False,
            clean=False, clean_dry_run=False, data_dir=tmp_path / "data",
            home_dir=tmp_path / "home")
        monkeypatch.setattr(
            acceptance, "_tests_command_and_env",
            lambda _context: ([sys.executable, "-c", "import time; time.sleep(5)"],
                              {}, ["fake"], []))
        result = acceptance.gate_tests(ctx)
        assert result.status == "fail"
        assert "未结束" in result.detail
        assert "复现" in result.detail


class TestDistChecks:
    def test_complete_dist_passes(self, tmp_path):
        static = tmp_path / "static"
        _write(static / "dist" / "index.html",
               '<script src="/static/dist/assets/index-a.js"></script>'
               '<link href="/static/dist/assets/index-b.css">')
        _write(static / "dist" / "assets" / "index-a.js", "console.log(1)")
        _write(static / "dist" / "assets" / "index-b.css", "body{}")
        problems, checks = acceptance.check_dist(static)
        assert problems == [] and checks

    def test_missing_asset_is_reported(self, tmp_path):
        static = tmp_path / "static"
        _write(static / "dist" / "index.html",
               '<script src="/static/dist/assets/index-a.js"></script>')
        problems, _checks = acceptance.check_dist(static)
        assert problems and "index-a.js" in problems[0]

    def test_release_freshness_detects_stale_dist(self, tmp_path):
        static = tmp_path / "static"
        index = _write(static / "dist" / "index.html", "x")
        src = _write(tmp_path / "web" / "src" / "main.tsx", "export {}")
        now = time.time()
        os.utime(index, (now - 100, now - 100))        # 产物比源码旧 100 秒
        os.utime(src, (now, now))
        problems, _checks = acceptance.check_dist_freshness(tmp_path, static)
        assert problems and "重建前端产物" in problems[0]
        # 反过来：产物更新时不应报错
        os.utime(index, (now + 100, now + 100))
        problems2, checks2 = acceptance.check_dist_freshness(tmp_path, static)
        assert problems2 == [] and checks2


class TestPrivateKeyScan:
    def test_scan_finds_issuer_key_file(self, tmp_path):
        _write(tmp_path / "pro" / ".license-private" / "private_key.hex")
        _write(tmp_path / "automind" / "core" / "edition.py", "x = 1\n")
        candidates = acceptance.scan_private_key_candidates(tmp_path)
        assert "pro/.license-private/private_key.hex" in candidates

    def test_normal_python_sources_are_not_candidates(self, tmp_path):
        _write(tmp_path / "automind" / "core" / "edition.py", "x = 1\n")
        _write(tmp_path / "pro" / "automind_pro" / "licensing" / "keys.py", "PUBLIC = 'x'\n")
        assert acceptance.scan_private_key_candidates(tmp_path) == []
