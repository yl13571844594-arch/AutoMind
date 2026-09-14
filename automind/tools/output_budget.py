"""工具输出体积治理 —— 当前轮刚产生的大输出，必须在进上下文之前夹住。

问题（v1.6.3 及更早）：``FunctionCallHandler.tool_results_to_messages`` 把
``str(result.output)`` **原样**放进下一轮 LLM 请求，没有任何截断保护
（``s[:57]`` 那种截断只存在于 ``_format_args`` 摘要里，根本不进上下文）。
一次大文件读取、一次长网页抓取、一次几千行的终端输出 = 下一轮请求体整体
重发一遍，而且此后每一轮都要重发。

已有折叠机制（``ReActExecutor.compact`` / ``OBS_KEEP_CHARS``）只在 token 预算
用到 80% 之后才触发，且压的是**历史消息**——当前轮刚产生的那条大输出会先一步
把上下文撑爆，折叠还没来得及跑。本模块补齐这一环：

  · **先夹后发**（:func:`limited_tool_content`）：任何工具结果进消息之前，
    先按「头部 + 尾部」策略截断，中间用明确标记说明被省掉了多少字符、
    以及**怎么拿回完整内容**（缩小参数重读 / 用 offset+limit 分段 / 后台重跑）。
  · **不是简单砍掉中段**：报错与结论通常在**尾部**，命令的用法/表头通常在
    **头部**，所以两头都留，砍中间 —— 与 ``compact`` 只留头部的做法互补。
  · **可解释**：每次截断都返回结构化账目（原长度、保留长度、省下多少），
    由调用方推入观测中心，让"这次任务到底被截了多少上下文"看得见。

配置（``ExecutionConfig``）：
  ``tool_output_max_chars`` / ``tool_output_head_chars`` /
  ``tool_output_tail_chars`` / ``tool_output_max_tokens``。
  把 ``tool_output_max_chars`` 设为 0 即恢复旧行为（原样下发）。
"""

from __future__ import annotations

from typing import Any

#: 与 HEAD/TAIL 配套的硬上限估算：1 token ≈ 3.5 字符（中英混排的保守值）
CHARS_PER_TOKEN = 3.5

#: 单个工具结果的绝对兜底上限 —— 即便配置被改坏也不会把请求体撑爆
HARD_MAX_CHARS = 200_000


def limits_from_config(ex: Any = None) -> dict[str, int]:
    """从 ExecutionConfig 取限额；缺字段时用与默认配置一致的值。"""
    def _get(name: str, default: int) -> int:
        try:
            v = getattr(ex, name, None)
            return default if v is None else int(v)
        except Exception:
            return default

    return {
        "max_chars": _get("tool_output_max_chars", 12000),
        "head": _get("tool_output_head_chars", 7000),
        "tail": _get("tool_output_tail_chars", 3000),
        "max_tokens": _get("tool_output_max_tokens", 6000),
    }


def truncation_marker(original: int, kept: int) -> str:
    """给模型看的截断说明 —— 必须包含"怎么拿回完整内容"的可执行指引。"""
    return (
        f"\n\n…[工具输出过长，已省略中间 {original - kept} 字符（原始 {original} 字符）。"
        f"如需完整内容，请**缩小参数范围**后重新调用该工具"
        f"（例如：读取文件用 offset/limit 分段、只取需要的那一段；"
        f"命令加过滤条件或只输出关键行；网页抓取指定更精确的选择器/片段），"
        f"而不是原样重试。]\n\n"
    )


def clip_text(text: str, max_chars: int, head: int, tail: int) -> tuple[str, dict[str, Any]]:
    """按「头 + 尾」截断文本，返回 (新文本, 账目)。"""
    original = len(text)
    if max_chars <= 0 or original <= max_chars:
        return text, {"truncated": False, "original_chars": original,
                      "kept_chars": original, "dropped_chars": 0}
    head = max(0, min(head, max_chars))
    tail = max(0, min(tail, max_chars - head))
    if head + tail <= 0:                   # 配置被设成 0/0 时至少留头部
        head = max_chars
    kept = head + tail
    marker = truncation_marker(original, kept)
    body = text[:head] + marker + (text[-tail:] if tail else "")
    return body, {
        "truncated": True, "original_chars": original,
        "kept_chars": len(body), "dropped_chars": original - kept,
    }


def limited_tool_content(content: Any, limits: dict[str, int] | None = None,
                         tool: str = "") -> tuple[str, dict[str, Any]]:
    """把任意工具输出转成"可安全进上下文"的字符串，并返回截断账目。

    账目字段：``truncated`` / ``original_chars`` / ``kept_chars`` /
    ``dropped_chars`` / ``tool`` / ``est_tokens_saved``。
    """
    if isinstance(content, dict):
        import json
        try:
            text = json.dumps(content, indent=2, ensure_ascii=False)
        except Exception:
            text = str(content)
    else:
        text = str(content)

    lim = limits or limits_from_config()
    max_chars = max(0, min(int(lim["max_chars"]), HARD_MAX_CHARS))
    # token 上限再夹一次（配置里两者语义不同：一个是字符、一个是估算 token）
    est = int(lim.get("max_tokens", 0) * CHARS_PER_TOKEN)
    if est > 0:
        max_chars = min(max_chars, est) if max_chars > 0 else est
    out, stat = clip_text(text, max_chars, int(lim["head"]), int(lim["tail"]))
    stat["tool"] = tool
    stat["est_tokens_saved"] = int(stat["dropped_chars"] / CHARS_PER_TOKEN)
    return out, stat
