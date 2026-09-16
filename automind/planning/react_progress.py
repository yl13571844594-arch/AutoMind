"""ReAct 无进展 / 重复动作治理 —— 让"原地打转"在烧光预算之前被叫停。

## 问题

此前 ReAct 路径只有一道熔断：**同一工具连续失败 3 次**（``FAILURE_THRESHOLD``）。
它挡的是"坏工具被反复调用"，挡不住**成功但毫无进展**的动作 —— 而这才是
卡壳最常见的形态：

    file_read("app.py")      → 成功，第 1 次
    file_read("app.py")      → 成功，第 2 次（一模一样的参数）
    file_read("app.py")      → 成功，第 3 次 …… 一直烧到 max_iterations=50

不报错、不熔断、不提醒，用户最后只拿到一份「部分交付清单」。代价还特别高：
ReAct 每一步都要重发工具 schema + 全量消息，转一圈就是几万 token。

## 做法

1. **动作指纹**：对每次工具调用算一个稳定指纹（工具名 + 规范化参数）。
   连续出现 **完全相同** 的指纹即视为"没进展"。
2. **先提示、后拦截**：连续第 ``threshold`` 次先**放行**但在结果里追加一段
   具体的纠偏指引（"你已经第 N 次读同一个文件了，请直接基于已有内容作答，
   或改用 offset/limit 读还没看过的部分"）；再下一次就直接**拦截**，
   把指引当成失败结果喂回模型 —— 不再为同一个动作重复付上下文成本。
3. **只读结果复用**：SAFE 级只读工具的相同调用，结果在**本次 run 内**复用。
   Plan 路径早就有 ``_subtask_cache``，ReAct 一直没有；重复读同一个文件的
   结果会被反复塞进上下文，只有体积截断与旧消息折叠在救。
4. **可观测**：拦截次数、复用次数、每个工具的动作次数都进 ``progress_report()``，
   并在 ``partial_report()`` 里以「无进展」呈现 —— 用户能看到"卡在哪一步、
   哪个动作被反复执行"。

## 边界（不做什么）

* 只拦**完全相同**的动作。参数不同 = 有可能是真进展（分段读文件、逐条处理），
  一律放行 —— 误杀真进展比漏掉一次重复更糟。
* UUID / 时间戳这类"每次都不一样的参数"会天然让指纹不同，因此不会被误拦。
* 拦截产生的结果用的是**本地判定**，不消耗任何 token。
"""

from __future__ import annotations

import json
from typing import Any

#: 只读工具（同参调用可安全复用结果）。用工具自身的权限等级判定为主，
#: 这里是"等级被误标成 SAFE 但实际有副作用"的兜底排除名单。
SIDE_EFFECT_TOOLS = frozenset({
    "notify",            # 弹通知（用户可见的副作用）
    "screenshot_tool",   # 落盘截图
    "clipboard_tool",    # 覆盖剪贴板
    "calendar",          # 可写日程
    "terminal",          # 命令可能有副作用，绝不缓存
    "python_sandbox",    # 执行任意代码
    "process_tool",      # 可杀进程
    "git_tool",          # 可提交/推送
})

#: 工具 → 「换个招数」的具体建议。空手说"请换个方法"没有用，模型会再试一遍。
_ALTERNATIVES: dict[str, str] = {
    "file_read": "文件内容**已经在上下文里**了，直接基于它继续；确实要更多内容就"
                 "改用 offset/limit 读尚未看过的区间，或用 file_search 直接定位关键片段",
    "file_search": "换更具体的关键词/glob，或改用 terminal 做 grep/find",
    "terminal": "换一条命令（改参数或改过滤条件），或改用 python_sandbox 处理",
    "python_sandbox": "改脚本逻辑或输入，别重复跑同一段代码",
    "web_search": "换关键词，或改用 web_fetch 直接抓一个已知 URL",
    "web_fetch": "换 URL，或先 web_search 找到正确地址再抓",
    "file_write": "内容已经写过一次了；需要改动请用 file_edit 做增量修改",
    "file_edit": "先 file_read 确认当前内容（可能已被上一次编辑改变），再编辑",
    "file_multi_edit": "先 file_read 确认当前内容，再重新构造编辑列表",
    "db_query": "改 SQL 或加 LIMIT 缩小结果集",
    "http_request": "改 URL/方法/请求体，或先确认接口返回了什么",
    "browser": "改选择器或步骤；同一个页面重复打开不会得到新信息",
    "excel_tool": "换动作（如 read 改 inspect）或换 sheet/range",
    "word_tool": "换动作或换文件",
    "pdf_tool": "换页码区间，或改用 ocr_tool 处理扫描件",
}


