"""v1.11.15：输入框模型选择 — 纯逻辑单测（automind/core/model_choice.py）。

不起服务、不碰网络、不依赖真实 LLM：这里只验证"可用模型清单怎么筛"与
"一条消息的模型选择怎么校验、回执怎么写"。
"""

from __future__ import annotations

from automind.core import model_choice as mc


def _payload(**over) -> dict:
    """一份典型配置：DeepSeek 配了 Key，OpenAI 没配。"""
    base = {
        "edition": "community",
        "router_config": None,
        "default": {"provider": "deepseek", "model": "deepseek-chat"},
        "current": {"provider": "deepseek", "model": "deepseek-chat"},
        "providers": {
            "deepseek": {"model": "deepseek-chat",
                         "custom_models": ["deepseek-chat", "deepseek-v4-pro"]},
        },
        "api_keys": {"deepseek": "sk-test"},
        "env_api_keys": {},
        "mode_models": {"coding": {"provider": "deepseek", "model": "deepseek-reasoner"}},
    }
    base.update(over)
    return base


# ═══════════════════════════════════════════════════════════
# 可用模型清单
# ═══════════════════════════════════════════════════════════


def test_available_only_lists_configured_providers():
    d = mc.build_available(_payload())
    providers = {o["provider"] for o in d["options"]}
    assert "deepseek" in providers
    # 没配 Key 的 OpenAI 不应出现（列出来只会让人点一下再失败）
    assert "openai" not in providers
    assert all(o["ready"] for o in d["options"])
    # ollama 本地推理不需要 Key，视为可用（排在最后）
    assert "ollama" in providers
    assert {p["provider"] for p in d["providers"]} >= {"deepseek", "ollama"}


def test_available_custom_models_first_and_deduped():
    d = mc.build_available(_payload())
    ds = [o for o in d["options"] if o["provider"] == "deepseek"]
    assert ds, "DeepSeek 应至少有一个可用模型"
    # 自定义收藏置顶（后加的在前），与 /api/models 的展示顺序一致
    assert ds[0]["model"] == "deepseek-v4-pro" and ds[0]["source"] == "custom"
    keys = [o["key"] for o in d["options"]]
    assert len(keys) == len(set(keys)), "options 不能有重复模型"
    # 当前 / 各模式正在用的模型一定在列表里（否则切走就切不回来）
    assert any(o["model"] == "deepseek-chat" for o in ds)
    assert any(o["model"] == "deepseek-reasoner" and o["source"] == "mode" for o in ds)


def test_available_merges_provider_aliases():
    # 只配了环境变量里的 MOONSHOT_API_KEY，用的 provider 名是别名 moonshot：
    # 可用列表只能出现一组（kimi / moonshot 不能各列一遍重复模型）
    payload = {
        "edition": "community",
        "default": {"provider": "moonshot", "model": "moonshot-v1-128k"},
        "current": {"provider": "moonshot", "model": "moonshot-v1-128k"},
        "providers": {},
        "api_keys": {},
        "env_api_keys": {"kimi": "mk-env"},
        "mode_models": {},
    }
    d = mc.build_available(payload)
    kimi_rows = [p for p in d["providers"] if p["provider"] in ("kimi", "moonshot")]
    assert len(kimi_rows) == 1
    row = kimi_rows[0]
    assert row["ready"] is True and row["provider"] == "moonshot"
    assert "moonshot-v1-128k" in row["models"]
    assert all(o["provider"] != "kimi" for o in d["options"])


def test_available_alias_saved_key_counts_as_ready():
    # 用户把 Key 保存在 kimi 下、当前用的是别名 moonshot —— 同一家服务，算配好了
    payload = {
        "edition": "community",
        "default": {"provider": "moonshot", "model": "moonshot-v1-128k"},
        "current": {"provider": "moonshot", "model": "moonshot-v1-128k"},
        "providers": {},
        "api_keys": {"kimi": "sk-saved"},
        "env_api_keys": {},
        "mode_models": {},
    }
    d = mc.build_available(payload)
    row = [p for p in d["providers"] if p["provider"] == "moonshot"][0]
    assert row["ready"] is True
    assert any(o["provider"] == "moonshot" and o["ready"] for o in d["options"])


