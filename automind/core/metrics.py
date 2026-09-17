"""指标注册表 —— 给运维一个能接进 Prometheus 的出口（v1.7.3）。

## 为什么要有这个

此前平台的"可观测性"只有三样：JSONL 轨迹文件、`/api/observe/*` 的界面数据、
以及一个**恒返回 ok** 的 `/api/health`。运维要接 Grafana / Alertmanager 时，
面对的是一堆只能人看的 JSON —— "今天失败率多少""P95 耗时多少""审批平均等多久"
全都得自己写脚本扒日志。

## 设计取舍

* **不引入任何依赖**：本仓的核心依赖只有 4 个，为了一个 `/metrics` 拉进
  `prometheus_client` 不划算。Prometheus 的文本暴露格式本身很简单。
* **内存有界**：耗时用**固定桶**直方图（不是保留全部样本）。一个跑几个月的
  实例不该因为"记录了每次耗时"而吃掉几百 MB —— 那种指标最后会变成事故本身。
* **线程/协程安全**：计数器与直方图都有锁。任务在事件循环里，工具却可能在线程
  池里跑（`run_blocking`），两边都会打点。
* **只记数字，不记内容**：标签里不许放用户数据（任务原文、路径、提示词）——
  指标是长期存储且常被多人查看，泄一次就是一次数据事故。这条是硬约束，
  由 `_sanitize_label` 兜底：超长或含可疑内容的标签值会被截断并替换。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

#: 延迟桶（秒）—— 覆盖"秒级工具调用"到"小时级长任务"
DEFAULT_BUCKETS: tuple[float, ...] = (0.1, 0.5, 1, 5, 15, 60, 300, 900, 3600)

#: 标签值上限（防止有人把任务原文当标签塞进来）
_MAX_LABEL_LEN = 64


def _sanitize_label(value: Any) -> str:
    """标签值只留短标识符 —— 指标会被长期保存并被多人查看，不该成为数据出口。"""
    text = str(value)
    if len(text) > _MAX_LABEL_LEN:
        text = text[:_MAX_LABEL_LEN] + "~"
    return text.replace("\\", "_").replace('"', "_").replace("\n", "_")


class Metrics:
    """进程内指标注册表（计数器 / 直方图 / 仪表）。"""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple, float] = defaultdict(float)
        self._gauges: dict[tuple, float] = {}
        self._buckets = tuple(sorted(buckets))
        #: key -> [每桶计数…, +Inf 计数, 观测次数, 总和]
        self._hist: dict[tuple, list[float]] = {}
        self._help: dict[str, str] = {}
        self._types: dict[str, str] = {}
        self._start = time.monotonic()

    # ── 打点 ───────────────────────────────────────────────

    def _key(self, name: str, labels: dict[str, Any] | None) -> tuple:
        if not labels:
            return (name, ())
        return (name, tuple(sorted((k, _sanitize_label(v)) for k, v in labels.items())))

    def describe(self, name: str, help_text: str, kind: str = "counter") -> None:
        """登记指标的语义（只影响 `/metrics` 的 HELP/TYPE 注释）。"""
        self._help.setdefault(name, help_text)
        self._types.setdefault(name, kind)

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        """记录一次观测（用于耗时/体积这类分布）。

        桶是**非累积**的：一次观测只落进它命中的第一个桶（超出所有桶则记进
        ``+Inf`` 溢出位）。累积是在渲染时算的 —— 这是 Prometheus 直方图的
        标准口径，也让 ``snapshot()`` 里的每个桶就是"落在这一档的次数"。
        （第一版写成"每个命中的桶都 +1"，渲染时又累加一遍，结果是双倍计数；
        由 tests/core/test_metrics_and_readiness.py 的桶计数断言抓出来。）
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        key = self._key(name, labels)
        with self._lock:
            row = self._hist.get(key)
            if row is None:
                row = [0.0] * (len(self._buckets) + 3)
                self._hist[key] = row
            for i, upper in enumerate(self._buckets):
                if v <= upper:
                    row[i] += 1
                    break
            else:
                row[len(self._buckets)] += 1      # 超出所有桶 → +Inf 溢出位
            row[len(self._buckets) + 1] += 1      # count
            row[len(self._buckets) + 2] += v      # sum

    def counter_value(self, name: str, **labels: Any) -> float:
        with self._lock:
            return self._counters.get(self._key(name, labels), 0.0)

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start

    # ── 渲染 ───────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """结构化快照（供测试与 `/api/status` 使用，避免只能靠解析文本断言）。"""
        with self._lock:
            return {
                "counters": {k: v for k, v in self._counters.items()},
                "gauges": {k: v for k, v in self._gauges.items()},
                "histograms": {k: list(v) for k, v in self._hist.items()},
                "uptime_seconds": round(self.uptime_seconds(), 3),
            }

    @staticmethod
    def _fmt_labels(labels: tuple) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in labels)
        return "{" + inner + "}"

    def render(self) -> str:
        """Prometheus 文本暴露格式（0.0.4）。"""
        lines: list[str] = []
        seen_type: set[str] = set()
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hists = {k: list(v) for k, v in self._hist.items()}

        for name, kind in sorted(self._types.items()):
            if name in seen_type:
                continue
            seen_type.add(name)
            if name in self._help:
                lines.append(f"# HELP {name} {self._help[name]}")
            lines.append(f"# TYPE {name} {kind}")

        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{self._fmt_labels(labels)} {_num(value)}")
        for (name, labels), value in sorted(gauges.items()):
            lines.append(f"{name}{self._fmt_labels(labels)} {_num(value)}")
        for (name, labels), row in sorted(hists.items()):
            cumulative = 0.0
            for i, upper in enumerate(self._buckets):
                cumulative += row[i]
                bucket_labels = labels + (("le", _num(upper)),)
                lines.append(f"{name}_bucket{self._fmt_labels(bucket_labels)} {_num(cumulative)}")
            cumulative += row[len(self._buckets)]
            lines.append(f"{name}_bucket{self._fmt_labels(labels + (('le', '+Inf'),))} "
                         f"{_num(cumulative)}")
            lines.append(f"{name}_count{self._fmt_labels(labels)} {_num(row[len(self._buckets) + 1])}")
            lines.append(f"{name}_sum{self._fmt_labels(labels)} {_num(row[len(self._buckets) + 2])}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空（测试用；正常运行不该调用）。"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()


def _num(value: float) -> str:
    """整数就别显示成 1.0（Prometheus 能接受，但人读起来别扭）。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


#: 进程级单例
METRICS = Metrics()

# ── 指标语义登记（集中一处，避免散落在打点处各写一遍文案）──
METRICS.describe("automind_info", "版本与版本档位（值恒为 1）", "gauge")
METRICS.describe("automind_uptime_seconds", "进程已运行秒数", "gauge")
METRICS.describe("automind_running_tasks", "当前在跑的任务数", "gauge")
METRICS.describe("automind_approval_waiting", "正在等待人工审批的任务数", "gauge")
METRICS.describe("automind_tools", "已注册工具数", "gauge")
METRICS.describe("automind_tool_group_failures", "注册失败的工具组数", "gauge")
METRICS.describe("automind_quota_tasks_used", "今日已用任务额度", "gauge")
METRICS.describe("automind_tasks_total", "任务终态计数（按状态）", "counter")
METRICS.describe("automind_task_duration_seconds", "任务耗时分布", "histogram")
METRICS.describe("automind_tokens_total", "累计 token 用量（按种类）", "counter")
METRICS.describe("automind_approvals_total", "审批结果计数", "counter")
METRICS.describe("automind_tool_calls_total", "工具调用计数（按工具与结果）", "counter")
METRICS.describe("automind_tool_errors_total", "工具失败事件计数（按工具）", "counter")
METRICS.describe("automind_budget_events_total", "预算事件计数（预警/超额）", "counter")
METRICS.describe("automind_tokens_prompt", "当前会话累计输入 token", "gauge")
METRICS.describe("automind_tokens_completion", "当前会话累计输出 token", "gauge")
METRICS.describe("automind_interjections_pending", "待处理的中途插话条数", "gauge")
METRICS.describe("automind_quota_tasks_limit", "当日任务额度上限（不限时不上报）", "gauge")
METRICS.describe("automind_interjections_total", "中途插话计数（收下/纳入/未纳入）", "counter")
METRICS.describe("automind_webhook_deliveries_total", "Webhook 投递计数（按结果）", "counter")
METRICS.describe("automind_workflow_runs_total", "工作流运行计数（按终态）", "counter")
