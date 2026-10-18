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
    custom_templates pro    ``attach(server_ctx)`` 注册 /api/templates/custom 路由
    audit_export   pro      ``attach(server_ctx)`` 注册 /api/audit/export 路由
    sso_ldap       ent      ``attach(server_ctx)`` 注册 /api/auth/* 路由；
                            通过 ``register_token_validator`` 接入鉴权中间件
    rbac           ent      ``attach(server_ctx)`` 注册 /api/rbac 路由；
                            ``check(role, action) -> bool``
    model_gateway  ent      ``attach(server_ctx)`` 注册 /api/gateway 路由
    observability  pro      ``attach(server_ctx)`` 注册 /api/observe/runs|
                            dashboard 路由；订阅
                            ``automind.core.observability.add_listener``
                            留存历史（社区版仅当前任务实时 DAG）
    workflow_triggers pro   ``attach(server_ctx)`` 注册触发器管理与入站
                            webhook 路由；``start()`` / ``stop()``；
                            ``on_restart(records)``（服务启动时收到被打断的
                            运行）；``on_run_finished(record, report)``
                            （失败重试与通知）；``count_active() -> int``
    workflow_parallel pro   ``await run_group(jobs, max_concurrency,
                            stop=None) -> list``：有界并发执行一组协程工厂
    workflow_history  pro   ``attach(server_ctx)`` 注册 /api/workflow/dashboard；
                            ``keep_days() -> int | None``（运行记录按天保留）
    workflow_from_trace pro ``attach(server_ctx)`` 注册 /api/workflow/from-trace
    workflow_templates pro  ``attach(server_ctx)`` 注册 /api/workflow/templates
    workflow_governance ent ``attach(server_ctx)``；``authorize(identity, verb, file)``、
                            ``check_run(file, schema, trigger=)``、``secrets_for(names)``、
                            ``await admit(run_id, team, on_queue) -> release``、
                            ``publish_state(file, digest)``
    ============= ======== ==============================================

``server_ctx``（服务端传给 attach 的上下文字典，稳定契约 v1）：
    app / get_agent / read_config / write_config / push_history /
    broadcast / task_history() / token_totals() / interaction_to_execution

    v1.9.0 追加（工作流，对既有扩展向后兼容）：
    workflow_catalog() / workflow_store() / start_workflow(...) /
    resume_workflow(run_id, ...) / workflow_running(file, trigger_key) / data_dir()

    v1.10.0 追加：request_identity(request) / workflow_authorize(request, verb, file) /
    workflow_can(identity, verb, file) / workflow_policy(file, schema, trigger=) /
    workflow_loader() / workflow_dir() / save_workflow_file(name, text, overwrite=)
"""

from __future__ import annotations

import json
from pathlib import Path
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
    "custom_templates": (EDITION_PRO, "自定义模板"),
    "audit_export": (EDITION_PRO, "审计报告导出（PDF）"),
    "session_pool": (EDITION_ENTERPRISE, "多用户会话池（执行态隔离）"),
    "experts_pro": (EDITION_PRO, "专家进阶（无限创建/分享/导入导出/统计）"),
    "sso_ldap": (EDITION_ENTERPRISE, "SSO / LDAP 单点登录集成"),
    "rbac": (EDITION_ENTERPRISE, "细粒度权限（RBAC）"),
    "model_gateway": (EDITION_ENTERPRISE, "私有模型网关"),
    "expert_approval": (EDITION_ENTERPRISE, "企业专家市场（审批发布）"),
    "rag_pro": (EDITION_PRO, "知识库进阶（无限文档/多库/Reranker/引用溯源）"),
    "kb_enterprise": (EDITION_ENTERPRISE, "知识库企业版（混合检索/热度统计/检索审计/批量导入）"),
    "semantic_cache": (EDITION_PRO, "语义缓存（相似问题秒回，省 Token）"),
    "model_router": (EDITION_PRO, "模型智能路由（按任务复杂度分级选模型）"),
    "cost_dashboard": (EDITION_ENTERPRISE, "成本仪表盘（模型成本/缓存节省分析）"),
    "observability": (EDITION_PRO, "观测中心（执行流程可视化 + 实时看板）"),
    # ── 内置工具的进阶能力（v1.5.0）──
    # 分级粒度是"动作"而非整个工具：社区版能真正用起 Excel/Word/PDF 等基础读写，
    # 只有少数进阶动作留给商业版。社区版**不实现**这些动作，运行时委派给
    # automind_pro 的特性对象 —— 因此这里列出的每一项都对应真实现，
    # 不存在"买了却提示不支持"的占位卖点。
    "office_pro": (EDITION_PRO, "办公进阶（Excel 样式 / Word 模板套打 / PDF 水印加密）"),
    "data_pro": (EDITION_PRO, "数据进阶（非 SQLite 数据库、写操作、批量导出）"),
    "integration_pro": (EDITION_PRO, "集成进阶（富文本模板消息）"),
    # ── 工作流无人值守（v1.9.0，docs/WORKFLOW_ROADMAP.md §5）──
    # 社区核心在**加载期**按这三个键拦截对应写法（triggers / parallel / 逐项并发），
    # 真正的调度循环、入站 webhook、并发执行器与历史看板都在 automind_pro 里。
    "workflow_triggers": (EDITION_PRO, "工作流触发器（定时 / webhook / 重启续跑 / 失败通知）"),
    "workflow_parallel": (EDITION_PRO, "工作流并行执行（parallel 组 / foreach 逐项并发）"),
    "workflow_history": (EDITION_PRO, "工作流运行历史（90 天）与看板"),
    # ── 工作流治理与固化（v1.10.0，路线图 §6）──
    "workflow_from_trace": (EDITION_PRO, "从执行轨迹生成工作流"),
    "workflow_templates": (EDITION_PRO, "自定义工作流模板库"),
    "workflow_governance": (EDITION_ENTERPRISE,
                            "工作流治理（发布审批 / 按工作流权限 / 多级审批 / 运行审计 / 凭据 / 并发配额）"),
}

#: **已规划、尚未实现**的商业特性：特性键 -> (最低版本, 中文名, 计划版本)。
#:
#: 为什么不直接放进 ``COMMERCIAL_FEATURES``：那张表的每一项都必须有真实现 ——
#: ``feature_flags()`` 按它生成，企业版许可证会被断言"全部为真"（pro/tests），
#: 界面也据它显示 🔒。把还没写的能力塞进去，企业版用户会看到五个"未授权"，
#: 那是"买了却提示不支持"的占位卖点。先登记在这里，社区核心就能在工作流**加载期**
#: 拦住这些写法、说清需要哪个版本（见 automind/workflow/schema.py 的 RESERVED_*）；
#: 实现交付时把键**移入** ``COMMERCIAL_FEATURES``。
PLANNED_FEATURES: dict[str, tuple[str, str, str]] = {
    # v1.9.0 已交付：workflow_triggers / workflow_parallel / workflow_history
    # v1.10.0 已交付：workflow_from_trace / workflow_templates / workflow_governance
    # （路线图三版的规划项至此全部移入 COMMERCIAL_FEATURES；表保留给后续规划）
}

#: 版本高低（判断"当前版本够不够"用）
EDITION_RANK: dict[str, int] = {"community": 0, "pro": 1, "enterprise": 2}

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

        _add_extensions_path()
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


# ── 应用内升级（v1.11.1）─────────────────────────────────
#
# 此前从社区版升到商业版要四步、全在应用外：pip 装 wheel → 设环境变量或手写
# 许可证文件 → 重启服务 → 看左上角徽标有没有变。桌面版更难：冻结包里没有 pip，
# 环境变量要改系统设置。许可证填错了也没有任何反馈，只是"重启后还是社区版"。
#
# 现在界面里一步完成：
#   · 商业扩展包可以直接上传 wheel，解压进 ``<数据目录>/extensions``（不需要 pip，
#     冻结包同样适用），加载时自动加入 sys.path；
#   · 许可证在界面里粘贴即校验，有效才写入 ``~/.automind/license``，
#     无效当场说清原因（签名不符 / 过期 / 格式错 / 公钥未配置）；
#   · 校验通过后**就地重新激活**，新特性的路由由服务端热挂载，不必重启。


def extensions_dir() -> Path:
    """商业扩展包的免 pip 安装目录（``<数据目录>/extensions``）。"""
    from automind.core.paths import data_dir

    return Path(data_dir()).resolve() / "extensions"


def license_file() -> Path:
    """应用内激活写入的许可证文件（与 automind_pro 的读取顺序一致）。"""
    return Path.home() / ".automind" / "license"


def _add_extensions_path() -> None:
    import sys

    try:
        d = extensions_dir()
    except Exception:
        return
    if (d / "automind_pro").is_dir() and str(d) not in sys.path:
        sys.path.insert(0, str(d))


def pro_package_info() -> dict:
    """商业扩展包是否可用：已安装？版本？协议版本是否匹配？"""
    import importlib

    _add_extensions_path()
    importlib.invalidate_caches()
    try:
        pro = importlib.import_module("automind_pro")
    except ImportError:
        return {"installed": False, "version": "", "compatible": False, "path": "",
                "api_version": None}
    except Exception as e:                      # 包损坏（半截解压 / 语法错）
        return {"installed": False, "version": "", "compatible": False, "path": "",
                "api_version": None, "error": f"商业扩展包无法加载：{e}"[:300]}
    ver = getattr(pro, "EXTENSION_API_VERSION", 0)
    return {"installed": True, "version": str(getattr(pro, "__version__", "")),
            "api_version": ver, "compatible": ver == EXTENSION_API_VERSION,
            "path": str(Path(getattr(pro, "__file__", "") or "").parent)}


def license_diagnosis() -> dict | None:
    """为什么没激活（由 automind_pro 给出；未安装商业包时为 None）。"""
    try:
        from automind_pro.license import license_status  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return license_status()
    except Exception as e:
        return {"reason": "status_failed", "error": str(e)[:200]}


_REASON_TEXT = {
    "no_license_configured": "还没有配置许可证",
    "public_key_not_provisioned": "商业扩展包缺少验签公钥（安装包不完整，请重新获取）",
    "license_present_but_invalid": "找到了许可证，但校验未通过（签名不符 / 已过期 / 格式错误）",
}


def upgrade_status() -> dict:
    """升级面板需要的全部信息：当前版本、扩展包、许可证、为什么没激活。"""
    load_extensions()
    pkg = pro_package_info()
    diag = license_diagnosis() if pkg["installed"] else None
    edition = _state["edition"]
    if edition != EDITION_COMMUNITY:
        step = "active"
    elif not pkg["installed"]:
        step = "need_package"
    elif not pkg["compatible"]:
        step = "incompatible"
    else:
        step = "need_license"
    reason = (diag or {}).get("reason", "")
    return {
        "edition": edition, "label": _EDITION_LABELS.get(edition, edition),
        "license": _state["license"], "package": pkg, "step": step,
        "reason": reason, "reason_text": _REASON_TEXT.get(reason, ""),
        "license_file": str(license_file()),
        "extensions_dir": str(extensions_dir()),
        "features": {k: k in _state["features"] for k in COMMERCIAL_FEATURES},
        "feature_labels": {k: {"tier": t, "label": lbl}
                           for k, (t, lbl) in COMMERCIAL_FEATURES.items()},
    }


def _reactivate() -> tuple[set[str], set[str]]:
    """重新执行扩展激活；返回 (新增特性, 失去的特性)。

    已在用的特性对象**保留原实例**：它们的路由早已挂在旧实例上、调度器也在旧实例里
    跑着，换成新实例只会出现"界面看到的和后台在跑的不是同一个"。
    """
    old = dict(_state["features"])
    _state.update({"edition": EDITION_COMMUNITY, "features": {}, "license": None})
    load_extensions(force=True)
    for k, impl in old.items():
        if k in _state["features"]:
            _state["features"][k] = impl
    now = set(_state["features"])
    return now - set(old), set(old) - now


def activate_license(raw: str) -> dict:
    """校验并激活许可证：有效才落盘，随后就地重新激活扩展。"""
    raw = (raw or "").strip()
    if not raw:
        return {"ok": False, "code": "empty", "error": "请粘贴许可证内容"}
    if len(raw) > 4096:
        return {"ok": False, "code": "invalid", "error": "许可证内容过长，请确认粘贴的是许可证本身"}
    pkg = pro_package_info()
    if not pkg["installed"]:
        return {"ok": False, "code": "need_package",
                "error": pkg.get("error") or "尚未安装商业扩展包 automind-pro：请先上传扩展包（.whl）"}
    if not pkg["compatible"]:
        return {"ok": False, "code": "incompatible",
                "error": f"商业扩展包协议版本（{pkg['api_version']}）与当前社区核心"
                         f"（{EXTENSION_API_VERSION}）不匹配，请获取与本版本配套的扩展包"}
    try:
        from automind_pro.license import parse_license  # type: ignore[import-not-found]

        info = parse_license(raw)
    except Exception as e:
        return {"ok": False, "code": "invalid", "error": f"许可证校验出错：{e}"[:300]}
    if info is None:
        return {"ok": False, "code": "invalid",
                "error": "许可证无效：签名校验未通过或已过期。请确认完整复制了整行许可证"}
    path = license_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(raw + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        return {"ok": False, "code": "write_failed", "error": f"许可证有效，但写入 {path} 失败：{e}"}
    added, lost = _reactivate()
    edition = _state["edition"]
    if edition == EDITION_COMMUNITY:
        # 文件写好了、单独校验也通过，激活却没生效 —— 多半是更高优先级的来源
        # （环境变量 AUTOMIND_LICENSE / 当前目录 .automind_license）里有一份别的许可证
        return {"ok": False, "code": "not_activated",
                "error": "许可证已保存但未能激活。若设置了环境变量 AUTOMIND_LICENSE 或当前目录有 "
                         ".automind_license，它们优先于界面里填写的许可证，请清除后重启。"}
    logger.info("license_activated_in_app", edition=edition, added=sorted(added))
    return {"ok": True, "edition": edition, "label": _EDITION_LABELS.get(edition, edition),
            "license": _state["license"], "added": sorted(added), "lost": sorted(lost)}


#: 上传扩展包的体积上限：automind-pro 的 wheel 不到 1MB，给足余量
MAX_PACKAGE_BYTES = 30 * 1024 * 1024

#: v1.11.7「一键升级包」（.ampkg）：发行方把扩展包 wheel + 许可证打成**一个文件**发给客户，
#: 客户拖进升级面板一次完成"装包 + 激活"。本质是一个 zip：
#:     automind-upgrade.json   {"format": 1, "tier": "PRO"|"ENT", "customer": ..., "expiry": ...}
#:     license.txt             许可证（一行）
#:     automind_pro-*.whl      扩展包
UPGRADE_MANIFEST = "automind-upgrade.json"


def _read_upgrade_bundle(data: bytes) -> dict[str, Any] | None:
    """是一键升级包就拆出 {wheel, license, manifest}；不是返回 None（按普通 wheel 处理）。"""
    import io
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    names = zf.namelist()
    if UPGRADE_MANIFEST not in names:
        return None
    try:
        manifest = json.loads(zf.read(UPGRADE_MANIFEST).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        manifest = {}
    wheels = [n for n in names if n.endswith(".whl") and "/" not in n]
    lic = zf.read("license.txt").decode("utf-8", errors="replace").strip() if "license.txt" in names else ""
    return {"manifest": manifest if isinstance(manifest, dict) else {},
            "wheel": zf.read(wheels[0]) if wheels else b"", "license": lic}


def install_upgrade(data: bytes) -> dict:
    """升级面板的统一入口：一键升级包（装包 + 激活）或单独的扩展包 wheel。"""
    if not data:
        return {"ok": False, "error": "没有收到文件"}
    if len(data) > MAX_PACKAGE_BYTES:
        return {"ok": False, "error": "文件过大，不像是 AutoMind 升级包"}
    bundle = _read_upgrade_bundle(data)
    if bundle is None:
        return install_pro_package(data)
    if not bundle["wheel"] and not pro_package_info()["installed"]:
        return {"ok": False, "error": "升级包里没有扩展包（.whl），请向发行方重新获取"}
    result: dict[str, Any] = {"ok": True, "bundle": True, "manifest": bundle["manifest"], "added": []}
    if bundle["wheel"]:
        pkg = install_pro_package(bundle["wheel"])
        if not pkg.get("ok"):
            return pkg
        result.update({k: pkg[k] for k in ("package", "restart_required", "edition") if k in pkg})
        result["added"] = list(pkg.get("added") or [])
        if pkg.get("restart_required"):
            # 已经加载过旧版扩展：新代码要重启后才生效；许可证先落盘，重启后自动激活
            if bundle["license"]:
                _store_license_text(bundle["license"])
                result["license_saved"] = True
            return result
    if bundle["license"]:
        act = activate_license(bundle["license"])
        if not act.get("ok"):
            act["package_installed"] = bool(bundle["wheel"])
            return act
        result.update({"edition": act["edition"], "label": act.get("label"),
                       "license": act.get("license")})
        result["added"] = sorted(set(result["added"]) | set(act.get("added") or []))
        result["lost"] = act.get("lost", [])
    return result


def _store_license_text(raw: str) -> None:
    path = license_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw.strip() + "\n", encoding="utf-8")
    except OSError as e:                             # pragma: no cover - 磁盘问题
        logger.warning("license_store_failed", error=str(e))


def install_pro_package(data: bytes) -> dict:
    """把上传的 automind-pro wheel 解压进扩展目录（免 pip，冻结包同样可用）。

    只接受 ``automind_pro/`` 与它自己的 ``automind_pro-*.dist-info/`` —— 出现别的
    顶层内容一律拒绝：这个入口不是通用的"装任意 Python 包"。
    """
    import io
    import shutil
    import sys
    import zipfile

    if not data:
        return {"ok": False, "error": "没有收到文件"}
    if len(data) > MAX_PACKAGE_BYTES:
        return {"ok": False, "error": "文件过大，不像是 automind-pro 扩展包"}
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"ok": False, "error": "不是有效的 .whl / .zip 文件"}
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if "automind_pro/__init__.py" not in names:
        return {"ok": False, "error": "压缩包里没有 automind_pro 包，请确认上传的是 automind-pro 的 .whl"}
    for n in names:
        parts = n.split("/")
        if n.startswith("/") or ".." in parts or "\\" in n or ":" in parts[0]:
            return {"ok": False, "error": f"压缩包含非法路径：{n}"}
        top = parts[0]
        if top != "automind_pro" and not (top.startswith("automind_pro-") and top.endswith(".dist-info")):
            return {"ok": False, "error": f"压缩包含 automind_pro 以外的内容：{top}"}

    dest = extensions_dir()
    staging = dest.parent / "extensions.staging"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        zf.extractall(staging)
        dest.mkdir(parents=True, exist_ok=True)
        # 先整体解压到暂存目录再替换：半截解压的包会让下次启动直接导入失败
        for child in list(dest.iterdir()):
            if child.name == "automind_pro" or child.name.startswith("automind_pro-"):
                shutil.rmtree(child, ignore_errors=True)
        for child in staging.iterdir():
            shutil.move(str(child), str(dest / child.name))
    except OSError as e:
        return {"ok": False, "error": f"解压扩展包失败：{e}"}
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 已导入过旧版本时只能重启才能换成新代码（Python 模块不可安全热替换）
    restart = "automind_pro" in sys.modules
    info = pro_package_info()
    if not info["installed"]:
        return {"ok": False, "error": info.get("error") or "扩展包已解压，但无法导入"}
    result: dict[str, Any] = {"ok": True, "package": info, "restart_required": restart,
                              "edition": _state["edition"], "added": []}
    if not restart and _state["edition"] == EDITION_COMMUNITY:
        # 机器上已经有许可证（之前配过）：装好包立即尝试激活
        added, _ = _reactivate()
        result["edition"] = _state["edition"]
        result["added"] = sorted(added)
    return result


def reset_for_tests() -> None:
    """重置版本状态（仅测试用）。"""
    _state.update({"edition": EDITION_COMMUNITY, "features": {},
                   "loaded": False, "license": None})