def test_available_mode_models_and_defaults_returned():
    d = mc.build_available(_payload())
    assert d["default"] == {"provider": "deepseek", "model": "deepseek-chat"}
    assert d["current"] == {"provider": "deepseek", "model": "deepseek-chat"}


def test_available_auto_router_community_locked():
    d = mc.build_available(_payload())
    auto = d["auto"]
    assert auto["available"] is False
    assert auto["edition"] == "community"
    assert auto["enabled"] is False
    assert auto["tiers"] == []
    assert "专业版" in auto["reason"]
    # 社区版 auto 回执也要能正常渲染（前端据此显示 🔒 + 引导升级）
    res, rec = mc.resolve_choice({"mode": "auto"}, has_feature=False,
                                 default_llm=d["default"])
    assert res["switch"] is False and res["applied"] is False
    assert rec["model"]["mode"] == "auto" and rec["model"]["applied"] is False
    assert "专业版/企业版" in rec["model"]["reason"]


def test_available_auto_router_pro_tiers():
    payload = _payload(edition="pro", router_config={
        "enabled": True, "max_tiers": 2,
        "tiers": [
            {"name": "轻量", "provider": "deepseek", "model": "deepseek-chat",
             "max_score": 35},
            {"name": "强力", "provider": "deepseek", "model": "deepseek-reasoner",
             "max_score": 100},
        ],
    })
    auto = mc.build_available(payload)["auto"]
    assert auto["available"] is True and auto["enabled"] is True
    assert auto["edition"] == "pro"
    assert [t["name"] for t in auto["tiers"]] == ["轻量", "强力"]
    assert auto["summary"] == "轻量 → 强力"
    assert auto["max_tiers"] == 2


def test_available_auto_router_enabled_but_no_tiers():
    payload = _payload(edition="pro", router_config={"enabled": True, "tiers": []})
    auto = mc.build_available(payload)["auto"]
    assert auto["available"] is True and auto["enabled"] is True
    assert auto["tiers"] == [] and auto["summary"] == ""


# ═══════════════════════════════════════════════════════════
# 手动选择
# ═══════════════════════════════════════════════════════════


def test_manual_choice_valid():
    d = mc.build_available(_payload())
    res, rec = mc.resolve_choice(
        {"mode": "manual", "provider": "deepseek", "model": "deepseek-reasoner"},
        has_feature=False, default_llm=d["default"], available=d)
    assert res["applied"] is True and res["switch"] is True
    assert (res["provider"], res["model"]) == ("deepseek", "deepseek-reasoner")
    assert rec["model"]["applied"] is True
    assert rec["model"]["mode"] == "manual"
    assert rec["model"]["provider"] == "deepseek"
    assert rec["model"]["model"] == "deepseek-reasoner"


def test_manual_choice_invalid_model_not_silently_swapped():
    d = mc.build_available(_payload())
    res, rec = mc.resolve_choice(
        {"mode": "manual", "provider": "deepseek", "model": "gpt-4o"},
        default_llm=d["default"], available=d)
    assert res["applied"] is False and res["switch"] is False
    assert rec["model"]["applied"] is False
    assert "不可用" in rec["model"]["reason"]
    assert "gpt-4o" in rec["model"]["reason"]
    # 不能静默换成别的模型
    assert res["model"] == "gpt-4o"


def test_manual_choice_unconfigured_provider():
    d = mc.build_available(_payload())
    res, rec = mc.resolve_choice(
        {"mode": "manual", "provider": "openai", "model": "gpt-4o"},
        default_llm=d["default"], available=d)
    assert res["applied"] is False
    assert "不可用" in rec["model"]["reason"]


