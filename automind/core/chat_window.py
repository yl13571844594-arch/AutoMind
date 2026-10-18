"""对话历史窗口 — 在"上下文不能无限长"与"前缀缓存要能命中"之间取平衡。

背景（v1.11.10）
----------------
对话模式此前每轮都发 ``[system, *hist[-20:]]``。历史一旦超过 20 条，
**每一轮都会从最前面丢掉 1 条** —— 而 DeepSeek / GLM 的自动前缀缓存是
按"逐字节前缀"命中的：开头一变，后面再长也全部作废。表现就是连续追问
十几轮之后，每一轮的输入 token 都按**未命中**计费，命中率接近 0。

办法
----
不再逐条滑窗，改成"格点裁剪"：窗口起点按 ``TRIM`` 的整数倍对齐，

    start = TRIM × ceil((len(hist) − MAX) / TRIM)

起点在一小段时间内保持不变、只在窗口要超限时才一次前跳，于是
"连续几轮共享同一前缀"，大约每 3 轮才发生一次前缀变化，而窗口始终 ≤ MAX。

为什么放在 core 而不是 agent 里
-------------------------------
服务端 ``_kb_already_visible`` 也要判断"这段知识库片段模型到底看不看得见"，
它必须和 Agent 真正发出去的窗口**用同一份逻辑** —— 各写一份必然会漂移，
表现是"以为模型还看得见、其实早被挤出去了"，于是漏注入。
"""

from __future__ import annotations

import math
from typing import Any

#: 一次请求最多带多少条历史消息（与 v1.11.9 之前保持一致，避免上下文变长）
CHAT_HISTORY_MAX = 20
#: 窗口前跳的粒度 —— 一次跳太少会频繁改前缀，跳太多会丢太多上下文
CHAT_HISTORY_TRIM = 6


def chat_window_start(total: int) -> int:
    """按"格点"算窗口起点：起点在若干轮内不变，窗口长度始终 ≤ MAX。

    例子（每轮 +2 条消息）::

        total=21 → start=6   (窗口 15)
        total=23 → start=6   (窗口 17)   ← 与上一轮共享前缀
        total=25 → start=6   (窗口 19)   ← 仍共享前缀
        total=27 → start=12  (窗口 15)   ← 一次前跳，此后又是三轮共享
    """
    if total <= CHAT_HISTORY_MAX:
        return 0
    overflow = total - CHAT_HISTORY_MAX
    return CHAT_HISTORY_TRIM * math.ceil(overflow / CHAT_HISTORY_TRIM)


def chat_history_window(hist: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """返回本轮真正要发给模型的历史消息（不含 system 提示词）。

    * 窗口长度始终 ≤ ``CHAT_HISTORY_MAX``；
    * 起点尽量保持不变，连续几轮的前缀相同（可命中提示缓存）；
    * 裁剪后若首条是 ``assistant`` / ``tool``，向后跳直到 user —— 多数
      OpenAI 兼容服务不接受对话以助手发言开头；但**开头的 system 消息要保留**
      （知识库注入就挂在历史里，丢了它会让去重判定误以为片段已经不在场）。
    """
    if not hist:
        return []
    start = chat_window_start(len(hist))
    window = list(hist[start:])
    while window and window[0].get("role") not in ("user", "system"):
        window.pop(0)
    if not window:                       # 极端：全被裁掉 → 退回最后一条
        window = list(hist[-1:])
    return window
