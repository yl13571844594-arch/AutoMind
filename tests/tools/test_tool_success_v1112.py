"""v1.11.2 工具调用成功率：命名空间工具名、技能改道、技能输入整形、MCP 结果与断线处理。"""

from __future__ import annotations

import sys
import textwrap
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from automind.agent import normalize_skill_input
from automind.core.types import ToolResult
from automind.tools.base import AbstractTool, ToolRegistry, resolve_name
from automind.tools.mcp_registry import _strip_nulls, normalize_result

NAMES = ["mcp__github__create_issue", "mcp__github__list_repos", "mcp__gitlab__create_issue",
         "mcp__fs__read_file", "file_read", "file_search", "web_search", "plugin_weather"]


# ── 工具名解析 ────────────────────────────────────────────


@pytest.mark.parametrize("asked,real", [
    ("github.create_issue", "mcp__github__create_issue"),
    ("github__create_issue", "mcp__github__create_issue"),
    ("github_create_issue", "mcp__github__create_issue"),
    ("GitHub/list_repos", "mcp__github__list_repos"),
    ("list_repos", "mcp__github__list_repos"),        # 本名唯一
    ("weather", "plugin_weather"),                     # 插件前缀
    ("read_file", "file_read"),                        # 词序（内置优先于 MCP 本名）
])
def test_namespaced_names_resolve(asked, real):
    assert resolve_name(asked, NAMES) == (real, [])


@pytest.mark.parametrize("asked", ["create_issue", "read", "search"])
def test_ambiguous_or_guessy_names_do_not_resolve(asked):
    real, tips = resolve_name(asked, NAMES)
    assert real is None and tips, "有歧义 / 只是猜测时不能替模型做主"


# ── 未知工具名改道 ────────────────────────────────────────


class _Echo(AbstractTool):
    name = "skill_invoke"
    description = "echo"
    parameters = {"type": "object", "properties": {"name": {"type": "string"},
                                                   "input": {}}, "required": ["name"]}

    async def execute(self, **kw):
        return ToolResult(tool_name=self.name, success=True, output=kw)


async def test_registry_fallback_reroutes_unknown_names():
    reg = ToolRegistry()
    reg.register(_Echo())
    reg.add_fallback(lambda name, kw: ("skill_invoke", {"name": "pdf_summary", **kw})
                     if name == "pdf_summary" else None)
    r = await reg.dispatch("pdf_summary", path="a.pdf")
    assert r.success and r.output == {"name": "pdf_summary", "path": "a.pdf"}
    miss = await reg.dispatch("totally_unknown")
    assert not miss.success and miss.metadata.get("tool_not_found")


async def test_registry_fallback_errors_are_contained():
    reg = ToolRegistry()

    def boom(name, kw):
        raise RuntimeError("x")

    reg.add_fallback(boom)
    r = await reg.dispatch("nope")
    assert not r.success


def test_agent_routes_skill_name_to_skill_invoke():
    from automind.agent import AutoMindAgent
    fake = SimpleNamespace(skill_registry=SimpleNamespace(list_names=lambda: ["pdf_summary", "web_research"]))
    route = AutoMindAgent._route_skill_as_tool
    assert route(fake, "PdfSummary", {"input": "a.pdf"}) == (
        "skill_invoke", {"name": "pdf_summary", "input": "a.pdf"})
    assert route(fake, "skill_web_research", {"q": 1}) == (
        "skill_invoke", {"name": "web_research", "q": 1})
    assert route(fake, "file_read", {}) is None


# ── 技能输入整形 ──────────────────────────────────────────


class _In(BaseModel):
    topic: str
    words: int = 300


class _Skill:
    input_schema = _In


def test_plain_text_fills_the_required_string_field():
    assert normalize_skill_input(_Skill(), "写 AI 周报") == ({"topic": "写 AI 周报"}, "")


