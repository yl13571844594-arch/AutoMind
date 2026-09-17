"""Token → 成本估算 —— 评测报告里"这次跑了多少钱"的唯一来源。

为什么非要估成本：评测最大的隐性阻力是"跑一次要花钱"，而用户在看到
"一次全套 ≈ $0.4"之前是不敢把它接进 CI 的。价格表按**每百万 token** 美元计，
数据来自各家公开定价（2025 年口径），**会过时** —— 因此：

  · 每个条目标注了 ``as_of``，并在报告里注明"估算值，仅供成本控制参考"；
  · 未收录的模型退回一个偏保守的默认价并标 ``unknown=True``，
    宁可高估（触发预算告警）也不要低估（让用户以为很便宜）。

需要精确账单时以服务商账单为准，本模块只用于"同一套件前后两次跑谁更贵"。
"""

from __future__ import annotations

from dataclasses import dataclass

AS_OF = "2025-01"

#: 模型名（小写，前缀匹配）→ (输入价, 输出价) 美元 / 百万 token
_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    # DeepSeek
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    # Anthropic
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    # Google
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.5-pro": (1.25, 10.00),
    # 国产
    "qwen-max": (1.60, 6.40),
    "qwen-plus": (0.40, 1.20),
    "glm-4-plus": (0.70, 0.70),
    "moonshot-v1-8k": (1.68, 1.68),
    "doubao-pro-32k": (0.11, 0.28),
    # 本地
    "llama3": (0.0, 0.0),
    "qwen2.5": (0.0, 0.0),
}

#: 未收录模型时的保守估计（偏高，用于触发预算告警而不是掩盖成本）
_FALLBACK = (3.00, 15.00)


@dataclass
class CostEstimate:
    prompt_tokens: int
    completion_tokens: int
    usd: float
    model: str
    matched: str
    unknown: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "usd": round(self.usd, 6), "model": self.model,
                "priced_as": self.matched, "unknown_model": self.unknown}


def _lookup(model: str) -> tuple[tuple[float, float], str, bool]:
    m = (model or "").strip().lower()
    if not m:
        return _FALLBACK, "fallback", True
    if m in _PRICES:
        return _PRICES[m], m, False
    # 前缀匹配：'gpt-4o-2024-08-06' → 'gpt-4o'；取最长的命中，避免
    # 'gpt-4o-mini-...' 被 'gpt-4o' 抢先匹配成贵 16 倍的价
    hits = [k for k in _PRICES if m.startswith(k)]
    if hits:
        best = max(hits, key=len)
        return _PRICES[best], best, False
    return _FALLBACK, "fallback", True


def estimate(prompt_tokens: int, completion_tokens: int, model: str) -> CostEstimate:
    """估算一次（或累计一次）用量的美元成本。"""
    (pin, pout), matched, unknown = _lookup(model)
    usd = (int(prompt_tokens or 0) / 1_000_000) * pin \
        + (int(completion_tokens or 0) / 1_000_000) * pout
    return CostEstimate(int(prompt_tokens or 0), int(completion_tokens or 0),
                        usd, model or "(未知)", matched, unknown)


def price_of(model: str) -> tuple[float, float]:
    """返回 ``(输入价, 输出价)``（美元/百万 token），供报告展示单价。"""
    (pin, pout), _, _ = _lookup(model)
    return pin, pout
