"""插件系统 + 生命周期钩子测试（§3.5 / §14.7）。"""

import asyncio
import json
import textwrap
from pathlib import Path

from automind.core.hooks import AgentHooks, invoke_hook, merge_hooks
from automind.core.plugin import PluginManager


def _make_plugin(base: Path, name: str, body: str, manifest: dict | None = None) -> None:
    pdir = base / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        json.dumps(manifest or {"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    (pdir / "hooks.py").write_text(textwrap.dedent(body), encoding="utf-8")


class TestHooks:
    def test_invoke_none_is_noop(self):
        asyncio.run(invoke_hook(None, "x"))  # 不应抛出

    def test_invoke_sync_and_async(self):
        seen = []

        async def a(x):
            seen.append(("a", x))

        def b(x):
            seen.append(("b", x))

        asyncio.run(invoke_hook(a, 1))
        asyncio.run(invoke_hook(b, 2))
        assert seen == [("a", 1), ("b", 2)]

    def test_exception_swallowed(self):
        async def boom(x):
            raise ValueError("nope")

        asyncio.run(invoke_hook(boom, 1))  # 不应抛出

    def test_merge_calls_all_in_order(self):
        calls = []

        async def h1(x):
            calls.append(1)

        async def h2(x):
            raise RuntimeError("mid fails")

        async def h3(x):
            calls.append(3)

        merged = merge_hooks([
            AgentHooks(before_run=h1), AgentHooks(before_run=h2), AgentHooks(before_run=h3),
        ])
        asyncio.run(invoke_hook(merged.before_run, "u"))
        assert calls == [1, 3]  # h2 抛错被吞，其余仍执行

    def test_merge_empty(self):
        merged = merge_hooks([AgentHooks(), AgentHooks()])
        assert merged.before_run is None


class TestPluginManager:
    def test_discover_and_load(self, tmp_path):
        _make_plugin(tmp_path, "greeter", """
            from automind.core.hooks import AgentHooks
            def get_hooks():
                async def before(u):
                    pass
                return AgentHooks(before_run=before)
        """)
        pm = PluginManager(plugin_dirs=[tmp_path])
        metas = pm.discover()
        assert [m.name for m in metas] == ["greeter"]
        assert metas[0].version == "1.0.0"

        hooks = pm.load("greeter")
        assert isinstance(hooks, AgentHooks)
        assert hooks.before_run is not None
        assert pm.loaded_names() == ["greeter"]

    def test_assemble_and_effect(self, tmp_path):
        sink = tmp_path / "sink.txt"
        _make_plugin(tmp_path, "logger", f"""
            from automind.core.hooks import AgentHooks
            SINK = {str(sink)!r}
            def get_hooks():
                async def before(u):
                    with open(SINK, 'a', encoding='utf-8') as f:
                        f.write('before:' + u + chr(10))
                return AgentHooks(before_run=before)
        """)
        pm = PluginManager(plugin_dirs=[tmp_path])
        pm.discover()
        pm.load("logger")
        merged = pm.assemble_hooks()
        asyncio.run(invoke_hook(merged.before_run, "hi"))
        assert sink.read_text(encoding="utf-8").strip() == "before:hi"

    def test_class_entry_point(self, tmp_path):
        _make_plugin(tmp_path, "cls", """
            from automind.core.hooks import AgentHooks
            class Hooks(AgentHooks):
                pass
        """, manifest={"name": "cls", "version": "0.1", "entry_point": "hooks:Hooks"})
        pm = PluginManager(plugin_dirs=[tmp_path])
        pm.discover()
        assert isinstance(pm.load("cls"), AgentHooks)

    def test_unload_and_status(self, tmp_path):
        _make_plugin(tmp_path, "p1", """
            from automind.core.hooks import AgentHooks
            def get_hooks():
                return AgentHooks()
        """)
        pm = PluginManager(plugin_dirs=[tmp_path])
        pm.load("p1")
        status = pm.status()
        assert any(s["name"] == "p1" and s["loaded"] for s in status)
        assert pm.unload("p1") is True
        assert pm.unload("p1") is False
        assert pm.loaded_names() == []

    def test_missing_plugin_returns_none(self, tmp_path):
        pm = PluginManager(plugin_dirs=[tmp_path])
        assert pm.load("nonexistent") is None

    def test_broken_manifest_skipped(self, tmp_path):
        pdir = tmp_path / "broken"
        pdir.mkdir()
        (pdir / "plugin.json").write_text("{ not valid json", encoding="utf-8")
        pm = PluginManager(plugin_dirs=[tmp_path])
        assert pm.discover() == []


class TestAgentIntegration:
    def test_agent_has_hooks_and_plugin_manager(self):
        # 仅验证属性与 API 存在，不需要 LLM
        from automind.agent import AutoMindAgent
        agent = AutoMindAgent.__new__(AutoMindAgent)
        agent.hooks = AgentHooks()
        agent.plugin_manager = PluginManager(plugin_dirs=[])
        # apply_plugin_hooks 应把已加载插件汇总为 hooks（此处为空）
        AutoMindAgent.apply_plugin_hooks(agent)
        assert isinstance(agent.hooks, AgentHooks)
