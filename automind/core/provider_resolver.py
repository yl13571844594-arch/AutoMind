"""默认模型与实际可用 Key 的对齐 — 开箱即用的兜底选路。

背景
----
代码默认 ``provider=openai`` / ``model=gpt-4o``，但绝大多数国内用户只配了
DeepSeek 之类某一家的 Key。结果是 CLI / Web 一打开就 ``llm_init_failed
provider='openai'``，界面只说"模型连接失败"，用户根本不知道要去设置里把模型
切成自己那家 —— 一个纯配置问题被呈现成了产品坏掉。

这里做的事很简单：**当默认（或当前选中的）提供商没有可用 Key，而另一家有，
就自动改用有 Key 的那家，并给一句明确的中文提示。** 提示由调用方负责显示
（CLI 打印、Web 放进 /api/status），本模块只负责判断与措辞。

不自动回退到 ollama：它不需要 Key，但需要本地跑着服务，静默切过去只会把
"没配 Key"换成一个更难懂的连接超时。
"""

from __future__ import annotations

import os

#: provider → 环境变量名（config.py / server_store.py 共用此表，避免三处各写一份）
ENV_KEY_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "grok": "GROK_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "bailian": "DASHSCOPE_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "doubao": "DOUBAO_API_KEY",
    "custom": "CUSTOM_API_KEY",
}

#: provider → 默认模型（与 /api/providers 的 defaults 保持一致）
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "deepseek": "deepseek-chat",
    "kimi": "moonshot-v1-128k",
    "moonshot": "moonshot-v1-128k",
    "bailian": "qwen-max",
    "dashscope": "qwen-max",
    "qwen": "qwen-max",
    "zhipu": "glm-4-plus",
    "glm": "glm-4-plus",
    "doubao": "doubao-pro-128k",
    "google": "gemini-2.5-flash",
    "gemini": "gemini-2.5-flash",
    "grok": "grok-3",
    "ollama": "llama3.2",
    "custom": "gpt-4o",
}

#: provider → 中文名（提示语里用，别让用户看 "bailian" 猜是谁）
PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic Claude",
    "deepseek": "DeepSeek",
    "kimi": "Kimi（月之暗面）",
    "moonshot": "Moonshot",
    "bailian": "阿里百炼",
    "dashscope": "DashScope",
    "qwen": "通义千问",
    "zhipu": "智谱 GLM",
    "glm": "智谱 GLM",
    "doubao": "字节豆包",
    "google": "Google Gemini",
    "gemini": "Google Gemini",
    "grok": "xAI Grok",
    "ollama": "Ollama（本地）",
    "custom": "自定义接口",
}

#: 回退优先级 —— 多家都有 Key 时按此顺序挑（同一家的别名只留一个代表）
_FALLBACK_ORDER: tuple[str, ...] = (
    "deepseek", "kimi", "bailian", "zhipu", "doubao",
    "openai", "anthropic", "google", "grok",
)


def label(provider: str) -> str:
    """提供商的中文显示名（未知时原样返回）。"""
    return PROVIDER_LABELS.get((provider or "").lower(), provider or "")


def default_model(provider: str) -> str:
    """提供商的默认模型（未知时返回空串）。"""
    return PROVIDER_DEFAULT_MODELS.get((provider or "").lower(), "")


def env_api_key(provider: str) -> str:
    """从环境变量读取某提供商的 Key（未配置时为空串）。"""
    return os.environ.get(ENV_KEY_MAP.get((provider or "").lower(), ""), "")


def has_api_key(provider: str, saved_keys: dict[str, str] | None = None) -> bool:
    """该提供商是否拿得到 Key（已保存的配置优先于环境变量）。"""
    p = (provider or "").lower()
    if not p:
        return False
    if p in ("ollama",):
        return True          # 本地推理无需 Key
    if saved_keys and (saved_keys.get(p) or "").strip():
        return True
    return bool(env_api_key(p).strip())


def configured_providers(saved_keys: dict[str, str] | None = None) -> list[str]:
    """列出所有拿得到 Key 的云端提供商，按回退优先级排序。"""
    return [p for p in _FALLBACK_ORDER if has_api_key(p, saved_keys)]


def resolve(
    provider: str,
    model: str = "",
    api_key: str = "",
    saved_keys: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """把"选中的提供商"对齐到"实际配得起的提供商"。

    Args:
        provider: 当前选中的提供商。
        model: 当前选中的模型（换提供商时会换成新家的默认模型）。
        api_key: 调用方已经解析出来的 Key（非空即视为当前提供商可用）。
        saved_keys: 配置文件里保存的 provider → key 映射。

    Returns:
        ``(provider, model, note)``。``note`` 非空时是一句面向用户的中文提示，
        说明发生了什么切换；无需切换时 ``note`` 为空串、provider/model 原样返回。
    """
    p = (provider or "").lower()
    if (api_key or "").strip() or has_api_key(p, saved_keys):
        return provider, model, ""

    candidates = [c for c in configured_providers(saved_keys) if c != p]
    if not candidates:
        # 一个 Key 都没有 —— 这不是"选错家"，如实说清楚该去哪配
        return provider, model, (
            f"尚未配置任何模型 API Key（当前默认 {label(p) or p}）。"
            "请设置对应的环境变量（如 DEEPSEEK_API_KEY / OPENAI_API_KEY）后重启，"
            "或在 Web 界面「设置 → API Keys」中填写。"
        )

    new_provider = candidates[0]
    new_model = default_model(new_provider) or model
    note = (
        f"未检测到 {label(p) or p} 的 API Key，"
        f"已检测到 {label(new_provider)} Key，将使用 {new_model}。"
        "如需改用其它提供商：命令行加 --provider，Web 在「设置 → 模型」中切换。"
    )
    return new_provider, new_model, note
