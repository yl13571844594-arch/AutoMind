"""cron 表达式 —— 解析、算下一次触发时间、算最小间隔（v1.9.0，R-201）。

为什么自己写而不引依赖
--------------------
工作流的定时触发器要在**加载期**回答两个问题：这条表达式写对了没有、两次触发
最近会隔多久（专业版下限 300 秒，见 ``quota.workflow_min_interval``）。这两件事
校验器必须能离线做（CI 里也要能跑），而 croniter 之类的库不是核心依赖 ——
为五个字段多拖一个依赖、再让"校验结果随依赖版本变化"，不划算。

支持的写法（标准 5 段：分 时 日 月 周）::

    *            任意
    5            单值
    1-5          区间
    */15  1-30/5 步长
    1,15,30      列表（上面几种可混用）
    JAN-DEC      月份名（大小写不敏感）
    SUN-SAT      星期名；数字 0 与 7 都表示周日
    @hourly @daily @midnight @weekly @monthly @yearly @annually

日与周同时受限时按 cron 的传统语义取**并集**（任一满足即触发）——
"每月 1 号和每周一"写成 ``0 9 1 * 1``，这是 Vixie cron 的行为，也是多数人的预期。

时区：按**服务进程的本地时间**。不支持秒字段、``L`` / ``W`` / ``#`` 等扩展 ——
写了就明确报错，绝不"认个大概"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: 五个字段的名称与取值范围（含端点）
_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("分钟", 0, 59),
    ("小时", 0, 23),
    ("日", 1, 31),
    ("月", 1, 12),
    ("星期", 0, 7),
)

_MONTHS = {name: i for i, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_WEEKDAYS = {name: i for i, name in enumerate(
    ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: 往后找下一次触发时最多看多少分钟（5 年）。cron 表达式合法但永远不触发的
#: 只有 "2 月 30 号" 这类 —— 那种情况要明确报出来，而不是死循环。
_SEARCH_LIMIT_MINUTES = 5 * 366 * 24 * 60

#: 算最小间隔时取样的起点：固定日期 → 同一表达式永远得出同一个结论
#: （校验结果不能"今天过、明天不过"）。2024-01-01 是周一、且 2024 是闰年。
_SAMPLE_START = datetime(2024, 1, 1, 0, 0)
#: 取样窗口：覆盖完整的一年 —— 月级、周级表达式的最短间隔都在里面
_SAMPLE_SPAN = timedelta(days=366)
#: 取样最多触发多少次就停（每分钟触发的表达式不需要数满一年）
_SAMPLE_MAX_FIRES = 4000


class CronError(ValueError):
    """cron 表达式不合法。``message`` 是可直接给人看的中文说明。"""


@dataclass(frozen=True)
class CronExpr:
    """一条解析好的 cron 表达式。"""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]          # 0 = 周日（7 已折算成 0）
    #: 日 / 周两个字段是否写成了 `*`（决定两者是"与"还是"或"）
    days_any: bool
    weekdays_any: bool

    # ── 解析 ──────────────────────────────────────────

    @classmethod
    def parse(cls, text: str) -> CronExpr:
        raw = str(text or "").strip()
        if not raw:
            raise CronError("cron 表达式为空")
        expanded = _MACROS.get(raw.lower(), raw)
        if expanded.startswith("@"):
            raise CronError(f"不认识的 cron 宏：{raw!r}。可选：{'、'.join(_MACROS)}")
        parts = expanded.split()
        if len(parts) != 5:
            hint = ("（检测到 6 段：本实现不支持秒字段，请去掉第一段）"
                    if len(parts) == 6 else "")
            raise CronError(f"cron 表达式应当正好有 5 段（分 时 日 月 周），"
                            f"实际是 {len(parts)} 段：{raw!r}{hint}")
        sets = [_parse_field(part, *spec) for part, spec in zip(parts, _FIELDS, strict=True)]
        weekdays = frozenset(0 if d == 7 else d for d in sets[4])
        expr = cls(text=raw, minutes=frozenset(sets[0]), hours=frozenset(sets[1]),
                   days=frozenset(sets[2]), months=frozenset(sets[3]), weekdays=weekdays,
                   days_any=parts[2] == "*", weekdays_any=parts[4] == "*")
        if expr.next_after(_SAMPLE_START) is None:
            raise CronError(f"cron 表达式 {raw!r} 永远不会触发（例如 2 月 30 号）")
        return expr

    # ── 匹配与推算 ────────────────────────────────────

    def _day_matches(self, moment: datetime) -> bool:
        dom = moment.day in self.days
        dow = (moment.isoweekday() % 7) in self.weekdays
        if self.days_any and self.weekdays_any:
            return True
        if self.days_any:
            return dow
        if self.weekdays_any:
            return dom
        return dom or dow                       # 两者都受限：并集（Vixie cron 语义）

    def matches(self, moment: datetime) -> bool:
        return (moment.minute in self.minutes and moment.hour in self.hours
                and moment.month in self.months and self._day_matches(moment))

    def next_after(self, moment: datetime) -> datetime | None:
        """严格晚于 ``moment`` 的下一次触发时间（精确到分钟）；永不触发返回 None。

        逐级跳：月不对跳到下个月初、日不对跳到次日零点、小时不对跳到下个整点，
        只有前面都对上了才逐分钟走 —— 最坏也只是几千次比较。
        """
        cur = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = cur + timedelta(minutes=_SEARCH_LIMIT_MINUTES)
        while cur <= limit:
            if cur.month not in self.months:
                year = cur.year + (1 if cur.month == 12 else 0)
                month = 1 if cur.month == 12 else cur.month + 1
                cur = cur.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_matches(cur):
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cur.hour not in self.hours:
                cur = (cur + timedelta(hours=1)).replace(minute=0)
                continue
            if cur.minute not in self.minutes:
                later = [m for m in self.minutes if m > cur.minute]
                cur = (cur.replace(minute=min(later)) if later
                       else (cur + timedelta(hours=1)).replace(minute=0))
                continue
            return cur
        return None

    def min_interval_seconds(self) -> int:
        """相邻两次触发的最短间隔（秒）—— 校验"定时最小间隔"用。

        从固定起点取样一整年：日 / 周 / 月级表达式的最短间隔都会落在里面；
        每分钟级的表达式几次就能确定，取样到上限即停。
        """
        best: int | None = None
        prev = self.next_after(_SAMPLE_START - timedelta(minutes=1))
        end = _SAMPLE_START + _SAMPLE_SPAN
        fires = 0
        while prev is not None and prev < end and fires < _SAMPLE_MAX_FIRES:
            nxt = self.next_after(prev)
            if nxt is None:
                break
            gap = int((nxt - prev).total_seconds())
            best = gap if best is None else min(best, gap)
            if best <= 60:                        # 不可能更短了
                break
            prev = nxt
            fires += 1
        # 一年只触发一次的表达式（@yearly）取样里只有一个间隔，或没有：按一年算
        return best if best is not None else int(_SAMPLE_SPAN.total_seconds())

    def describe(self) -> str:
        return self.text


def _parse_field(part: str, name: str, lo: int, hi: int) -> set[int]:
    """解析一个字段（含列表、区间、步长、名称）。"""
    values: set[int] = set()
    for item in part.split(","):
        if not item:
            raise CronError(f"{name}字段 {part!r} 里有空项（多了一个逗号？）")
        base, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"{name}字段的步长应当是正整数：{item!r}")
            step = int(step_text)
        if base == "*":
            start, stop = lo, hi
            if name == "星期":
                stop = 6                         # `*` 不重复数周日（7）
        elif "-" in base:
            a, _, b = base.partition("-")
            start, stop = _value(a, name, lo, hi), _value(b, name, lo, hi)
            if start > stop:
                raise CronError(f"{name}字段的区间起点大于终点：{item!r}")
        else:
            start = _value(base, name, lo, hi)
            # `5/15` = 从 5 起每 15 一次（到上限）；裸 `5` 就是 5
            stop = hi if step_text else start
        values.update(range(start, stop + 1, step))
    return values


def _value(token: str, name: str, lo: int, hi: int) -> int:
    upper = token.strip().upper()
    if name == "月" and upper in _MONTHS:
        return _MONTHS[upper]
    if name == "星期" and upper in _WEEKDAYS:
        return _WEEKDAYS[upper]
    if not upper.isdigit():
        if upper in ("L", "W", "?") or "#" in upper or upper.endswith(("L", "W")):
            raise CronError(f"{name}字段不支持 {token!r} 这类扩展写法（L / W / # / ?）")
        raise CronError(f"{name}字段里有无法识别的值：{token!r}")
    value = int(upper)
    if not lo <= value <= hi:
        raise CronError(f"{name}字段的值 {value} 超出范围 {lo}–{hi}")
    return value


def format_interval(seconds: int) -> str:
    """把秒数写成人话（报错文案用）。"""
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


__all__ = ["CronError", "CronExpr", "format_interval"]
