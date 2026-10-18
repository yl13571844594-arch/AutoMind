"""插件系统（§14.7）— 在 AgentHooks 之上提供第三方插件的发现、加载与卸载。

插件目录结构::

    ~/.automind/plugins/
    ├── my-plugin/
    │   ├── plugin.json      # 元信息 { name, version, description, entry_point, priority? }
    │   └── hooks.py         # 提供 get_hooks() -> AgentHooks，或 AgentHooks 实例/子类

`entry_point` 形如 "hooks:get_hooks"（模块名:属性名），默认即此值。
`priority`（可选，默认 100）：提供分派中间件的插件在链中的位置，数字越小越
靠外层 —— 见 :meth:`PluginManager.assemble_middlewares`。
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
from typing import Any

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
    # v1.11.9：中间件在分派链中的位置。**数字越小越靠外层**（越先包住工具）。
    # 不写默认 100，同优先级按插件名字典序稳定排序。为什么需要它：组装顺序
    # 此前纯按插件名字典序 —— 插件一多，"谁包住谁"完全由名字的字母顺序决定，
    # 交付链（delivery_manifest 外层记录 → artifact_selfcheck 校验 →
    # fs_lock_guard 重试）的正确嵌套关系全靠巧合。
    priority: int = 100


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
    def _read_priority(data: dict[str, Any]) -> int:
        """读清单里的 priority；非法值（缺省/布尔/非整数/越界）一律回退默认 100。

        允许范围 [-1000, 1000]：这个量级足以表达任何合理的分层，
        又不会让一个手滑写出的巨大数字把插件顶到链的最外层。
        注意 bool 是 int 的子类（``int(True) == 1``），要先于 int() 拦掉。
        """
        raw = data.get("priority")
        if raw is None or isinstance(raw, bool):
            return 100
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 100
        return value if -1000 <= value <= 1000 else 100

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
            priority=PluginManager._read_priority(data),
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
            hooks = PluginManager._resolve_hooks(target)
            if hooks is not None:
                PluginManager._attach_extensions(hooks, module, meta.name)
            return hooks
        except Exception as e:
            logger.error("plugin_load_failed", plugin=meta.name,
                         file=str(module_file),
                         error=f"{type(e).__name__}: {e}")
            return None

    @staticmethod
    def _attach_extensions(hooks: AgentHooks, module: Any, name: str) -> None:
        """插件除钩子外还可以提供（v1.11.6）：

        * ``get_tools() -> list[AbstractTool]``：新增工具（不能顶替同名内置工具）；
        * ``get_middleware() -> async mw(tool_name, kwargs, call_next)``：包住工具分派，
          可以重试、改写参数、补充结果说明（见 ``builtin_plugins/net_resilience``）。
        """
        tools: list[Any] = []
        middleware = None
        try:
            if callable(getattr(module, "get_tools", None)):
                tools = list(module.get_tools() or [])
            if callable(getattr(module, "get_middleware", None)):
                middleware = module.get_middleware()
        except Exception as e:
            logger.error("plugin_extensions_failed", plugin=name, error=f"{type(e).__name__}: {e}")
        hooks.plugin_tools = tools               # type: ignore[attr-defined]
        hooks.plugin_middleware = middleware     # type: ignore[attr-defined]

    def assemble_tools(self) -> list[Any]:
        out: list[Any] = []
        for n in sorted(self._loaded):
            out.extend(getattr(self._loaded[n], "plugin_tools", None) or [])
        return out

    def assemble_middlewares(self) -> list[Any]:
        """汇总已加载插件的分派中间件，按 (priority, 插件名) 稳定排序。

        列表顺序 = 包裹顺序：**排在前面的在外层**（先收到调用、最后拿到结果）。
        内置交付链的既定顺序（v1.11.9）：delivery_manifest(10) 记录最终落盘
        → artifact_selfcheck(20) 回读校验 → fs_lock_guard(30) 占用重试
        → net_resilience(40) 网络重试。外层看到的都是内层处理完的**最终**结果。
        """
        items = [
            (self._meta[n].priority if n in self._meta else 100, n,
             getattr(self._loaded[n], "plugin_middleware", None))
            for n in self._loaded
        ]
        return [m for _, _, m in sorted(items, key=lambda t: (t[0], t[1]))
                if m is not None]

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
        """合并所有已加载插件的 hooks 为一份（按 priority → 名称的顺序合并）。"""
        # 与 assemble_middlewares 同序：priority 小的先注册 —— 对 after_run 这类
        # 钩子意味着先执行。交付链上 delivery_manifest(10) 的清单追加因此稳定
        # 先于 task_notify(100) 的通知发出，顺序不再取决于目录的字母序。
        order = sorted(
            self._loaded,
            key=lambda n: (self._meta[n].priority if n in self._meta else 100, n),
        )
        return merge_hooks([self._loaded[n] for n in order])

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
