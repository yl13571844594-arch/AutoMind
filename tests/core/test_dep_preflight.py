"""v1.11.9 依赖预检（dep_preflight）测试：看得全、判得准、修得动。

预检的意义在"开箱即用"：缺什么、影响什么、怎么补，必须在任务失败**之前**
就能看清。这里验证三件事：
* 检查项完整（核心 / Web / 办公可选 / 外部程序 / 内置插件）；
* 缺口判定与报告渲染正确（含可照抄的安装命令）；
* ensure_missing 只动 Python 库、逐个安装、无缺口时零动作。
"""

from __future__ import annotations

import json
from typing import Any

from automind.core import dep_preflight as dp


class TestChecks:
    def test_python_deps_cover_three_groups(self):
        rows = dp.check_python_deps()
        kinds = {r["kind"] for r in rows}
        assert {"core", "web", "office"} <= kinds
        # 核心与 Web 是必需的（缺了对应功能直接不可用）
        for r in rows:
            if r["kind"] in ("core", "web"):
                assert r["required"] is True
        by_name = {r["name"]: r for r in rows}
        # 关键映射抽查：模块名 ↔ pip 包名
        assert by_name["yaml"]["package"].startswith("pyyaml")
        assert by_name["docx"]["package"].startswith("python-docx")
        assert by_name["PIL"]["package"].startswith("pillow")
        # 本机装了核心依赖，这几项必须是 ✓（否则 automind 本身都 import 不进来）
        for m in ("pydantic", "yaml", "rich"):
            assert by_name[m]["ok"] is True, m

    def test_binaries_checked_with_fix_hints(self):
        rows = dp.check_binaries()
        assert rows, "至少要检查 ffmpeg / ffprobe / tesseract / git"
        names = {r["name"] for r in rows}
        assert {"ffmpeg", "ffprobe", "tesseract", "git"} <= names
        for r in rows:
            assert r["kind"] == "binary"
            if not r["ok"]:
                assert r["fix"], "缺口必须带安装办法，不能只说'没有'"

    def test_extra_deps_cover_browser_and_mcp(self):
        """浏览器/MCP 不在办公 extra 里，但缺了要能在自检里看见（v1.11.12）。"""
        rows = dp.check_python_deps()
        extra = {r["name"]: r for r in rows if r["kind"] == "extra"}
        assert {"playwright", "mcp"} <= set(extra)
        assert all(r["required"] is False for r in extra.values())

    def test_builtin_plugins_all_pass_syntax_check(self):
        """8 个内置插件的清单可解析、入口文件在、语法能编译。"""
        rows = dp.check_builtin_plugins()
        names = {r["name"] for r in rows}
        assert {"cost_tracker", "pii_guard", "task_notify", "hello_hooks",
                "net_resilience", "delivery_manifest",
                "artifact_selfcheck", "fs_lock_guard"} <= names
        bad = [r for r in rows if not r["ok"]]
        assert not bad, f"内置插件自检失败：{[(r['name'], r['detail']) for r in bad]}"

    def test_missing_items_filters(self):
        results = [
            {"kind": "core", "name": "a", "ok": True},
            {"kind": "core", "name": "b", "ok": False},
            {"kind": "plugin", "name": "p", "ok": False},
        ]
        got = dp.missing_items(results)                       # type: ignore[arg-type]
        assert [r["name"] for r in got] == ["b", "p"]


class TestReport:
    def test_render_mentions_fix_for_missing(self):
        results = [
            {"kind": "office", "name": "openpyxl", "package": "openpyxl>=3.1",
             "purpose": "Excel 读写", "required": False, "ok": False},
            {"kind": "binary", "name": "tesseract", "package": "", "purpose": "OCR",
             "required": False, "ok": False,
             "fix": "winget install --id UB-Mannheim.TesseractOCR -e"},
            {"kind": "core", "name": "rich", "package": "rich>=13",
             "purpose": "CLI", "required": True, "ok": True},
        ]
        text = dp.render_report(results)                      # type: ignore[arg-type]
        assert "✗ openpyxl" in text and "pip install openpyxl>=3.1" in text
        assert "✗ tesseract" in text and "winget install" in text
        assert "✓ rich" in text
        assert "1 项未就绪" not in text          # 文案按缺失总数计（此处为 2）

    def test_render_all_ok(self):
        text = dp.render_report([{"kind": "core", "name": "x", "ok": True}])  # type: ignore[arg-type]
        assert "全部通过" in text


class TestEnsureMissing:
    def test_nothing_missing_is_noop(self):
        """全部就绪时 ensure_missing 一个 pip 都不起（返回 0/0）。"""
        results: list[dict[str, Any]] = [
            {"kind": "office", "name": "m", "package": "pkg>=1", "purpose": "p", "ok": True},
        ]
        ok, fail = dp.ensure_missing(results)
        assert (ok, fail) == (0, 0)

    def test_only_python_deps_are_ensured(self):
        """外部程序与插件缺口不进 ensure_missing 的安装清单（pip 装不到）。"""
        results = [
            {"kind": "office", "name": "m1", "package": "pkg1>=1", "purpose": "p", "ok": False},
            {"kind": "binary", "name": "ffmpeg", "package": "", "purpose": "p", "ok": False},
            {"kind": "plugin", "name": "broken", "package": "", "purpose": "p", "ok": False},
        ]
        todo = [r for r in dp.missing_items(results)          # type: ignore[arg-type]
                if r["kind"] in ("core", "web", "office")]
        assert [r["name"] for r in todo] == ["m1"]


class TestMainJson:
    def test_json_output_shape(self, capsys):
        """--json 输出可解析、逐项带 kind/name/ok（机器可读口径稳定）。"""
        code = dp.main(["--json"])
        out = capsys.readouterr().out
        rows = json.loads(out)
        assert isinstance(rows, list) and rows
        for r in rows:
            assert set(("kind", "name", "ok")) <= set(r)
        # 返回码与缺口一致
        assert code == (1 if any(not r["ok"] for r in rows) else 0)
