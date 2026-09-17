"""/metrics 与就绪探针（v1.7.3 第 5 项）。

## 修的是什么

此前平台的"可观测性"只有三样：JSONL 轨迹、界面用的 `/api/observe/*`、
以及一个**恒返回 ok** 的 `/api/health`：

* 运维要接 Prometheus / Grafana，全仓 **0 处** `prometheus`/`otel` 命中 ——
  失败率、P95 耗时、审批等待时长全都得自己写脚本扒日志；
* 编排系统拿 `/api/health` 当就绪探针，于是"数据库打不开""磁盘满了"
  一概显示健康，流量照导，用户拿到的是一个又一个失败任务。

## 这个文件盯住的三件事

1. **指标真的在动**：任务数、耗时分布、工具调用、审批结果、插话、预算事件
   都要能从事件流里折算出来（在 `observability.record()` 这个唯一漏斗上打点）；
2. **就绪探针真的会红**：数据目录/数据库/项目目录/磁盘任一不可用 → 503；
3. **不让监控变成数据出口**：标签值被截断并清洗，不允许把任务原文塞进指标。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from automind.core.metrics import METRICS, Metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


# ═══════════════════════════════════════════════════════════
# 1. 指标注册表本身
# ═══════════════════════════════════════════════════════════


def test_counters_and_gauges_render_in_prometheus_format():
    m = Metrics()
    m.describe("demo_total", "示例计数器", "counter")
    m.inc("demo_total")
    m.inc("demo_total", 2)
    m.gauge("demo_gauge", 7)

    text = m.render()

    assert "# TYPE demo_total counter" in text
    assert "demo_total 3" in text
    assert "demo_gauge 7" in text


def test_histogram_uses_fixed_buckets_and_stays_bounded():
    """耗时用固定桶，不保留样本 —— 长跑实例不该因为记指标而吃内存。"""
    m = Metrics(buckets=(1, 10))
    for v in (0.5, 5, 20, 100):
        m.observe("demo_seconds", v)

    text = m.render()

    assert 'demo_seconds_bucket{le="1"} 1' in text
    assert 'demo_seconds_bucket{le="10"} 2' in text
    assert 'demo_seconds_bucket{le="+Inf"} 4' in text
    assert "demo_seconds_count 4" in text
    assert "demo_seconds_sum 125.5" in text
    # 只有 3 个桶位 + count/sum 的固定开销，与观测次数无关
    assert len(m.snapshot()["histograms"][("demo_seconds", ())]) == 2 + 3


def test_labels_are_sanitized_so_they_cannot_carry_user_data():
    """指标会被长期保存并被多人查看 —— 不许把任务原文当标签塞进来。"""
    m = Metrics()
    m.inc("demo_total", tool="x" * 200)

    key = next(iter(m.snapshot()["counters"]))
    label_value = dict(key[1])["tool"]

    assert len(label_value) <= 65, "超长标签必须被截断"
    assert label_value.endswith("~")


def test_label_values_cannot_break_the_exposition_format():
    m = Metrics()
    m.inc("demo_total", tool='bad"name\nwith\\breaks')

    line = [ln for ln in m.render().splitlines() if ln.startswith("demo_total")][0]

    assert line.count('"') == 2, "标签里的引号必须被清洗，否则整行格式就废了"
    assert "\n" not in line


def test_integer_values_are_rendered_without_a_decimal_point():
    m = Metrics()
    m.gauge("demo_gauge", 12.0)
    assert "demo_gauge 12" in m.render()


def test_reset_clears_everything():
    m = Metrics()
    m.inc("a")
    m.gauge("b", 1)
    m.observe("c", 1)
    m.reset()

    snap = m.snapshot()

    assert not snap["counters"] and not snap["gauges"] and not snap["histograms"]


# ═══════════════════════════════════════════════════════════
# 2. 事件流 → 指标（打点挂在唯一漏斗上）
# ═══════════════════════════════════════════════════════════


def test_task_lifecycle_events_become_metrics():
    from automind.core import observability as obs

    obs.record("s1", {"type": "task_start", "interaction": "chat"})
    obs.record("s1", {"type": "chat_done", "duration_ms": 2500})

    assert METRICS.counter_value("automind_tasks_total", status="started") == 1
    assert METRICS.counter_value("automind_tasks_total", status="completed") == 1
    hist = METRICS.snapshot()["histograms"][("automind_task_duration_seconds", ())]
    assert hist[-1] == pytest.approx(2.5), "耗时要以秒为单位进直方图"


@pytest.mark.parametrize("etype,status", [
    ("task_error", "failed"),
    ("task_cancelled", "cancelled"),
])
def test_failure_and_cancel_are_counted_separately(etype, status):
    from automind.core import observability as obs

    obs.record("s1", {"type": "task_start"})
    obs.record("s1", {"type": etype, "error": "boom"})

    assert METRICS.counter_value("automind_tasks_total", status=status) == 1


def test_tool_calls_are_counted_by_tool_and_result():
    from automind.core import observability as obs

    obs.record("s1", {"type": "task_start"})
    obs.record("s1", {"type": "step_action", "tool": "file_read", "success": True})
    obs.record("s1", {"type": "step_action", "tool": "file_read", "success": False})

    assert METRICS.counter_value("automind_tool_calls_total",
                                 tool="file_read", result="ok") == 1
    assert METRICS.counter_value("automind_tool_calls_total",
                                 tool="file_read", result="fail") == 1


def test_approval_and_interjection_events_are_counted():
    from automind.core import observability as obs

    obs.record("s1", {"type": "task_start"})
    obs.record("s1", {"type": "approval_request", "tool": "terminal"})
    obs.record("s1", {"type": "approval_resolved", "approved": True})
    obs.record("s1", {"type": "approval_timeout", "tool": "terminal"})
    obs.record("s1", {"type": "interjection_received", "seq": 1})
    obs.record("s1", {"type": "interjection_applied", "at": "chat_round"})
    obs.record("s1", {"type": "interjection_dropped", "seq": 2})

    assert METRICS.counter_value("automind_approvals_total", outcome="requested") == 1
    assert METRICS.counter_value("automind_approvals_total", outcome="approved") == 1
    assert METRICS.counter_value("automind_approvals_total", outcome="timeout") == 1
    for state in ("accepted", "applied", "dropped"):
        assert METRICS.counter_value("automind_interjections_total", state=state) == 1


def test_metrics_failure_never_breaks_the_event_pipeline(monkeypatch):
    """监控设施故障绝不能影响任务执行（与轨迹写入同一条原则）。"""
    from automind.core import observability as obs

    def _boom(*_a, **_kw):
        raise RuntimeError("指标后端炸了")

    monkeypatch.setattr(METRICS, "inc", _boom)
    monkeypatch.setattr(obs, "_trace_event", lambda *_a, **_kw: None)

    obs.record("s1", {"type": "task_start"})       # 不应抛出


# ═══════════════════════════════════════════════════════════
# 3. 端点接线
# ═══════════════════════════════════════════════════════════


def test_metrics_endpoint_serves_prometheus_text(monkeypatch):
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)
    client = TestClient(srv.app)
    srv._observability.record("s1", {"type": "task_start"})

    r = client.get("/metrics")

    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "automind_tasks_total" in r.text
    assert "# TYPE automind_tasks_total counter" in r.text


def test_metrics_endpoint_is_protected_when_a_token_is_set(monkeypatch):
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "tok-1", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)
    client = TestClient(srv.app)

    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer tok-1"}).status_code == 200


def test_readiness_probe_reports_each_dependency(monkeypatch):
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)
    client = TestClient(srv.app)

    r = client.get("/api/health/ready")
    body = r.json()

    assert r.status_code in (200, 503)
    for name in ("data_dir", "database", "project_root", "disk", "llm"):
        assert name in body["checks"], f"就绪探针漏了 {name}"
    assert "ready" in body and "failed" in body


def test_readiness_fails_closed_when_a_dependency_is_broken(monkeypatch):
    """关键依赖坏掉时必须 503 —— 这正是旧 /api/health 恒 ok 的问题所在。"""
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)

    def _broken():
        return {"data_dir": {"ok": False, "error": "磁盘只读"},
                "database": {"ok": True}, "project_root": {"ok": True},
                "disk": {"ok": True}, "llm": {"ok": True, "configured": True}}

    monkeypatch.setattr(srv, "_readiness_checks", _broken)
    client = TestClient(srv.app)

    r = client.get("/api/health/ready")

    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert r.json()["failed"] == ["data_dir"]


def test_missing_llm_alone_does_not_fail_readiness(monkeypatch):
    """没配 Key 只是"还不能干活"，不该让编排系统反复重启实例。"""
    import automind.server as srv

    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)

    def _no_llm():
        return {"data_dir": {"ok": True}, "database": {"ok": True},
                "project_root": {"ok": True}, "disk": {"ok": True},
                "llm": {"ok": True, "configured": False, "note": "未配置 Key"}}

    monkeypatch.setattr(srv, "_readiness_checks", _no_llm)
    client = TestClient(srv.app)

    r = client.get("/api/health/ready")

    assert r.status_code == 200
    assert r.json()["checks"]["llm"]["configured"] is False
