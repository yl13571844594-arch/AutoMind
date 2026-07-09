"""编程准确度增强的回归测试 —
file_edit 最近匹配提示 + ReAct 自动验证（多文件 / JSON / 解析后路径）。
"""

from __future__ import annotations

import asyncio

import pytest

from automind.core.types import ToolCall, ToolResult
from automind.planning.react_executor import ReActExecutor
from automind.tools.base import ToolRegistry
from automind.tools.file_editor import FileEditTool


def _executor() -> ReActExecutor:
    return ReActExecutor(llm=None, tool_registry=ToolRegistry(), auto_validate=True)


class TestNearestMatchHint:
    """file_edit 精确匹配失败时应回显文件中最接近的片段。"""

    def test_indentation_mismatch_gives_hint(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("def main():\n    value = compute()\n    return value\n",
                     encoding="utf-8")
        tool = FileEditTool()
        # 模型给的 old_string 缩进错误（制表符而非 4 空格）
        r = asyncio.run(tool.execute(path=str(f), old_string="\tvalue = compute()",
                                     new_string="\tvalue = compute(x)"))
        assert not r.success
        assert "Nearest match" in r.error
        assert "value = compute()" in r.error  # 回显真实行
        assert "2\t" in r.error  # 带行号

    def test_totally_absent_string_no_hint(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("print('hello')\n", encoding="utf-8")
        tool = FileEditTool()
        r = asyncio.run(tool.execute(path=str(f), old_string="THIS_NEVER_EXISTS_ANYWHERE",
                                     new_string="x"))
        assert not r.success
        assert "not found" in r.error

    def test_exact_match_still_works(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("a = 1\nb = 2\n", encoding="utf-8")
        tool = FileEditTool()
        r = asyncio.run(tool.execute(path=str(f), old_string="b = 2", new_string="b = 3"))
        assert r.success
        assert f.read_text(encoding="utf-8") == "a = 1\nb = 3\n"


class TestAutoValidateEnhanced:
    """ReAct TDD 内环：解析后路径优先、multi_edit 全覆盖、JSON 校验。"""

    def test_resolved_path_from_output_preferred(self, tmp_path):
        # 参数里是相对路径（相对项目根，CWD 下不存在），输出里是解析后的绝对路径
        f = tmp_path / "mod.py"
        f.write_text("x = 1\n", encoding="utf-8")
        ex = _executor()
        tc = ToolCall(id="1", name="file_write",
                      arguments={"path": "mod.py"})
        r = ToolResult(tool_name="file_write", success=True, output={"path": str(f)})
        out = ex._auto_validate_result(tc, r)
        assert out.output["auto_validation"] == "syntax_check: OK"

    def test_multi_edit_validates_each_file(self, tmp_path):
        good = tmp_path / "good.py"
        good.write_text("a = 1\n", encoding="utf-8")
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        ex = _executor()
        tc = ToolCall(id="1", name="file_multi_edit", arguments={"edits": []})
        r = ToolResult(tool_name="file_multi_edit", success=True, output={
            "results": [
                {"path": "good.py", "success": True, "output": {"path": str(good)}},
                {"path": "bad.py", "success": True, "output": {"path": str(bad)}},
            ],
        })
        out = ex._auto_validate_result(tc, r)
        note = out.output["auto_validation"]
        assert "FAILED" in note and str(bad) in note
        assert len(ex.validations) == 2

    def test_json_validation(self, tmp_path):
        f = tmp_path / "config.json"
        f.write_text('{"key": }', encoding="utf-8")
        ex = _executor()
        tc = ToolCall(id="1", name="file_write", arguments={"path": str(f)})
        r = ToolResult(tool_name="file_write", success=True, output={"path": str(f)})
        out = ex._auto_validate_result(tc, r)
        assert "invalid JSON" in out.output["auto_validation"]
        assert ex.validations[-1]["ok"] is False

    def test_failed_multi_edit_entries_skipped(self, tmp_path):
        ex = _executor()
        tc = ToolCall(id="1", name="file_multi_edit", arguments={"edits": []})
        r = ToolResult(tool_name="file_multi_edit", success=True, output={
            "results": [{"path": "x.py", "success": False, "error": "not found",
                         "output": None}],
        })
        out = ex._auto_validate_result(tc, r)
        assert "auto_validation" not in (out.output or {})

    def test_yaml_validation(self, tmp_path):
        pytest.importorskip("yaml")
        f = tmp_path / "cfg.yaml"
        f.write_text("key: [unclosed\n", encoding="utf-8")
        ex = _executor()
        tc = ToolCall(id="1", name="file_write", arguments={"path": str(f)})
        r = ToolResult(tool_name="file_write", success=True, output={"path": str(f)})
        out = ex._auto_validate_result(tc, r)
        assert "FAILED" in out.output["auto_validation"]

    def test_toml_validation(self, tmp_path):
        good = tmp_path / "ok.toml"
        good.write_text('[tool]\nname = "x"\n', encoding="utf-8")
        bad = tmp_path / "bad.toml"
        bad.write_text("[tool\n", encoding="utf-8")
        ex = _executor()
        for f, expect in ((good, "OK"), (bad, "FAILED")):
            tc = ToolCall(id="1", name="file_write", arguments={"path": str(f)})
            r = ToolResult(tool_name="file_write", success=True, output={"path": str(f)})
            out = ex._auto_validate_result(tc, r)
            assert expect in out.output["auto_validation"]


class TestFileReadRanges:
    """file_read 分段读取与大文件截断（保护上下文 → 提升编码准确度）。"""

    def test_offset_limit(self, tmp_path):
        from automind.tools.file_editor import FileReadTool
        f = tmp_path / "big.txt"
        f.write_text("".join(f"line{i}\n" for i in range(1, 101)), encoding="utf-8")
        tool = FileReadTool()
        r = asyncio.run(tool.execute(path=str(f), offset=10, limit=5))
        assert r.success
        assert r.output["content"] == "line10\nline11\nline12\nline13\nline14\n"
        assert r.output["range"] == "lines 10-14 of 100"
        assert r.output["total_lines"] == 100

    def test_large_file_truncated_with_note(self, tmp_path):
        from automind.tools.file_editor import FileReadTool
        f = tmp_path / "huge.txt"
        f.write_text("x" * 200 + "\n" * 1 + ("y" * 100 + "\n") * 2000, encoding="utf-8")
        tool = FileReadTool()
        r = asyncio.run(tool.execute(path=str(f)))
        assert r.success and r.output.get("truncated") is True
        assert "offset/limit" in r.output["note"]
        assert len(r.output["content"]) < len(f.read_text(encoding="utf-8"))

    def test_small_file_unchanged(self, tmp_path):
        from automind.tools.file_editor import FileReadTool
        f = tmp_path / "s.txt"
        f.write_text("hello", encoding="utf-8")
        r = asyncio.run(FileReadTool().execute(path=str(f)))
        assert r.success and r.output["content"] == "hello"
        assert "truncated" not in r.output


class TestNoOpEditGuard:
    def test_identical_strings_rejected(self, tmp_path):
        f = tmp_path / "n.py"
        f.write_text("a = 1\n", encoding="utf-8")
        r = asyncio.run(FileEditTool().execute(
            path=str(f), old_string="a = 1", new_string="a = 1"))
        assert not r.success and "identical" in r.error
