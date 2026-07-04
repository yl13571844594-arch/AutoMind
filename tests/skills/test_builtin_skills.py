"""新增内置技能测试（§14.8）— log_analyzer / doc_generator / dep_audit。"""

import asyncio

from automind.skills.builtin.dep_audit import DependencyAuditSkill
from automind.skills.builtin.doc_generator import DocGeneratorSkill
from automind.skills.builtin.log_analyzer import LogAnalyzerSkill


class TestLogAnalyzer:
    LOG = "\n".join([
        "2026-06-30 10:00:01 INFO started ok",
        "2026-06-30 10:00:02 ERROR connect failed: ValueError host 12",
        "2026-06-30 10:00:03 WARNING retry 3",
        "2026-06-30 10:00:04 ERROR connect failed: ValueError host 45",
        "2026-06-30 10:00:05 CRITICAL out of memory",
    ])

    def test_level_and_error_counts(self):
        r = asyncio.run(LogAnalyzerSkill().execute({"log_text": self.LOG}))
        assert r.success
        s = r.output
        assert s["levels"]["ERROR"] == 2
        assert s["levels"]["CRITICAL"] == 1
        assert s["error_count"] == 3  # ERROR + CRITICAL

    def test_top_exceptions_and_patterns(self):
        r = asyncio.run(LogAnalyzerSkill().execute({"log_text": self.LOG}))
        s = r.output
        assert s["top_exceptions"][0]["name"] == "ValueError"
        assert s["top_exceptions"][0]["count"] == 2
        # 两条 ERROR 归一化后应聚成同一模式（数字被替换）
        assert s["top_patterns"][0]["count"] == 2

    def test_empty_fails(self):
        r = asyncio.run(LogAnalyzerSkill().execute({"log_text": "   "}))
        assert not r.success

    def test_report_written(self, tmp_path):
        out = tmp_path / "report.md"
        r = asyncio.run(LogAnalyzerSkill().execute(
            {"log_text": self.LOG, "report_file": str(out)}))
        assert r.success and out.exists()
        assert "日志分析报告" in out.read_text(encoding="utf-8")


class TestDocGenerator:
    def test_extracts_signatures_and_docstrings(self, tmp_path):
        src = tmp_path / "m.py"
        src.write_text(
            '"""Module doc."""\n'
            "def foo(a: int, b: str = 'x') -> bool:\n"
            '    """Foo does things."""\n'
            "    return True\n"
            "class C:\n"
            '    """A class."""\n'
            "    def method(self, x): ...\n",
            encoding="utf-8",
        )
        r = asyncio.run(DocGeneratorSkill().execute({"source": str(src)}))
        assert r.success
        md = r.output["markdown"]
        assert "Module doc." in md
        assert "foo(a: int, b: str='x') -> bool" in md
        assert "class `C`" in md
        assert "Foo does things." in md
        assert r.output["symbols"] == 3  # foo + C + method

    def test_private_excluded_by_default(self, tmp_path):
        src = tmp_path / "p.py"
        src.write_text("def _hidden():\n    pass\ndef shown():\n    pass\n", encoding="utf-8")
        r = asyncio.run(DocGeneratorSkill().execute({"source": str(src)}))
        assert "_hidden" not in r.output["markdown"]
        assert "shown" in r.output["markdown"]

    def test_missing_source_fails(self, tmp_path):
        r = asyncio.run(DocGeneratorSkill().execute({"source": str(tmp_path / "nope.py")}))
        assert not r.success

    def test_directory_source(self, tmp_path):
        (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
        r = asyncio.run(DocGeneratorSkill().execute({"source": str(tmp_path)}))
        assert r.success and r.output["files"] == 2


class TestDepAudit:
    def test_requirements_parsing(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "pydantic>=2.0\nrequests\nrequests\n# a comment\n-e .\n", encoding="utf-8")
        r = asyncio.run(DependencyAuditSkill().execute({"path": str(tmp_path)}))
        assert r.success
        s = r.output
        assert s["total"] == 3  # pydantic, requests, requests
        assert "requests" in s["unpinned"]
        assert "requests" in s["duplicates"]

    def test_pyproject_parsing(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "click"]\n'
            '[project.optional-dependencies]\ndev = ["pytest>=8"]\n',
            encoding="utf-8",
        )
        r = asyncio.run(DependencyAuditSkill().execute({"path": str(tmp_path)}))
        assert r.success
        names = {d["name"] for d in r.output["dependencies"]}
        assert {"httpx", "click", "pytest"} <= names
        assert "click" in r.output["unpinned"]

    def test_no_deps_files_fails(self, tmp_path):
        r = asyncio.run(DependencyAuditSkill().execute({"path": str(tmp_path)}))
        assert not r.success

    def test_report_written(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        out = tmp_path / "audit.md"
        r = asyncio.run(DependencyAuditSkill().execute(
            {"path": str(tmp_path), "report_file": str(out)}))
        assert r.success and out.exists()


class TestRegistration:
    def test_all_builtins_registered(self):
        from automind.skills.skill_registry import SkillRegistry
        reg = SkillRegistry()
        n = reg.register_builtin_skills()
        assert n == 6
        for name in ("log_analyzer", "doc_generator", "dep_audit"):
            assert name in reg