def test_json_string_is_parsed_and_flat_params_merged():
    data, err = normalize_skill_input(_Skill(), '{"topic": "x"}', {"words": 50, "junk": 1})
    assert err == "" and data == {"words": 50, "topic": "x"}


def test_flat_params_alone_work():
    assert normalize_skill_input(_Skill(), None, {"topic": "t"}) == ({"topic": "t"}, "")


def test_validation_error_lists_fields():
    data, err = normalize_skill_input(_Skill(), {"words": "many"})
    assert data is None and "topic" in err and "必填" in err


def test_markdown_skill_passes_through():
    md = SimpleNamespace(input_schema=None)
    assert normalize_skill_input(md, "随便") == ("随便", "")


# ── MCP 结果整理 ──────────────────────────────────────────


def _txt(t):
    return SimpleNamespace(type="text", text=t)


def test_mcp_error_result_is_failure():
    ok, content = normalize_result(SimpleNamespace(is_error=True, content=[_txt("bad arg")],
                                                   structured_content=None))
    assert ok is False and content == "bad arg"


def test_mcp_camelcase_v1_fields():
    ok, content = normalize_result({"isError": False, "content": [{"type": "text", "text": "hi"}]})
    assert ok and content == "hi"


def test_mcp_structured_duplicate_collapses():
    ok, content = normalize_result(SimpleNamespace(is_error=False, content=[_txt("5")],
                                                   structured_content={"result": "5"}))
    assert ok and content == "5"


def test_mcp_image_is_summarised():
    ok, content = normalize_result(SimpleNamespace(
        is_error=False, structured_content=None,
        content=[SimpleNamespace(type="image", text=None, mime_type="image/png", data="AAAA")]))
    assert ok and "图片" in content and "AAAA" not in content


def test_strip_nulls_respects_nullable():
    schema = {"properties": {"a": {"type": "string"}, "b": {"type": ["string", "null"]}}}
    assert _strip_nulls({"a": None, "b": None, "c": 1}, schema) == {"b": None, "c": 1}


# ── 真实 MCP 服务器（需要 mcp SDK）─────────────────────────


SERVER = textwrap.dedent('''
    import os
    try:
        from mcp.server.mcpserver import MCPServer as _S
    except ImportError:
        from mcp.server.fastmcp import FastMCP as _S
    app = _S("demo")

    @app.tool()
    def add(a: int, b: int, note: str | None = None) -> str:
        """Add two integers."""
        return f"{a + b}"

    @app.tool()
    def fail(reason: str) -> str:
        """Always fails."""
        raise ValueError(reason)

    @app.tool()
    def secret() -> str:
        """Env secret."""
        return os.environ.get("DEMO_SECRET", "<missing>")

    if __name__ == "__main__":
        app.run()
''')


async def test_real_mcp_server_roundtrip(tmp_path):
    pytest.importorskip("mcp")
    from automind.tools.mcp_registry import MCPRegistry, MCPServerConfig
    script = tmp_path / "srv.py"
    script.write_text(SERVER, encoding="utf-8")
    reg = MCPRegistry()
    reg.add_server(MCPServerConfig(name="demo", command=sys.executable, args=[str(script)],
                                   env={"DEMO_SECRET": "s3"}))
    reg.add_server(MCPServerConfig(name="bad", command="definitely-not-a-command-xyz"))
    try:
        res = await reg.connect_all()
        if not res["demo"]:
            pytest.skip(f"本机起不来 MCP 测试服务器：{reg.last_error('demo')}")
        assert res["bad"] is False and "找不到启动命令" in reg.last_error("bad")
        tr = ToolRegistry()
        reg.register_to(tr)
        r = await tr.dispatch("demo.add", a="2", b=3, note=None)
        assert r.success and r.output == "5"
        r = await tr.dispatch("mcp__demo__fail", reason="x")
        assert not r.success, "MCP 的 isError 结果必须判为失败"
        r = await tr.dispatch("demo_secret")
        assert r.success and r.output == "s3", "配置里的 env 要传给服务器进程"
    finally:
        await reg.disconnect_all()
