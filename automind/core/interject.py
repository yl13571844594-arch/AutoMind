"""中途插话（interjection）—— 生成过程中用户补的那句话，如何被"接住"。

**要解决的问题**：AI 正在写一个长回答，用户看到一半发现"它还少考虑了一件事"。
在此之前界面只有两条路：等它写完再说一句（那一轮已经跑偏了），或者点停止
（把已经生成的内容全部丢掉）。用户真正想要的第三种：**补一句，然后它带着
这句话继续**。而当时的实现里，任务执行期间输入框是禁用的 —— 用户想补话，
第一件要做的事竟然是"先停下"。

这个模块只放"插话"这件事本身需要的三样东西，不掺业务逻辑：

  · :class:`Interjection` —— 一条插话（有编号、有"是否真的交给了模型"的痕迹）；
  · :class:`InterjectionQueue` —— 收话的地方。**收下 ≠ 生效**，两件事分开记账，
    这样"没来得及纳入本轮"才可能被如实报出来，而不是悄悄吞掉；
  · :func:`render_for_model` —— 把插话渲染成给模型看的一段话。措辞很关键：
    必须说清"这是补充、不要重头再来"，否则模型会把补充当成新任务重新开始，
    用户看到的就是"回答写了一半突然重启"。

队列的并发性：``collections.deque`` 的 ``append``/``popleft`` 在 CPython 里是
原子的，生产者（WebSocket 事件循环）与消费者（任务协程）不需要额外的锁 ——
而插话恰恰是**跨任务**发生的（收话的是 WS 循环，用话的是执行协程）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: 单条插话的长度上限（够写一段说明；再长就该作为新任务发，而不是插话）
MAX_TEXT_CHARS = 4000

#: 一轮任务最多接住几条 —— 防止有人拿插话当输入通道刷满上下文
MAX_PENDING = 8

#: 一轮回答里最多为插话续写几次（每次续写 = 一次额外的 LLM 调用）
MAX_ROUNDS = 3


class InterjectionTooLong(ValueError):
    """插话超长/超量 —— 收下它只会把上下文挤爆，不如当场说清楚。"""


@dataclass
class Interjection:
    """一条用户中途补充。"""

    seq: int
    text: str
    at: float = field(default_factory=time.monotonic)
    #: 模型是否**真的读到了**它（收下只是排队，不等于生效）
    applied: bool = False
    #: 在哪一步交给模型的（chat_round / react_step / plan_step）
    applied_at: str = ""

    def mark_applied(self, where: str) -> None:
        self.applied = True
        self.applied_at = where

    def to_event(self) -> dict[str, Any]:
        """推给前端的事件字段（五处事件共用同一份字段，免得漂移）。"""
        return {"seq": self.seq, "text": self.text, "applied_at": self.applied_at}


class InterjectionQueue:
    """收话的队列 —— "收下"与"生效"分开记账。"""

    def __init__(self, *, max_pending: int = MAX_PENDING,
                 max_chars: int = MAX_TEXT_CHARS) -> None:
        self._items: deque[Interjection] = deque()
        self._seq = 0
        self.max_pending = max_pending
        self.max_chars = max_chars
        #: 本轮一共收下 / 真正交给模型 / 最后没能纳入 的条数（供清单与观测展示）
        self.accepted = 0
        self.applied = 0
        self.dropped = 0

    # ── 生产端（WebSocket 循环）────────────────────────────

    def push(self, text: str) -> Interjection:
        """收下一条插话，返回它；不合格时抛 :class:`InterjectionTooLong`。

        拒绝的理由必须**当场说清**（而不是收下再丢掉）：插话是用户抢在
        生成过程中打出来的一句话，被静默吞掉的话，用户只会以为自己没说。
        """
        body = (text or "").strip()
        if not body:
            raise InterjectionTooLong("补充内容为空")
        if len(body) > self.max_chars:
            raise InterjectionTooLong(
                f"补充内容太长（{len(body)} 字符，上限 {self.max_chars}）——"
                f"请精简后重发，或等本轮结束后作为新任务发送")
        if len(self._items) >= self.max_pending:
            raise InterjectionTooLong(
                f"本轮待处理的补充已有 {len(self._items)} 条（上限 {self.max_pending}）——"
                f"请等它们被纳入后再补")
        self._seq += 1
        item = Interjection(seq=self._seq, text=body)
        self._items.append(item)
        self.accepted += 1
        return item

    # ── 消费端（任务协程）──────────────────────────────────

    def drain(self) -> list[Interjection]:
        """取走当前所有待处理插话（不清账目，便于事后如实汇报）。"""
        out: list[Interjection] = []
        while self._items:
            out.append(self._items.popleft())
        return out

    def pending(self) -> int:
        return len(self._items)

    def report(self) -> dict[str, int]:
        return {"accepted": self.accepted, "applied": self.applied,
                "dropped": self.dropped, "pending": len(self._items)}


def render_for_model(items: list[Interjection], *, continuing: bool = False) -> str:
    """把插话渲染成一条**给模型看**的用户消息。

    措辞是有意的，三件事必须同时说清：

    1. 这是**补充**，不是新任务 —— 否则模型会把正在做的事丢下重新开始；
    2. 它要**并入当前这件事** —— 用户补话的目的就是修正当前输出；
    3. 已经写出去的部分**不要重写** —— 界面上的答案是连续的一整段，
       重写会让用户看到同一段内容出现两遍。
    """
    lines = [
        "【用户在你执行过程中补充了以下内容，请把它并入当前正在处理的这件事】",
    ]
    for it in items:
        lines.append(f"  {it.seq}. {it.text}")
    lines.append(
        "要求：这是对**当前任务/当前回答**的补充或修正，**不是新任务** ——"
        "不要重新开始、不要复述已经给出的内容；"
        + ("接着上面已经写到的位置继续，把上述补充一并考虑进去。"
           if continuing else "把上述补充一并考虑进去。")
    )
    return "\n".join(lines)


def mark_applied(items: list[Interjection], where: str) -> None:
    for it in items:
        it.mark_applied(where)
