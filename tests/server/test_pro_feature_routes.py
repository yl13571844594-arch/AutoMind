"""Pro / 企业版特性路由挂载清单的守卫（社区版即可运行，不依赖 automind_pro）。

背景（v1.11.17 修复）：这些特性自带 REST 路由，实现由 automind-pro 在运行时通过
``edition`` 协议注入。但社区核心 ``automind.server._ATTACHABLE_FEATURES`` 曾是**第二份
手工维护**的清单，漏掉了 semantic_cache / model_router / cost_dashboard / rag_pro /
kb_enterprise。于是 Pro 许可证下 ``edition.get_feature("semantic_cache")`` 明明有实现，
``attach()`` 却从未执行，真实路由没注册，``/api/cache``、``/api/router`` 只命中 403
降级路由 —— 用户看到"该功能未开通或被版本门控"，而 ``/api/status`` 显示已授权。

本文件的断言故意**不需要商业包 / 许可证**（社区版 CI 里也跑）：
  1. 挂载清单必须与 ``edition.COMMERCIAL_FEATURES`` 同源同序 —— 以后新增带路由的
     特性自动覆盖，不再依赖"记得改两处"（漏改正是本 bug 的根因）；
  2. 已知五个"自带路由"的特性必须在清单里；
  3. 社区版行为不变：这些路径的 403 降级路由仍然注册，没有扩展包时由它给出升级提示；
  4. 前端容错文案按"是否已授权"分两种语境（没有 JS 测试栈，沿用本仓库
     test_v11110_cache_panel.py 的源码结构断言方式）。
"""

from __future__ import annotations

from pathlib import Path

import automind.server as srv
from automind.core import edition

#: 曾因手工清单漏项而 403 的特性 → 它的一条对外路由（用于确认社区版降级路由还在）
ROUTE_FEATURES: dict[str, str] = {
    "semantic_cache": "/api/cache",
    "model_router": "/api/router",
    "cost_dashboard": "/api/costs",
    "rag_pro": "/api/kb/kbs",
    "kb_enterprise": "/api/kb/stats",
}

#: 前端「智能路由与成本」视图：门控文案的落点
ROUTER_VIEW = (Path(__file__).resolve().parents[2]
               / "web" / "src" / "components" / "views" / "RouterView.tsx")


def _route_paths() -> set[str]:
    return {getattr(r, "path", "") for r in srv.app.routes}


class TestAttachListCoverage:
    """挂载清单的覆盖度不再靠人工记忆。"""

    def test_derived_from_commercial_features(self):
        """清单必须直接派生自 COMMERCIAL_FEATURES：同源、同序、一个不落。"""
        expected = tuple(edition.COMMERCIAL_FEATURES)
        actual = tuple(srv._ATTACHABLE_FEATURES)
        assert actual == expected, (
            "挂载清单与 COMMERCIAL_FEATURES 不一致：请保持 tuple(COMMERCIAL_FEATURES) "
            "派生，手工维护的第二份清单一定会漏（v1.11.16 就漏了 5 个带路由的特性）。")

    def test_route_bearing_features_are_listed(self):
        """5 个自带 REST 路由的特性一个都不能少（漏一个 = 该功能买后仍 403）。"""
        missing = [name for name in ROUTE_FEATURES if name not in srv._ATTACHABLE_FEATURES]
        assert not missing, f"自带 REST 路由的特性未被挂载：{missing}"

    def test_no_unknown_feature(self):
        """清单里的键都必须是登记过的商业特性，否则会被静默忽略。"""
        unknown = [name for name in srv._ATTACHABLE_FEATURES
                   if name not in edition.COMMERCIAL_FEATURES]
        assert not unknown, f"挂载清单里有未登记的商业特性：{unknown}"


class TestCommunityFallbackRoutes:
    """没有扩展包 / 许可证时，这些路径仍有 403 降级路由 —— 社区版升级引导不变。"""

    def test_locked_routes_registered(self):
        paths = _route_paths()
        missing = [path for path in ROUTE_FEATURES.values() if path not in paths]
        assert not missing, f"社区版 403 降级路由不见了：{missing}"

    def test_locked_features_are_attachable(self):
        # 降级路由对应的功能键必须都在挂载清单里：否则装上 Pro 后 attach 永不执行，
        # 403 就变成永久行为（正是本 bug 的形态）。
        missing = set(ROUTE_FEATURES) - set(srv._ATTACHABLE_FEATURES)
        assert not missing, f"有降级路由的功能不在挂载清单里：{sorted(missing)}"


class TestRouterViewGuardHint:
    """guardPayload：已授权却收到门控体时，提示要指向服务端未挂载，而不是"未开通"。"""

    @staticmethod
    def _src() -> str:
        return ROUTER_VIEW.read_text(encoding="utf-8")

    def test_distinguishes_licensed_from_unlicensed(self):
        src = self._src()
        # "已授权"分支必须以 /api/status 的特性表为准（否则会把服务端故障说成没买）
        assert "featureOn(feature)" in src
        # 已授权 → 服务端未挂载 / 重启 / 换扩展包
        assert "服务端未挂载该特性" in src
        assert "重启服务" in src and "同版本的扩展包" in src
        # 未授权 → 保留原来的"未开通或版本门控"提示
        assert "该功能未开通或被版本门控" in src

    def test_still_surfaces_errors_for_retry(self):
        # 门控/错误都抛 Error；AsyncBoundary 据此渲染错误面板 + 重试，不会白屏
        src = self._src()
        assert "throw new Error(" in src
        assert "AsyncBoundary state={toBoundaryState(" in src
