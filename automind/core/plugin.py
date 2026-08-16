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
from automind.core.logging import get_logger

logger = get_logger("automind.plugin")


@dataclass
class PluginMeta:
    """插件元信息。"""

    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    entry_point: str = "hooks:get_hooks"
    path: str = ""  # 插件目录绝对路径


def builtin_plugin_dir() -> Path:
    """随包分发的内置插件目录（automind/builtin_plugins）。"""
    return Path(__file__).resolve().parent.parent / "builtin_plugins"


def user_plugin_dir() -> Path:
    """用户自建插件目录。"""
    return Path("~/.automind/plugins").expanduser()


class PluginManager:
    """插件管理器 — 扫描、加载、卸载插件并汇总其 hooks。"""

    def __init__(self, plugin_dirs: list[Path] | None = None) -> None:
        # 内置目录在前、用户目录在后：同名时用户插件覆盖内置（_meta 后写胜出），
        # 这样用户想改写内置插件的行为，放一个同名目录即可，无需改源码。
        #
        # 此前默认只有用户目录 —— 随包分发的 4 个内置插件（cost_tracker /
        # pii_guard / task_notify / hello_hooks）文件在、清单在，却**从未被
        # 扫描到**，插件面板永远显示"未发现插件"。
        self.plugin_dirs: list[Path] = plugin_dirs or [
            builtin_plugin_dir(), user_plugin_dir(),
        ]
        self._builtin_dir: Path = builtin_plugin_dir()
        self._loaded: dict[str, AgentHooks] = {}
        self._meta: dict[str, PluginMeta] = {}

    def is_builtin(self, meta: PluginMeta | None) -> bool:
        """该插件是否来自随包分发的内置目录。"""
        if meta is None or not meta.path:
            return False
        try:
            return self._builtin_dir.resolve() in Path(meta.path).resolve().parents
        except OSError:
            return False

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
        """按清单加载插件；失败返回 None 并**说明原因**。

        此前这里是一个光秃秃的 `except Exception: return None`，任何失败都退化成
        "插件加载失败或不存在"这一句既不区分原因、也无处排查的提示。桌面版打包
        漏带 hooks.py 时（清单在、代码不在），界面上 4 个内置插件全都显示正常
        却一个也用不了，而日志里一个字都没有 —— 排查全靠猜。
        """
        mod_name, _, attr = meta.entry_point.partition(":")
        attr = attr or "get_hooks"
        module_file = Path(meta.path) / f"{mod_name}.py"
        if not module_file.exists():
            logger.error("plugin_entry_missing", plugin=meta.name,
                         expected=str(module_file),
                         hint="清单存在但入口代码缺失；打包时可能漏带了 .py 文件")
            return None
        try:
            spec = importlib.util.spec_from_file_location(
                f"automind_plugin_{meta.name}", module_file
            )
            if spec is None or spec.loader is None:
                logger.error("plugin_spec_failed", plugin=meta.name,
                             file=str(module_file))
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            target = getattr(module, attr, None)
            if target is None:
                logger.error("plugin_entry_point_not_found", plugin=meta.name,
                             attr=attr, file=str(module_file))
                return None
            return PluginManager._resolve_hooks(target)
        except Exception as e:
            logger.error("plugin_load_failed", plugin=meta.name,
                         file=str(module_file),
                         error=f"{type(e).__name__}: {e}")
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
                # 按插件**实际所在目录**判定，而不是"plugin_dirs[0] 即内置"——
                # 后者在用户自定义 plugin_dirs 时会把用户插件错标成内置
                "builtin": self.is_builtin(m),
                "loaded": n in self._loaded,
            })
        return out