def action_key(name: str, arguments: Any) -> str:
    """动作指纹：工具名 + 规范化参数。

    参数顺序不影响指纹（``sort_keys``）；不可 JSON 化的值降级为 ``repr``，
    再不行就退化成"工具名 + 类型"，宁可少拦一次也不抛异常。
    """
    try:
        blob = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False,
                          default=str)
    except Exception:
        try:
            blob = repr(sorted((arguments or {}).items()))
        except Exception:
            blob = f"<{type(arguments).__name__}>"
    return f"{name}::{blob}"


def looks_like_a_different_path(paths: str) -> bool:
    """参数里是否出现"每次都会变"的标记（UUID / 时间戳）——用于排除误判。

    仅作提示用，真正的判定仍然是"指纹完全相同"。
    """
    import re
    return bool(re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}|"
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|"
        r"\b\d{10}\b|\b\d{13}\b", paths))


def alternative_hint(name: str) -> str:
    """给模型的具体替代建议。"""
    return _ALTERNATIVES.get(
        name, "换一个工具或换一组参数，或直接基于已有信息给出结论")


class ReactProgressGuard:
    """ReAct 的"进展"记账器：重复动作拦截 + 只读结果复用。

    生命周期与一次 ``run()`` 绑定（``reset()`` 在每个 run 开始时调用）。
    """

    def __init__(self, threshold: int = 2, *, cache_enabled: bool = True) -> None:
        self.threshold = max(1, int(threshold))
        self.cache_enabled = bool(cache_enabled)
        self.reset()

    def reset(self) -> None:
        self._last_key = ""
        self._streak = 0
        self._counts: dict[str, int] = {}
        self._cache: dict[str, Any] = {}
        self.repeats = 0            # 重复动作出现次数（含被放行的）
        self.guided = 0             # 附带纠偏提示放行的次数
        self.blocked = 0            # 直接拦截（未执行）的次数
        self.cache_hits = 0         # 只读结果复用命中次数
        self.blocked_tools: dict[str, int] = {}
        #: 本次 run 里被判定"无进展"的连续长度（用于提前收尾判定）
        self.no_progress_streak = 0

    # ── 判定 ────────────────────────────────────────────────

    def check(self, name: str, arguments: Any) -> tuple[str, str]:
        """动作执行前的判定。

        Returns:
            ``("run", "")``       — 正常执行；
            ``("guide", 提示)``   — 仍执行，但把提示附在结果里；
            ``("block", 提示)``   — 不执行，提示作为结果返回。
        """
        key = action_key(name, arguments)
        self._counts[name] = self._counts.get(name, 0) + 1
        if key == self._last_key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
            self.no_progress_streak = 0
            return "run", ""

        self.repeats += 1
        self.no_progress_streak += 1
        nth = self._streak
        if nth < self.threshold:
            return "run", ""
        if nth == self.threshold:
            self.guided += 1
            return "guide", self._message(name, nth, blocking=False)
        self.blocked += 1
        self.blocked_tools[name] = self.blocked_tools.get(name, 0) + 1
        return "block", self._message(name, nth, blocking=True)

    def _message(self, name: str, nth: int, *, blocking: bool) -> str:
        head = (f"⚠️ 未检测到进展：你已连续 {nth} 次执行**完全相同**的动作"
                f"「{name}」（参数逐字一致）。")
        if blocking:
            head += "本次未执行 —— 重复执行不会带来任何新信息，只会重复消耗上下文。"
        else:
            head += "本次仍然执行了，但请立刻改变做法。"
        return f"{head}\n建议：{alternative_hint(name)}"

    # ── 只读结果复用 ────────────────────────────────────────

    def cached(self, name: str, arguments: Any) -> Any | None:
        """命中只读缓存则返回当时的结果，否则 None。"""
        if not self.cache_enabled:
            return None
        hit = self._cache.get(action_key(name, arguments))
        if hit is not None:
            self.cache_hits += 1
        return hit

    def remember(self, name: str, arguments: Any, result: Any) -> None:
        if self.cache_enabled:
            self._cache.setdefault(action_key(name, arguments), result)

    # ── 报告 ────────────────────────────────────────────────

    def report(self) -> dict[str, Any]:
        return {
            "repeats": self.repeats,
            "guided": self.guided,
            "blocked": self.blocked,
            "cache_hits": self.cache_hits,
            "no_progress_streak": self.no_progress_streak,
            "blocked_tools": dict(self.blocked_tools),
            "tool_calls": dict(self._counts),
            "threshold": self.threshold,
        }
