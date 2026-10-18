"""v1.11.13 工具调用成功率提升：参数归一 / 路径锚定 / 编码回退 / 插件自检 / 终端兜底。

这些用例全部来自实跑复现的失败模式（IM 机器人与 Web 会话工作模式默认开启目录
隔离，旧实现下它们高频出现）：

* ``file_write`` 明明成功，却被 artifact_selfcheck 按服务进程 cwd 回读 → 误判
  "文件不存在"（隔离工作区里**每次**都会中）；
* office/media/archive 工具按进程 cwd 解析相对路径 → 产物写到 AutoMind 自己的
  目录，用户找不到，也混进了别的会话/机器人工作区；
* 模型把参数写成 ``file_path`` / ``cmd`` / ``limit``，或把列表再序列化一层
  （``"['a.txt','b.txt']"``）→ 直接失败；
* 中文 Windows 上 GBK 文本用 UTF-8 读，``file_read`` 直接异常；
* cmd.exe 遇到链式命令里的 ``ls``（``cd x && ls``）不会触发换壳兜底。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import automind
from automind.core import session_ctx
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import (
    ToolRegistry,
    absolutize_paths,
    apply_param_aliases,
    coerce_param_types,
)
from automind.tools.file_editor import FileReadTool, FileWriteTool

# ── 测试替身 ────────────────────────────────────────────────


class _FakeTool:
    name = "fake"
    description = "fake"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "output": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
            "workdir": {"type": "string"},
            "timeout": {"type": "number"},
            "background": {"type": "boolean"},
        },
        "required": ["path"],
    }
    permission_tier = PermissionTier.SAFE

    async def execute(self, **kwargs):
        return ToolResult(tool_name=self.name, success=True, output=dict(kwargs))


def _load_plugin(name: str):
    hooks = Path(automind.__file__).parent / "builtin_plugins" / name / "hooks.py"
    spec = importlib.util.spec_from_file_location(f"v1113_{name}", hooks)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ═══════════════════════════════════════════════════════════
# 参数归一化
# ═══════════════════════════════════════════════════════════


class TestParamAliases:
    def test_common_aliases_map_to_schema_names(self):
        tool = _FakeTool()
        out, renamed = apply_param_aliases(
            tool, {"file_path": "a.txt", "cmd": "x", "limit": 3})
        assert out["path"] == "a.txt"
        assert renamed.get("file_path") == "path"

    def test_alias_does_not_override_explicit_param(self):
        out, renamed = apply_param_aliases(
            _FakeTool(), {"path": "real.txt", "file_path": "other.txt"})
        assert out["path"] == "real.txt"
        assert "file_path" in out and "file_path" not in renamed

    def test_coerce_python_literal_list(self):
        tool = _FakeTool()
        out, fixed = coerce_param_types(
            tool, {"files": "['a.txt', 'b.txt']", "timeout": "60",
                   "background": "true"})
        assert out["files"] == ["a.txt", "b.txt"]
        assert out["timeout"] == 60 and out["background"] is True
        assert set(fixed) == {"files", "timeout", "background"}

    def test_coerce_code_fenced_json(self):
        tool = _FakeTool()
        out, _ = coerce_param_types(
            tool, {"sources": "```json\n[\"a\", \"b\"]\n```"})
        assert out["sources"] == ["a", "b"]


# ═══════════════════════════════════════════════════════════
# 路径锚定（工作区不混淆的核心）
# ═══════════════════════════════════════════════════════════


class TestPathAnchoring:
    def test_relative_paths_anchor_to_session_workspace(self, tmp_path):
        project = tmp_path / "proj"
        ws = project / ".automind" / "workspaces" / "bot-x"
        ws.mkdir(parents=True)
        with session_ctx.bind_session("bot-x", workspace=str(ws),
                                      project_root=str(project)):
            out, changed = absolutize_paths(_FakeTool(), {
                "path": "a.txt", "output": "out/b.txt",
                "sources": ["s1.txt", "s2.txt"],
                "files": [{"path": "f.txt", "description": "d"}],
            })
        assert out["path"] == str(ws / "a.txt")
        assert out["output"] == str(ws / "out/b.txt")
        assert out["sources"] == [str(ws / "s1.txt"), str(ws / "s2.txt")]
        assert out["files"][0]["path"] == str(ws / "f.txt")
        assert set(changed) >= {"path", "output", "sources", "files"}

    def test_absolute_urls_and_unknown_keys_untouched(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        with session_ctx.bind_session("s", project_root=str(project)):
            out, _ = absolutize_paths(_FakeTool(), {
                "path": "https://example.com/x",
                "command": "ls -la",
                "query": "hello",
            })
        assert out["path"] == "https://example.com/x"
        assert out["command"] == "ls -la" and out["query"] == "hello"

    def test_no_session_context_keeps_legacy_behavior(self):
        out, changed = absolutize_paths(_FakeTool(), {"path": "a.txt"})
        assert out["path"] == "a.txt" and changed == []

    def test_non_path_input_key_not_rewritten(self, tmp_path):
        """skill_invoke.input 是技能输入文本，不是路径，不能被锚成绝对路径。"""
        class _SkillLike:
            name = "skill_invoke"
            parameters = {"type": "object",
                          "properties": {"input": {"type": "string"}}}

        project = tmp_path / "proj"
        project.mkdir()
        with session_ctx.bind_session("s", project_root=str(project)):
            out, changed = absolutize_paths(_SkillLike(), {"input": "检查环境"})
        assert out["input"] == "检查环境" and changed == []

    def test_quote_wrapped_path_unwrapped(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        with session_ctx.bind_session("s", project_root=str(project)):
            out, _ = absolutize_paths(_FakeTool(), {"path": '"a.txt"'})
        assert out["path"] == str(project / "a.txt")

    def test_two_sessions_do_not_share_workspace(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        a = project / ".automind" / "workspaces" / "web-A"
        b = project / ".automind" / "workspaces" / "bot-B"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        async def write(ws: Path, text: str) -> None:
            reg = ToolRegistry()
            reg.register(FileWriteTool(project_root=project))
            with session_ctx.bind_session("x", workspace=str(ws),
                                          project_root=str(project)):
                r = await reg.dispatch("file_write", path="shared.txt", content=text)
                assert r.success, r.error

        import asyncio

        asyncio.run(write(a, "web"))
        asyncio.run(write(b, "bot"))
        assert (a / "shared.txt").read_text(encoding="utf-8") == "web"
        assert (b / "shared.txt").read_text(encoding="utf-8") == "bot"
        assert not (project / "shared.txt").exists(), "不应写进项目根/别的会话"

    def test_file_search_only_sees_own_workspace(self, tmp_path):
        """file_search 不能把别的会话/机器人副本当项目文件返回（v1.11.13）。"""
        import asyncio

        from automind.tools.data_tools import FileSearchTool

        project = tmp_path / "proj"
        own = project / ".automind" / "workspaces" / "bot-mine"
        other = project / ".automind" / "workspaces" / "bot-other"
        own.mkdir(parents=True)
        other.mkdir(parents=True)
        (own / "mine.txt").write_text("mine", encoding="utf-8")
        (other / "secret.txt").write_text("other session", encoding="utf-8")
        (project / "root.txt").write_text("root", encoding="utf-8")
        reg = ToolRegistry()
        reg.register(FileSearchTool(project_root=project))
        with session_ctx.bind_session("bot-mine", workspace=str(own),
                                      project_root=str(project)):
            r = asyncio.run(reg.dispatch("file_search", pattern="*.txt"))
        assert r.success, r.error
        paths = [m["path"] for m in r.output["matches"]]
        assert "mine.txt" in paths
        assert "secret.txt" not in paths and "root.txt" not in paths
        assert not any(p.startswith(".automind") for p in paths)


# ═══════════════════════════════════════════════════════════
# file_read / file_write 健壮性
# ═══════════════════════════════════════════════════════════


class TestFileTools:
    def test_gbk_text_read_falls_back(self, tmp_path):
        p = tmp_path / "gbk.txt"
        p.write_text("中文内容", encoding="gbk")
        r = __import__("asyncio").run(
            FileReadTool(project_root=tmp_path).execute(path="gbk.txt"))
        assert r.success, r.error
        assert r.output["encoding"] in ("gb18030", "gbk")
        assert "中文内容" in r.output["content"]

    def test_missing_file_suggests_similar(self, tmp_path):
        (tmp_path / "report.json").write_text("{}", encoding="utf-8")
        r = __import__("asyncio").run(
            FileReadTool(project_root=tmp_path).execute(path="reprot.json"))
        assert not r.success
        assert "文件不存在" in r.error
        assert r.output["similar"] and "report.json" in r.output["similar"][0]

    def test_dir_read_returns_actionable_hint(self, tmp_path):
        (tmp_path / "sub").mkdir()
        r = __import__("asyncio").run(
            FileReadTool(project_root=tmp_path).execute(path="sub"))
        assert not r.success and "目录" in r.error and "file_search" in r.error

    def test_write_non_string_content_is_serialized(self, tmp_path):
        r = __import__("asyncio").run(
            FileWriteTool(project_root=tmp_path).execute(
                path="a.json", content={"k": "值"}))
        assert r.success, r.error
        assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["k"] == "值"


# ═══════════════════════════════════════════════════════════
# 插件：隔离工作区里的产物自检/交付清单
# ═══════════════════════════════════════════════════════════


class TestPluginWorkspacePaths:
    def test_artifact_selfcheck_passes_in_session_workspace(self, tmp_path):
        mod = _load_plugin("artifact_selfcheck")
        project = tmp_path / "proj"
        ws = project / ".automind" / "workspaces" / "bot-x"
        ws.mkdir(parents=True)
        target = ws / "ok.txt"
        target.write_text("hello", encoding="utf-8")

        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=True,
                              output={"path": str(target)})

        import asyncio

        with session_ctx.bind_session("bot-x", workspace=str(ws),
                                      project_root=str(project)):
            res = asyncio.run(mod.get_middleware()(
                "file_write", {"path": "ok.txt"}, call_next))
        assert res.success, res.error

    def test_artifact_selfcheck_still_catches_missing_file(self, tmp_path):
        mod = _load_plugin("artifact_selfcheck")
        project = tmp_path / "proj"
        ws = project / ".automind" / "workspaces" / "bot-x"
        ws.mkdir(parents=True)

        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=True,
                              output={"path": "relative-but-not-returned.txt"})

        import asyncio

        with session_ctx.bind_session("bot-x", workspace=str(ws),
                                      project_root=str(project)):
            res = asyncio.run(mod.get_middleware()(
                "file_write", {"path": "missing.txt"}, call_next))
        assert not res.success and "文件不存在" in res.error

    def test_delivery_manifest_records_workspace_absolute_path(self, tmp_path):
        mod = _load_plugin("delivery_manifest")
        project = tmp_path / "proj"
        ws = project / ".automind" / "workspaces" / "bot-x"
        ws.mkdir(parents=True)
        target = ws / "out.txt"
        target.write_text("data", encoding="utf-8")
        mod._records.clear()

        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=True,
                              output={"path": str(target)})

        import asyncio

        with session_ctx.bind_session("bot-x", workspace=str(ws),
                                      project_root=str(project)):
            asyncio.run(mod.get_middleware()(
                "file_write", {"path": "out.txt"}, call_next))
        assert mod._records and mod._records[0]["path"] == str(target)
        assert mod._records[0]["size"] == 4


# ═══════════════════════════════════════════════════════════
# 终端：链式命令里的 Unix 写法兜底
# ═══════════════════════════════════════════════════════════


class TestTerminalFallback:
    def test_extracts_all_unknown_commands(self):
        from automind.tools import terminal as term

        err = ("'ls' 不是内部或外部命令，也不是可运行的程序\n"
               "'grep' 不是内部或外部命令，也不是可运行的程序")
        assert term._unknown_commands(err) == ["ls", "grep"]

    def test_repairs_ls_inside_mixed_cmd_chain(self):
        from automind.tools import terminal as term

        repaired = term._repair_cmd_command('cd /d "C:\\proj" && ls', "ls")
        assert repaired.endswith("dir") and "cd /d" in repaired

    def test_unsafe_prefix_blocks_retry(self):
        from automind.tools import terminal as term

        assert not term._safe_prefix_before("pip install x && ls", "ls")
        assert term._safe_prefix_before("cd /d C:\\x && echo hi && ls", "ls")

    def test_posix_fallback_runs_ls_via_other_shell(self):
        from automind.tools import terminal as term

        if not term._git_bash():
            pytest.skip("本机没有 Git Bash")
        tool = term.TerminalTool(workdir=".", timeout=60)
        err = "'ls' 不是内部或外部命令，也不是可运行的程序"
        r = __import__("asyncio").run(
            tool._posix_fallback("ls", err, str(Path.cwd()), dict(), 30))
        assert r is not None, "链式/单条 Unix 命令应自动换壳重跑"
        assert r.success, r.error
