"""输入框模型选择 — 可用模型清单 + 每条消息的模型选择解析（纯逻辑，便于单测）。

v1.11.15 起，输入框旁可以为本条消息指定模型（社区版手动选 / 专业·企业版还可
"自动路由"）。服务端要做两件事，都集中在这里，服务层只做薄封装：

1. ``build_available(payload)``
   把「默认配置 + 提供商配置 + 已保存的 Key + 各模式模型 + 路由配置」整理成
   前端选择器能直接渲染的结构：默认/当前模型、按 provider 分组的可用模型、
   自动路由是否可用（社区版 ``auto.available=False`` 并给出升级原因）。

   **只把「拿得到 Key」的提供商的模型列进 options**（``ollama`` 本地推理不需要
   Key，视为可用；``custom`` 填了合法 api_base 也算配好了）。没配 Key 的提供商
   不列出 —— 列出来让用户点一下再失败，不如直接说"先去配 Key"。

2. ``resolve_choice(choice, ...)``
   解析一条消息携带的选择：``{"mode":"default"}`` / ``{"mode":"manual",
   "provider","model"}`` / ``{"mode":"auto"}``，返回 ``(result, receipt)``。
   校验失败**绝不静默换模型**：result.applied=False + 人话原因，服务端照它写回执。

   自动路由在无 ``model_router`` 特性（社区版）时明确回复"需要专业版/企业版"；
   有特性但路由未启用/未配档位时 applied=True + note"路由未启用，使用当前默认
   模型"（不能让用户以为自动路由生效了，也不能让它看起来像失败）。

设计约束：本模块只依赖标准库与同目录的 provider 元数据，**不 import server /
agent / FastAPI** —— 这样纯逻辑单测不需要起服务、不碰网络。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from automind.core.provider_resolver import PROVIDER_LABELS as _PROVIDER_LABELS

#: provider → 推荐模型。``/api/models`` 与输入框模型选择器**共用这一份**，
#: 避免两处各写一份、加一个新模型只改了一边。
MODEL_MAP: dict[str, list[str]] = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4-turbo", "o1", "gpt-3.5-turbo"],
    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-v4-flash"],
    "kimi": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "bailian": ["qwen-max", "qwen-plus", "qwen-turbo"],
    "dashscope": ["qwen-max", "qwen-plus", "qwen-turbo"],
    "qwen": ["qwen-max", "qwen-plus", "qwen-turbo"],
    "zhipu": ["glm-4-plus", "glm-4-flash", "glm-4-air"],
    "glm": ["glm-4-plus", "glm-4-flash"],
    "doubao": ["doubao-pro-128k", "doubao-pro-32k"],
    "google": ["gemini-2.5-flash", "gemini-2.5-pro"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro"],
    "grok": ["grok-3", "grok-2"],
    "ollama": ["llama3.2", "mistral", "codellama", "qwen2.5", "deepseek-coder"],
    "custom": ["gpt-4o", "gpt-3.5-turbo"],
}

#: 同一家服务的多个 provider 别名 —— 可用列表里只保留**一个代表**。
#: 只配了一个 MOONSHOT_API_KEY 时，若不归并就会同时冒出 kimi / moonshot 两组
#: 一模一样的模型；五组阿里系别名（bailian / dashscope / qwen）会更夸张。
#: 元组第一个是规范名（没有显式引用时用它）。
PROVIDER_GROUPS: tuple[tuple[str, ...], ...] = (
    ("openai",),
    ("anthropic",),
    ("deepseek",),
    ("kimi", "moonshot"),
    ("bailian", "dashscope", "qwen"),
    ("zhipu", "glm"),
    ("doubao",),
    ("google", "gemini"),
    ("grok",),
    ("custom",),
    ("ollama",),
)

#: 所有已知 provider（规范化后）。服务层用它一次性算出各家环境变量 Key。
KNOWN_PROVIDERS: tuple[str, ...] = tuple(p for group in PROVIDER_GROUPS for p in group)

_GROUP_OF: dict[str, tuple[str, ...]] = {
    p: group for group in PROVIDER_GROUPS for p in group
}

#: 版本中文名
EDITION_LABELS: dict[str, str] = {
    "community": "社区版",
    "pro": "专业版",
    "enterprise": "企业版",
}

#: 自动路由不可用时的原因文案（前端置灰 + 🔒 时展示）
AUTO_ROUTER_REASON = (
    "自动路由是 AutoMind 专业版/企业版功能（按任务复杂度自动切换模型档位），"
    "当前版本不可用"
)

#: 模型选择的合法模式
CHOICE_MODES = ("default", "manual", "auto")


def recommended_models(provider: str) -> list[str]:
    """某 provider 的推荐模型（未知 provider 返回空列表）。"""
    return list(MODEL_MAP.get((provider or "").lower(), []))


def provider_label(provider: str) -> str:
    """provider 的中文显示名（未知时原样返回）。"""
    p = (provider or "").lower()
    return _PROVIDER_LABELS.get(p, p)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _aliases(provider: str) -> tuple[str, ...]:
    p = (provider or "").lower()
    return _GROUP_OF.get(p, (p,) if p else ())


def _env_key_for(provider: str, env_api_keys: dict) -> str:
    """按别名组找环境变量 Key（同组别名共用一个环境变量）。"""
    for alias in _aliases(provider):
        v = str(env_api_keys.get(alias) or "").strip()
        if v:
            return v
    return ""


def _saved_key_for(provider: str, api_keys: dict) -> str:
    """按别名组找已保存的 Key（用户可能在 kimi 下保存、却在 moonshot 下使用）。"""
    for alias in _aliases(provider):
        v = str(api_keys.get(alias) or "").strip()
        if v:
            return v
    return ""


def _has_key(provider: str, providers: dict, api_keys: dict, env_api_keys: dict,
             current: dict | None = None) -> bool:
    """该 provider 是否"配好了、能用"。

    * ``ollama``：本地推理不需要 Key —— 视为可用；
    * ``custom``：填了 api_base（OpenAI 标准 / 中转代理）就算配好；
    * 其余：已保存的 Key 或对应环境变量非空。
    """
    p = (provider or "").lower()
    if not p:
        return False
    if p == "ollama":
        return True
    if current and (current.get("provider") or "").lower() == p \
            and current.get("has_key"):
        return True
    if str(_saved_key_for(p, api_keys)) or _env_key_for(p, env_api_keys):
        return True
    if p == "custom":
        return bool(str(_as_dict(providers.get(p)).get("api_base") or "").strip())
    return False


def _representative(group: tuple[str, ...], referenced: dict[str, int],
                    providers: dict, api_keys: dict) -> str:
    """一组别名里选一个代表：显式引用 > 有配置 > 有已保存 Key > 规范名。

    显式引用之间按优先级（当前 > 默认 > 模式 > 配置 > 已保存 Key）取最高的 —
    不能简单地"按组内顺序取第一个被提到的"：用户当前用 moonshot、Key 却保存在
    kimi 名下时，必须让 moonshot 当代表（否则可用列表把当前模型挪到别名组里）。
    """
    refs = sorted((referenced[p], i, p) for i, p in enumerate(group) if p in referenced)
    if refs:
        return refs[0][2]
    for p in group:
        if p in providers:
            return p
    for p in group:
        if str(api_keys.get(p) or "").strip():
            return p
    return group[0]


def _referenced_providers(payload: dict) -> dict[str, int]:
    """本次配置里被显式提到的 provider（数字越小优先级越高）。"""
    order: dict[str, int] = {}

    def _add(provider: Any, rank: int) -> None:
        p = str(provider or "").strip().lower()
        if p and p not in order:
            order[p] = rank

    current = _as_dict(payload.get("current"))
    default = _as_dict(payload.get("default"))
    _add(current.get("provider"), 0)
    _add(default.get("provider"), 1)
    for mm in _as_dict(payload.get("mode_models")).values():
        _add(_as_dict(mm).get("provider"), 2)
    for p in _as_dict(payload.get("providers")):
        _add(p, 3)
    for p in _as_dict(payload.get("api_keys")):
        _add(p, 4)
    return order


def _clean_tiers(router_config: dict | None) -> list[dict]:
    if not isinstance(router_config, dict):
        return []
    tiers = router_config.get("tiers")
    if not isinstance(tiers, list):
        return []
    out: list[dict] = []
    for i, t in enumerate(tiers):
        t = _as_dict(t)
        provider = str(t.get("provider") or "")
        model = str(t.get("model") or "")
        if not provider or not model:
            continue
        out.append({
            "name": str(t.get("name") or f"档位{i + 1}"),
            "provider": provider,
            "model": model,
            "max_score": _as_int(t.get("max_score"), 100),
        })
    return out


def build_auto(payload: dict) -> dict:
    """自动路由状态（路由配置由服务层从 ``model_router.config()`` 取出传入）。

    ``router_config is None`` = 当前版本没有 ``model_router`` 特性（社区版）。
    """
    router_config = payload.get("router_config")
    edition = str(payload.get("edition") or "community")
    available = isinstance(router_config, dict)
    tiers = _clean_tiers(router_config)
    enabled = bool(_as_dict(router_config).get("enabled")) if available else False
    summary = " → ".join(t["name"] for t in tiers) if tiers else ""
    return {
        "available": available,
        "edition": edition,
        "edition_label": EDITION_LABELS.get(edition, edition),
        "enabled": enabled,
        "tiers": tiers,
        "summary": summary,
        "max_tiers": (router_config or {}).get("max_tiers") if available else None,
        "reason": "" if available else AUTO_ROUTER_REASON,
    }


def _option(provider: str, model: str, label_provider: str, ready: bool,
            source: str) -> dict:
    return {
        # key 供前端做 React key / 勾选判断；provider 与 model 分开存，
        # 因为 provider 名里也可能出现斜杠（不会，但别把协议绑死在拼接格式上）
        "key": f"{provider}/{model}",
        "provider": provider,
        "provider_label": provider_label(label_provider),
        "model": model,
        "label": model,
        "ready": ready,
        "source": source,
    }


def _options_for(provider: str, *, providers: dict, current: dict,
                 default: dict, mode_models: dict, ready: bool) -> list[dict]:
    """某 provider 的候选模型（自定义收藏置顶，按模型名去重）。"""
    cfg = _as_dict(providers.get(provider))
    seen: set[str] = set()
    options: list[dict] = []

    def _add(model: Any, source: str) -> None:
        m = str(model or "").strip()
        if not m or m in seen:
            return
        seen.add(m)
        options.append(_option(provider, m, provider, ready, source))

    # 1) 自定义收藏模型置顶（后加的在前，与 /api/models 的展示一致）
    for m in reversed(cfg.get("custom_models") or []):
        _add(m, "custom")
    # 2) 当前 / 默认 / 各模式正在用的模型（哪怕不在推荐列表里也要能选回来 ——
    #    否则自定义模型名没收藏过就再也选不到了）
    for item, source in ((current, "current"), (default, "default")):
        if (item.get("provider") or "").lower() == provider:
            _add(item.get("model"), source)
    for mm in mode_models.values():
        mm = _as_dict(mm)
        if (mm.get("provider") or "").lower() == provider:
            _add(mm.get("model"), "mode")
    # 3) 提供商配置里记着的模型
    _add(cfg.get("model"), "provider")
    # 4) 推荐模型
    for m in recommended_models(provider):
        _add(m, "recommended")
    return options


def build_available(payload: dict) -> dict:
    """构建输入框模型选择器的数据（纯函数）。

    Args:
        payload: 由服务层组装（键都可缺省）::

            {
              "default": {"provider", "model"},     # 全局默认模型
              "current": {"provider", "model"},     # 本会话当前生效模型
              "providers": {provider: {api_base, model, custom_models}},
              "api_keys": {provider: key},          # 已保存的 Key
              "env_api_keys": {provider: key},      # 环境变量里的 Key
              "mode_models": {mode: {"provider", "model"}},
              "edition": "community|pro|enterprise",
              "router_config": dict | None,         # model_router.config()；无特性为 None
            }

    Returns:
        ``{default, current, options, providers, auto}``，见模块文档。
    """
    payload = _as_dict(payload)
    default = _as_dict(payload.get("default"))
    current = _as_dict(payload.get("current"))
    providers = _as_dict(payload.get("providers"))
    api_keys = _as_dict(payload.get("api_keys"))
    env_api_keys = _as_dict(payload.get("env_api_keys"))
    mode_models = _as_dict(payload.get("mode_models"))
    referenced = _referenced_providers(payload)

    # ── 1) 每组别名选一个代表，判断是否配好 ──
    entries: list[dict] = []
    for group in PROVIDER_GROUPS:
        rep = _representative(group, referenced, providers, api_keys)
        ready = _has_key(rep, providers, api_keys, env_api_keys, current)
        in_play = ready or rep in referenced
        if not in_play:
            continue
        entries.append({"provider": rep, "ready": ready,
                        "rank": referenced.get(rep, 9)})

    # 当前 provider 排最前（用户在选的时候最可能就是想换/确认当前这家的其它模型），
    # 其次是全局默认，然后按目录顺序。同 rank 保持 PROVIDER_GROUPS 的顺序。
    entries.sort(key=lambda e: (e["rank"], KNOWN_PROVIDERS.index(e["provider"])
                                if e["provider"] in KNOWN_PROVIDERS else 99,))

    # ── 2) options：只列配好的提供商（社区版没配任何 Key 时一个也不列） ──
    options: list[dict] = []
    provider_rows: list[dict] = []
    for order, entry in enumerate(entries):
        provider, ready = entry["provider"], entry["ready"]
        models = _options_for(provider, providers=providers,
                              current=current, default=default,
                              mode_models=mode_models, ready=ready)
        provider_rows.append({
            "provider": provider,
            "label": provider_label(provider),
            "ready": ready,
            "models": [o["model"] for o in models],
        })
        if ready:
            options.extend(models)

    return {
        "default": {"provider": str(default.get("provider") or ""),
                    "model": str(default.get("model") or "")},
        "current": {"provider": str(current.get("provider") or ""),
                    "model": str(current.get("model") or "")},
        "options": options,
        "providers": provider_rows,
        "auto": build_auto(payload),
    }


# ═══════════════════════════════════════════════════════════
# 每条消息的模型选择解析
# ═══════════════════════════════════════════════════════════


def _receipt(*, mode: str, label: str, applied: bool, provider: str = "",
             model: str = "", value: str = "", reason: str = "",
             tier: str = "", score: Any = None, note: str = "") -> dict:
    """回执结构 —— 随 ``task_start`` 的 ``overrides`` 回给前端。"""
    return {"model": {
        "value": value or label,
        "label": label,
        "provider": provider,
        "model": model,
        "applied": bool(applied),
        "reason": reason,
        "tier": tier,
        "score": score,
        "mode": mode,
        "note": note,
    }}


def _result(*, mode: str, applied: bool, switch: bool, provider: str = "",
            model: str = "", reason: str = "", note: str = "",
            tier: str = "", score: Any = None) -> dict:
    """给服务层的执行指令：``switch=True`` 表示要把本会话 LLM 换过去。"""
    return {"mode": mode, "applied": bool(applied), "switch": bool(switch),
            "provider": provider, "model": model, "reason": reason,
            "note": note, "tier": tier, "score": score}


def resolve_choice(choice: Any, *, auto_select: Callable[..., dict | None] | None = None,
                   has_feature: bool = False, default_llm: dict | None = None,
                   available: dict | None = None, options: list[dict] | None = None,
                   task: str = "", interaction: str = "chat",
                   has_images: bool = False) -> tuple[dict | None, dict | None]:
    """解析一条消息的模型选择。

    Args:
        choice: ``{"mode":"default"}`` / ``{"mode":"manual","provider","model"}`` /
            ``{"mode":"auto"}``；不是 dict 或为空 → 返回 ``(None, None)``（不干预）。
        auto_select: 自动路由选择器 ``f(task, interaction, has_images) -> dict|None``，
            通常就是 ``model_router.select``。返回 ``{provider, model, tier, score}``。
        has_feature: 当前版本是否具备 ``model_router`` 特性（社区版为 False）。
        default_llm: ``{"provider","model"}`` 当前默认模型（回执文案 / auto 回落用）。
        available: :func:`build_available` 的结果（manual 校验用）。
        options: 直接给校验名单（优先于 ``available["options"]``，单测方便）。
        task / interaction / has_images: 传给 ``auto_select`` 的任务信息。

    Returns:
        ``(result, receipt)``。result 是给服务层的指令（要不要 switch_llm）；
        receipt 是给前端的 ``{"model": {...}}`` 回执。
    """
    if not isinstance(choice, dict) or not choice:
        return None, None
    mode = str(choice.get("mode") or "").strip().lower()
    if not mode:
        # 兼容只传 provider/model 的老写法：有目标即 manual，否则 default
        mode = "manual" if (choice.get("provider") or choice.get("model")) else "default"

    if mode not in CHOICE_MODES:
        reason = f"未知的模型选择方式：{mode}"
        return (_result(mode="default", applied=False, switch=False, reason=reason),
                _receipt(mode="default", label="🧠 默认模型", applied=False,
                         reason=reason))

    if mode == "default":
        d = _as_dict(default_llm)
        return (
            _result(mode="default", applied=True, switch=False,
                    provider=str(d.get("provider") or ""),
                    model=str(d.get("model") or "")),
            _receipt(mode="default", label="🧠 默认模型", applied=True,
                     provider=str(d.get("provider") or ""),
                     model=str(d.get("model") or ""),
                     note="使用全局默认模型"),
        )

    if mode == "manual":
        provider = str(choice.get("provider") or "").strip().lower()
        model = str(choice.get("model") or "").strip()
        opts = options
        if opts is None:
            opts = _as_dict(available).get("options") or []
        opts = [o for o in opts if isinstance(o, dict)]
        if not provider or not model:
            reason = "没有指定要使用的模型（provider / model 均必填），本条继续使用默认模型"
            return (_result(mode="manual", applied=False, switch=False, reason=reason),
                    _receipt(mode="manual", label="🧠 模型", applied=False,
                             reason=reason))
        valid = any((str(o.get("provider") or "").lower() == provider
                     and str(o.get("model") or "") == model
                     and o.get("ready", True)) for o in opts)
        if not valid:
            reason = (f"选择的模型 {provider}/{model} 当前不可用"
                      "（未配置 API Key 或不在可用模型列表中），本条继续使用默认模型")
            if not opts:
                reason += "；当前没有任何可用的模型配置，请先在「模型配置」中填写 API Key"
            return (_result(mode="manual", applied=False, switch=False,
                            provider=provider, model=model, reason=reason),
                    _receipt(mode="manual", label=f"🧠 {provider}/{model}",
                             applied=False, provider=provider, model=model,
                             reason=reason))
        return (
            _result(mode="manual", applied=True, switch=True,
                    provider=provider, model=model),
            _receipt(mode="manual", label=f"🧠 {provider}/{model}",
                     applied=True, provider=provider, model=model,
                     note="本条消息使用该模型"),
        )

    # ── auto：自动路由（专业版 / 企业版特性）──
    if not has_feature:
        return (
            _result(mode="auto", applied=False, switch=False,
                    reason=AUTO_ROUTER_REASON),
            _receipt(mode="auto", label="🧠 自动路由", applied=False,
                     reason=AUTO_ROUTER_REASON),
        )

    sel: dict | None = None
    select_err = ""
    if auto_select is not None:
        try:
            sel = auto_select(task, interaction, has_images)
        except TypeError:
            # 兼容只收 task 的选择器（第三方扩展 / 单测里的简化实现）
            try:
                sel = auto_select(task)
            except Exception as e:                 # pragma: no cover - 防御
                select_err = str(e)
        except Exception as e:
            select_err = str(e)
    if select_err:
        reason = f"自动路由选择失败：{select_err}，本条继续使用默认模型"
        return (_result(mode="auto", applied=False, switch=False, reason=reason),
                _receipt(mode="auto", label="🧠 自动路由", applied=False, reason=reason))

    if not sel:
        # 有特性但路由未启用 / 未配档位：**不是失败**，但绝不能让用户以为
        # "自动路由生效了" —— 明确说明本条用的仍是当前默认模型。
        note = "路由未启用，使用当前默认模型"
        d = _as_dict(default_llm)
        return (
            _result(mode="auto", applied=True, switch=False, note=note,
                    provider=str(d.get("provider") or ""),
                    model=str(d.get("model") or "")),
            _receipt(mode="auto", label="🧠 自动路由", applied=True, note=note,
                     provider=str(d.get("provider") or ""),
                     model=str(d.get("model") or "")),
        )

    provider = str(sel.get("provider") or "").strip()
    model = str(sel.get("model") or "").strip()
    tier = str(sel.get("tier") or "")
    score = sel.get("score")
    if not provider or not model:
        reason = "自动路由没有给出可用的档位目标模型，本条继续使用默认模型"
        return (_result(mode="auto", applied=False, switch=False, tier=tier,
                        score=score, reason=reason),
                _receipt(mode="auto", label="🧠 自动路由", applied=False,
                         tier=tier, score=score, reason=reason))
    label = "🧠 自动路由" + (f" · {tier}" if tier else "")
    return (
        _result(mode="auto", applied=True, switch=True, provider=provider,
                model=model, tier=tier, score=score,
                note=f"已按任务复杂度自动选择 {tier or '档位'}"),
        _receipt(mode="auto", label=label, applied=True, provider=provider,
                 model=model, tier=tier, score=score,
                 note="按任务复杂度自动选择"),
    )