def test_manual_choice_missing_fields():
    d = mc.build_available(_payload())
    for choice in ({"mode": "manual"}, {"mode": "manual", "provider": "deepseek"},
                   {"mode": "manual", "model": "deepseek-chat"}):
        res, rec = mc.resolve_choice(choice, default_llm=d["default"], available=d)
        assert res["applied"] is False and res["switch"] is False
        assert "必填" in rec["model"]["reason"]


def test_choices_missing_or_empty_do_not_interfere():
    assert mc.resolve_choice(None) == (None, None)
    assert mc.resolve_choice({}) == (None, None)
    assert mc.resolve_choice("auto") == (None, None)


def test_default_choice_keeps_current_model():
    d = mc.build_available(_payload())
    res, rec = mc.resolve_choice({"mode": "default"}, default_llm=d["default"],
                                 available=d)
    assert res["applied"] is True and res["switch"] is False
    assert rec["model"]["mode"] == "default"
    assert rec["model"]["provider"] == "deepseek"


# ═══════════════════════════════════════════════════════════
# 自动路由
# ═══════════════════════════════════════════════════════════


def test_auto_choice_with_feature_hits_tier():
    calls: dict = {}

    def select(task, interaction, has_images):
        calls.update(task=task, interaction=interaction, has_images=has_images)
        return {"provider": "deepseek", "model": "deepseek-reasoner",
                "tier": "强力", "score": 88}

    res, rec = mc.resolve_choice(
        {"mode": "auto"}, auto_select=select, has_feature=True,
        default_llm={"provider": "deepseek", "model": "deepseek-chat"},
        task="重构这个模块并跑测试", interaction="coding", has_images=True)
    assert res["applied"] is True and res["switch"] is True
    assert (res["provider"], res["model"]) == ("deepseek", "deepseek-reasoner")
    assert res["tier"] == "强力" and res["score"] == 88
    assert calls == {"task": "重构这个模块并跑测试", "interaction": "coding",
                     "has_images": True}
    m = rec["model"]
    assert m["applied"] is True and m["mode"] == "auto"
    assert m["tier"] == "强力" and m["score"] == 88
    assert m["provider"] == "deepseek" and m["model"] == "deepseek-reasoner"
    assert "强力" in m["label"]


def test_auto_choice_router_disabled_uses_default_with_note():
    res, rec = mc.resolve_choice(
        {"mode": "auto"}, auto_select=lambda *_a, **_k: None, has_feature=True,
        default_llm={"provider": "deepseek", "model": "deepseek-chat"})
    # 不静默失败，也不谎报"自动路由生效"
    assert res["applied"] is True and res["switch"] is False
    assert "路由未启用" in rec["model"]["note"]
    assert rec["model"]["applied"] is True


def test_auto_choice_select_error_is_reported():
    def boom(*a, **k):
        raise RuntimeError("router exploded")

    res, rec = mc.resolve_choice({"mode": "auto"}, auto_select=boom, has_feature=True,
                                 default_llm={"provider": "deepseek", "model": "x"})
    assert res["applied"] is False and res["switch"] is False
    assert "自动路由选择失败" in rec["model"]["reason"]


def test_auto_choice_select_gives_empty_target():
    res, rec = mc.resolve_choice(
        {"mode": "auto"}, has_feature=True,
        auto_select=lambda *_a, **_k: {"provider": "", "model": "", "tier": "轻量"},
        default_llm={"provider": "deepseek", "model": "deepseek-chat"})
    assert res["applied"] is False
    assert "没有给出可用的档位目标模型" in rec["model"]["reason"]


def test_auto_choice_ignores_manual_provider_fields():
    # 自动路由模式下 choice 里的 provider/model 不参与决策，一切以 router.select 为准
    res, rec = mc.resolve_choice(
        {"mode": "auto", "provider": "openai", "model": "gpt-4o"},
        has_feature=True,
        auto_select=lambda *_a, **_k: {"provider": "deepseek", "model": "deepseek-chat",
                                       "tier": "轻量", "score": 20},
        default_llm={"provider": "deepseek", "model": "deepseek-chat"})
    assert res["switch"] is True
    assert res["provider"] == "deepseek" and res["model"] == "deepseek-chat"
