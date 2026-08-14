"""内置插件必须真的被发现、被加载、被记住。

v1.6.0 随包分发了 4 个内置插件（cost_tracker / pii_guard / task_notify /
hello_hooks），文件与清单都齐全 —— 但 `PluginManager` 的默认 `plugin_dirs`
只有 `~/.automind/plugins`，内置目录**从未被扫描**，插件面板永远显示
"未发现插件"。这类缺陷不报错、不抛异常，只是功能静默不存在。

另外两处连带问题：
  · `status()` 曾用"plugin_dirs[0] 即内置目录"来判定 builtin，用户自定义
    plugin_dirs 时会把用户插件错标成内置；
  · 启用状态只活在内存里，一次改模型触发的 Agent 重建就悄悄失效。
"""

from __future__ import annotations

import json

import pytest

from automind.core.plugin import PluginManager, builtin_plugin_dir

BUILTIN = {"cost_tracker", "pii_guard", "task_notify", "hello_hooks"}


class TestDiscovery:
    def test_builtin_dir_ships_all_four_plugins(self):
        d = builtin_plugin_dir()
        assert d.is_dir(), f"内置插件目录不存在：{d}"
        found = {p.name for p in d.iterdir() if (p / "plugin.json").is_file()}
        assert found >= BUILTIN, f"缺少内置插件：{sorted(BUILTIN - found)}"

    def test_default_manager_discovers_builtins(self):
        """默认构造（不传 plugin_dirs）就必须能看见内置插件。"""
        names = {m.name for m in PluginManager().discover()}
        assert names >= BUILTIN, f"默认配置下发现不到：{sorted(BUILTIN - names)}"

    @pytest.mark.parametrize("name", sorted(BUILTIN))
    def test_manifest_is_wellformed(self, name):
        data = json.loads((builtin_plugin_dir() / name / "plugin.json").read_text("utf-8"))
        assert data["name"] == name
        assert data.get("description"), "插件面板要显示描述，不能为空"
        mod, _, attr = data.get("entry_point", "hooks:get_hooks").partition(":")
        assert (builtin_plugin_dir() / name / f"{mod}.py").is_file()
        assert attr

    @pytest.mark.parametrize("name", sorted(BUILTIN))
    def test_each_builtin_loads_and_hooks_something(self, name):
        pm = PluginManager()
        pm.discover()
        hooks = pm.load(name)
        assert hooks is not None, f"{name} 加载失败"
        attached = [k for k in vars(hooks) if getattr(hooks, k) is not None]
        assert attached, f"{name} 一个生命周期钩子都没挂上，等于没装"


class TestBuiltinFlag:
    def test_builtin_plugins_are_labelled_builtin(self):
        st = {p["name"]: p for p in PluginManager().status()}
        for n in BUILTIN:
            assert st[n]["builtin"] is True, f"{n} 应标记为内置"
            assert st[n]["version"], f"{n} 版本号不该为空"

    def test_user_plugin_is_not_labelled_builtin(self, tmp_path):
        """曾经的实现把 plugin_dirs[0] 当内置目录 —— 用户插件会被错标。"""
        d = tmp_path / "my-plugin"
        d.mkdir()
        (d / "plugin.json").write_text(
            json.dumps({"name": "my-plugin", "version": "0.1.0"}), encoding="utf-8")
        (d / "hooks.py").write_text(
            "from automind.core.hooks import AgentHooks\n"
            "def get_hooks():\n    return AgentHooks()\n", encoding="utf-8")

        pm = PluginManager(plugin_dirs=[tmp_path])
        st = {p["name"]: p for p in pm.status()}
        assert st["my-plugin"]["builtin"] is False, "用户目录里的插件不该显示为内置"

    def test_user_plugin_overrides_builtin_of_same_name(self, tmp_path):
        """同名时用户插件胜出 —— 想改写内置行为无需改源码。"""
        d = tmp_path / "cost_tracker"
        d.mkdir()
        (d / "plugin.json").write_text(
            json.dumps({"name": "cost_tracker", "version": "9.9.9"}), encoding="utf-8")
        (d / "hooks.py").write_text(
            "from automind.core.hooks import AgentHooks\n"
            "def get_hooks():\n    return AgentHooks()\n", encoding="utf-8")

        pm = PluginManager(plugin_dirs=[builtin_plugin_dir(), tmp_path])
        st = {p["name"]: p for p in pm.status()}
        assert st["cost_tracker"]["version"] == "9.9.9"
        assert st["cost_tracker"]["builtin"] is False


class _FakeAgent:
    """只带插件管理器的最小 Agent 替身（真 Agent 要建记忆库，太重）。"""

    def __init__(self, autoload_builtins: bool = False):
        self.plugin_manager = PluginManager()
        self.applied = False
        if autoload_builtins:
            # 复刻 AutoMindAgent.__init__ 的行为：内置插件默认全开
            for meta in self.plugin_manager.discover():
                if self.plugin_manager.is_builtin(meta):
                    self.plugin_manager.load(meta.name)

    def apply_plugin_hooks(self):
        self.applied = True


class TestEnabledStatePersists:
    @pytest.fixture(autouse=True)
    def _isolated_config(self, tmp_path):
        import automind.server as srv
        srv._store.config_file = tmp_path / "config.json"
        return srv

    def _write(self, srv, names):
        cfg = srv._read_config()
        cfg["enabled_plugins"] = names
        srv._write_config(cfg)

    def test_enabled_plugins_survive_agent_rebuild(self, _isolated_config):
        """改一次模型就触发 Agent 重建 —— 启用的插件不能因此静默失效。"""
        srv = _isolated_config
        self._write(srv, ["cost_tracker", "task_notify"])

        agent = _FakeAgent()
        srv._restore_plugins(agent)
        assert set(agent.plugin_manager.loaded_names()) == {"cost_tracker", "task_notify"}
        assert agent.applied, "恢复后必须把 hooks 应用到 Agent，否则只是加载了个寂寞"

    def test_disabled_builtin_stays_disabled_after_rebuild(self, _isolated_config):
        """内置插件默认自动加载 —— 用户关掉它，重建后不能又被装回来。

        只"补加载"不"补卸载"的实现里，这个开关是按不住的：点了关闭，
        下次改个模型就自己开回来了，而且没有任何提示。
        """
        srv = _isolated_config
        self._write(srv, ["cost_tracker"])          # 用户只留了这一个

        agent = _FakeAgent(autoload_builtins=True)  # 重建：内置全被自动装上
        assert "pii_guard" in agent.plugin_manager.loaded_names()

        srv._restore_plugins(agent)
        assert agent.plugin_manager.loaded_names() == ["cost_tracker"]
        assert agent.applied

    def test_untouched_config_keeps_builtins_on(self, _isolated_config):
        """用户从没动过开关时，不该把默认全开的内置插件全卸掉。"""
        srv = _isolated_config                      # 配置里没有 enabled_plugins 键
        agent = _FakeAgent(autoload_builtins=True)
        before = agent.plugin_manager.loaded_names()

        srv._restore_plugins(agent)
        assert agent.plugin_manager.loaded_names() == before
        assert before, "内置插件本应默认加载"

    def test_missing_plugin_does_not_break_startup(self, _isolated_config):
        """配置里记着一个已被删掉的插件，不能让整个服务起不来。"""
        srv = _isolated_config
        self._write(srv, ["cost_tracker", "已经被删掉的插件"])

        agent = _FakeAgent()
        srv._restore_plugins(agent)                 # 不抛异常
        assert agent.plugin_manager.loaded_names() == ["cost_tracker"]
