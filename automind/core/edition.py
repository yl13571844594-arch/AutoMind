"""版本（Edition）机制 — 社区版 / 专业版 / 企业版特性门控与商业扩展加载。

开源 / 商业分离的核心约定：
    - 社区核心（本仓库开源部分）不包含任何商业功能实现；
    - 商业能力由独立分发的 ``automind_pro`` 包在运行时通过本模块
      的**稳定扩展接口**注入（未安装 / 未授权时自动降级为社区版）；
    - 本模块的公开接口以 ``EXTENSION_API_VERSION`` 标注版本，
      同一大版本内保持向后兼容 —— 社区版持续迭代不会破坏已发布的
      专业版扩展；若未来必须做不兼容变更，将提升该版本号并在
      ``load_extensions`` 中做兼容协商。

扩展协议 v1（automind_pro 需实现）::

    # automind_pro/__init__.py
    EXTENSION_API_VERSION = 1

    def activate(api: "ExtensionAPI") -> str | None:
        '''校验许可证并注册特性。

        返回激活后的版本名（"pro" / "enterprise"），未授权返回 None。
        '''

特性注册表（键名与实现契约均属稳定接口）：
    ============= ======== ==============================================
    特性键         最低版本  实现契约
    ============= ======== ==============================================
    multi_agent    pro      ``create(llm) -> orchestrator``；orchestrator
                            提供 ``await run(task, context, on_event) -> dict``
    loop_engine    pro      ``await run(agent, task, on_event, max_iterations) -> dict``
    scheduler      pro      ``attach(server_ctx)`` 注册路由；``start()`` /
                            ``stop()`` / ``count() -> int``
    advanced_stats pro      ``attach(server_ctx)`` 注册路由
    session_pool   ent      ``attach(server_ctx)``；``enabled() -> bool``、
                            ``acquire(sid)``、``release(sid)``、``aclose_all()``
    ============= ======== ==============================================

``server_ctx``（服务端传给 attach 的上下文字典，稳定契约 v1）：
    app / get_agent / read_config / write_config / push_history /
    broadcast / task_history() / token_totals() / interaction_to_execution
"""

from __future__ import annotations

from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.edition")

EXTENSION_API_VERSION = 1

EDITION_COMMUNITY = "community"
EDITION_PRO = "pro"
EDITION_ENTERPRISE = "enterprise"

#: 全部商业特性清单：特性键 -> (最低版本, 中文名)
COMMERCIAL_FEATURES: dict[str, tuple[str, str]] = {
    "multi_agent": (EDITION_PRO, "协同模式（多智能体编排）"),
    "loop_engine": (EDITION_PRO, "循环模式（Loop Engineering）"),
    "scheduler": (EDITION_PRO, "定时任务"),
    "advanced_stats": (EDITION_PRO, "高级统计仪表盘"),
    "session_pool": (EDITION_ENTERPRISE, "多用户会话池（执行态隔离）"),
}

_EDITION_LABELS = {
    EDITION_COMMUNITY: "社区版",
    EDITION_PRO: "专业版",
    EDITION_ENTERPRISE: "企业版",
}

_state: dict[str, Any] = {
    "edition": EDITION_COMMUNITY,
    "features": {},       # feature name -> implementation object
    "loaded": False,      # load_extensions 是否已执行
    "license": None,      # 扩展包写入的许可证摘要（customer/expiry 等，可选）
}


class FeatureNotAvailable(RuntimeError):
    """请求的功能不在当前版本授权范围内。"""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(upgrade_hint(feature))


def upgrade_hint(feature: str) -> str:
    """生成面向用户的升级提示文案。"""
    tier, label = COMMERCIAL_FEATURES.get(feature, (EDITION_PRO, feature))
    tier_label = _EDITION_LABELS.get(tier, tier)
    return (
        f"「{label}」是 AutoMind {tier_label}功能，当前为"
        f"{_EDITION_LABELS[get_edition()]}。"
        f"请安装 automind-pro 并配置有效许可证（环境变量 AUTOMIND_LICENSE "
        f"或项目根目录 .automind_license 文件）后重启服务。"
    )


# ── 状态查询 ──────────────────────────────────────────────


def get_edition() -> str:
    """当前生效的版本名：community / pro / enterprise。"""
    load_extensions()
    return _state["edition"]


def edition_label() -> str:
    return _EDITION_LABELS.get(get_edition(), get_edition())


def has_feature(name: str) -> bool:
    load_extensions()
    return name in _state["features"]


def get_feature(name: str) -> Any | None:
    load_extensions()
    return _state["features"].get(name)


def require_feature(name: str) -> Any:
    """取用特性实现；未授权时抛出 :class:`FeatureNotAvailable`。"""
    impl = get_feature(name)
    if impl is None:
        raise FeatureNotAvailable(name)
    return impl


def feature_flags() -> dict[str, bool]:
    """全部已知商业特性的可用性开关（供 /api/status 与前端渲染）。"""
    load_extensions()
    return {name: name in _state["features"] for name in COMMERCIAL_FEATURES}


def license_info() -> dict | None:
    """扩展包激活时登记的许可证摘要（社区版为 None）。"""
    load_extensions()
    return _state["license"]


# ── 扩展注册（由 automind_pro.activate 回调）─────────────


class ExtensionAPI:
    """传递给商业扩展 ``activate()`` 的受控注册接口（稳定契约 v1）。"""

    api_version = EXTENSION_API_VERSION

    def register_feature(self, name: str, impl: Any) -> None:
        if name not in COMMERCIAL_FEATURES:
            logger.warning("edition_unknown_feature", feature=name)
        _state["features"][name] = impl

    def set_edition(self, edition: str, license: dict | None = None) -> None:
        if edition not in (EDITION_PRO, EDITION_ENTERPRISE):
            raise ValueError(f"非法版本名: {edition}")
        _state["edition"] = edition
        _state["license"] = license


# ── 扩展加载 ──────────────────────────────────────────────


def load_extensions(force: bool = False) -> str:
    """探测并激活商业扩展包（幂等；未安装 / 未授权则保持社区版）。

    返回激活后的版本名。任何扩展侧异常都不影响社区核心启动。
    """
    if _state["loaded"] and not force:
        return _state["edition"]
    _state["loaded"] = True
    try:
        import importlib

        pro = importlib.import_module("automind_pro")
    except ImportError:
        return _state["edition"]  # 未安装商业包 → 社区版
    try:
        ext_ver = getattr(pro, "EXTENSION_API_VERSION", 0)
        if ext_ver != EXTENSION_API_VERSION:
            logger.warning("edition_api_mismatch", core=EXTENSION_API_VERSION, ext=ext_ver)
            return _state["edition"]
        edition = pro.activate(ExtensionAPI())
        if edition:
            logger.info("edition_activated", edition=edition,
                        features=sorted(_state["features"]))
    except Exception as e:  # 商业包故障不拖垮社区核心
        logger.warning("edition_activate_failed", error=str(e))
    return _state["edition"]


def reset_for_tests() -> None:
    """重置版本状态（仅测试用）。"""
    _state.update({"edition": EDITION_COMMUNITY, "features": {},
                   "loaded": False, "license": None})
