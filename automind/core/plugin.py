"""插件系统（§14.7）— 在 AgentHooks 之上提供第三方插件的发现、加载与卸载。

插件目录结构::

    ~/.automind/plugins/
    ├── my-plugin/
    │   ├── plugin.json      # 元信息 { name, version, description, entry_point }
    │   └── hooks.py         # 提供 get_hooks() -> AgentHooks，或 AgentHooks 实例/子类

`entry_point` 形如 "hooks:get_hooks"（模块名:属性名），默认即此值。
被引用的属性可以是：
    - 返回 AgentHooks 的函数（可无参）；
    - AgentHooks 实例；
    - AgentHooks 子类（将被实例化）。

安全说明：加载插件会执行其 Python 代码，仅应加载可信来源的插件。
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

from automind.core.hooks import AgentHooks, merge_hooks


@dataclass
class PluginMeta:
    """插件元信息。"""

    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    entry_point: str = "hooks:get_hooks"
    path: str = ""  # 插件目录绝对路径


class PluginManager:
    """插件管理器 — 扫描、加载、卸载插件并汇总其 hooks。"""

    def __init__(self, plugin_dirs: list[Path] | None = None) -> None:
        self.plugin_dirs: list[Path] = plugin_dirs or [
            Path("~/.automind/plugins").expanduser()
        ]
        self._loaded: dict[str, AgentHooks] = {}
        self._meta: dict[str, PluginMeta] = {}

    # ── 发现 ──────────────────────────────────────────────

    def discover(self) -> list[PluginMeta]:
        """扫描插件目录，返回所有可用插件的元信息。"""
        found: list[PluginMeta] = []
        for base in self.plugin_dirs:
            if not base.exists():
                continue
            for entry in sorted(base.iterdir()):
                if not entry.is_dir():
                    continue
                manifest = entry / "plugin.json"
                if not manifest.exists():
                    continue
                meta = self._read_manifest(manifest, entry)
                if meta is not None:
                    found.append(meta)
        # 刷新元信息缓存
        self._meta = {m.name: m for m in found}
        return found

    @staticmethod
    def _read_manifest(manifest: Path, entry: Path) -> PluginMeta | None:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        name = data.get("name") or entry.name
        return PluginMeta(
            name=name,
            version=str(data.get("version", "0.0.0")),
            description=data.get("description", ""),
            author=data.get("author", ""),
            entry_point=data.get("entry_point", "hooks:get_hooks"),
            path=str(entry.resolve()),
        )

    # ── 加载 / 卸载 ───────────────────────────────────────

    def load(self, name: str) -> AgentHooks | None:
        """加载指定插件并返回其 hooks；失败返回 None。"""
        if name in self._loaded:
            return self._loaded[name]
        meta = self._meta.get(name)
        if meta is None:
            # 允许先 load 未 discover 的插件：即时扫描一次
            self.discover()
            meta = self._meta.get(name)
        if meta is None:
            return None

        hooks = self._load_from_meta(meta)
        if hooks is not None:
            self._loaded[name] = hooks
        return hooks

    @staticmethod
    def _load_from_meta(meta: PluginMeta) -> AgentHooks | None:
        mod_name, _, attr = meta.entry_point.partition(":")
        attr = attr or "get_hooks"
        module_file = Path(meta.path) / f"{mod_name}.py"
        if not module_file.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location(
                f"automind_plugin_{meta.name}", module_file
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            target = getattr(module, attr, None)
            if target is None:
                return None
            return PluginManager._resolve_hooks(target)
        except Exception:
            return None

    @staticmethod
    def _resolve_hooks(target: object) -> AgentHooks | None:
        """把入口属性解析为 AgentHooks 实例。"""
        if isinstance(target, AgentHooks):
            return target
        if isinstance(target, type) and issubclass(target, AgentHooks):
            return target()
        if callable(target):
            result = target()
            if isinstance(result, AgentHooks):
                return result
        return None

    def unload(self, name: str) -> bool:
        """卸载插件；返回是否确实卸载了。"""
        return self._loaded.pop(name, None) is not None

    # ── 汇总 ──────────────────────────────────────────────

    def assemble_hooks(self) -> AgentHooks:
        """合并所有已加载插件的 hooks 为一份。"""
        return merge_hooks(list(self._loaded.values()))

    def loaded_names(self) -> list[str]:
        return sorted(self._loaded.keys())

    def status(self) -> list[dict]:
        """列出已发现插件及其加载状态（供 Web/CLI 展示）。"""
        # 确保元信息最新
        discovered = {m.name: m for m in self.discover()}
        # 已加载但目录已删的插件也一并展示
        names = set(discovered) | set(self._loaded)
        out = []
        for n in sorted(names):
            m = discovered.get(n) or self._meta.get(n)
            out.append({
                "name": n,
                "version": m.version if m else "",
                "description": m.description if m else "",
                "author": m.author if m else "",
                "loaded": n in self._loaded,
            })
        return out
