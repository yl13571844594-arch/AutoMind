"""路由清单快照 —— 拆分 server.py 的安全网。

`server.py` 是 3800+ 行的"上帝文件"，要拆成多个模块。拆分过程中最容易发生、
也最难发现的事故是**某个端点悄悄消失或改名**：应用照常启动、其余测试照常通过
（没人专门测那个端点），只有用户点到那个功能时才 404。

这份快照把"对外暴露了哪些端点"钉死。拆分时只要集合不变，就说明搬运没漏；
真要增删端点，必须显式重新生成快照 —— 那正是应该被 review 的时刻。
它同时兼作 API 契约文档：改路径 = 破坏用户已有的脚本与集成。

更新快照（**要连同 diff 一起 review**）::

    python tests/server/test_route_inventory.py --update
"""

from __future__ import annotations

from pathlib import Path

import automind.server as srv

#: 基线快照文件
SNAPSHOT = Path(__file__).with_name("routes_snapshot.txt")

#: FastAPI 自带、与业务无关的端点
_IGNORED = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

#: 与商业扩展（automind-pro）约定的上下文键。社区版重构时必须保持可用，
#: 否则装了扩展的用户升级社区版就会 attach 失败。
PRO_CONTEXT_KEYS = {
    "app", "get_agent", "read_config", "write_config", "push_history",
    "broadcast", "task_history", "token_totals", "interaction_to_execution",
    "session_agent_factory", "max_concurrent", "version",
    "register_token_validator", "rebuild_agent",
}


def _routes() -> set[str]:
    """当前注册的 "方法 路径" 集合；忽略 docs 与静态文件挂载。"""
    from starlette.routing import Mount

    out: set[str] = set()
    for r in srv.app.routes:
        if isinstance(r, Mount):          # StaticFiles 挂载不是 API 端点
            continue
        path = getattr(r, "path", None)
        if not path or path in _IGNORED:
            continue
        methods = getattr(r, "methods", None)
        if methods:
            out |= {f"{m} {path}" for m in methods if m not in ("HEAD", "OPTIONS")}
        else:
            out.add(f"WS {path}")         # WebSocket 路由没有 methods
    return out


def _snapshot() -> set[str]:
    return {
        ln.strip()
        for ln in SNAPSHOT.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }


class TestRouteInventory:
    def test_no_route_disappeared(self):
        """拆分/重构后端点不能少 —— 少一个就是一个功能静默 404。"""
        missing = _snapshot() - _routes()
        assert not missing, f"以下端点不见了（拆分时漏搬 / 改名？）：{sorted(missing)}"

    def test_new_routes_are_declared(self):
        """新增端点要显式登记，顺便逼一次"这个 API 真的要对外吗"的思考。"""
        extra = _routes() - _snapshot()
        assert not extra, (
            f"发现未登记的端点：{sorted(extra)}\n"
            "有意新增的话，用 --update 重新生成快照并连同 diff 一起 review。")

    def test_every_route_has_a_handler(self):
        """路由在但 endpoint 为空 = 注册被覆盖或搬运出错。"""
        for r in srv.app.routes:
            if getattr(r, "path", "").startswith(("/api", "/v1", "/ws")):
                assert getattr(r, "endpoint", None) is not None, f"{r.path} 没有处理函数"

    def test_snapshot_is_not_empty(self):
        """快照文件被清空 / 路径写错时，上面两条会双双"通过"。"""
        assert len(_snapshot()) > 80, "快照条目异常地少，八成是文件被清空了"


class TestProExtensionContract:
    """商业扩展靠这份 ctx 挂载；社区版重构时不能把键弄丢。"""

    def test_context_keys_are_stable(self):
        missing = PRO_CONTEXT_KEYS - set(srv._build_server_ctx())
        assert not missing, (
            f"扩展契约 v1 缺键：{sorted(missing)} —— "
            "装了 automind-pro 的用户升级后会 attach 失败")

    def test_context_values_are_usable(self):
        """键在但值是 None 同样会让扩展炸掉。"""
        ctx = srv._build_server_ctx()
        for k in sorted(PRO_CONTEXT_KEYS):
            assert ctx.get(k) is not None, f"契约键 {k} 的值为 None"


class TestPublicSymbols:
    """测试与外部代码直接引用的 server 符号，拆分后必须仍能从原路径取到。"""

    SYMBOLS = [
        "app", "__version__", "get_agent", "_rebuild_agent", "_acquire_run_agent",
        "_read_config", "_write_config", "_store", "_task_history", "_token_totals",
        "_push_history", "_save_history_notify", "_get_session_history",
        "_save_session_history", "_restore_plugins", "_probe_port",
        "_port_conflict_message", "_session_clones", "_fs_roots",
        "_fs_within_roots", "_fs_path_denied", "api_stats", "main",
    ]

    def test_all_present(self):
        missing = [s for s in self.SYMBOLS if not hasattr(srv, s)]
        assert not missing, (
            f"以下符号从 automind.server 上消失了：{missing}\n"
            "拆分后请在 server.py 里重新导出，否则测试与既有集成会 ImportError。")


if __name__ == "__main__":       # pragma: no cover - 维护脚本
    import sys

    if "--update" in sys.argv:
        routes = sorted(_routes())
        header = (
            "# AutoMind 对外端点快照 —— 由 test_route_inventory.py --update 生成。\n"
            "# 改动此文件即改动对外 API 契约，请连同 diff 一起 review。\n"
        )
        SNAPSHOT.write_text(header + "\n".join(routes) + "\n", encoding="utf-8")
        print(f"已写入 {len(routes)} 个端点 → {SNAPSHOT}")
    else:
        print(f"当前注册 {len(_routes())} 个端点；加 --update 可刷新快照")
