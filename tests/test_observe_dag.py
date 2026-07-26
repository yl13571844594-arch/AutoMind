"""观测中心核心（automind.core.observability）— DAG 构建与版本边界。"""

from __future__ import annotations

import pytest

from automind.core import observability as ob


@pytest.fixture(autouse=True)
def _clean():
    ob.reset()
    yield
    ob.reset()


def _run_basic(sid: str = "s1") -> None:
    ob.record(sid, {"type": "task_start", "interaction": "work"})
    ob.record(sid, {"type": "plan_created", "task": "写报告", "root_goal_id": "R", "steps": [
        {"goal_id": "g1", "description": "查资料", "tool": "search", "parent_id": "R"},
        {"goal_id": "g2", "description": "汇总", "tool": None, "parent_id": "R"},
    ]})


def test_no_graph_before_task_start():
    """没有 task_start 的孤立事件不得凭空建图（历史回放场景）。"""
    ob.record("s1", {"type": "plan_step_start", "goal_id": "g1"})
    assert ob.snapshot("s1") is None


def test_plan_builds_dag_by_real_hierarchy():
    """同级叶子必须并列挂在父节点下，而不是串成链 —— 串链会显示出
    并不存在的先后依赖（实测叶子会并行/交错执行）。"""
    _run_basic()
    g = ob.snapshot("s1")
    assert g["task"] == "写报告"
    ids = [n["id"] for n in g["nodes"]]
    assert ids == ["root", "g1", "g2"]
    assert {"f": "root", "t": "g1", "kind": "sub"} in g["edges"]
    assert {"f": "root", "t": "g2", "kind": "sub"} in g["edges"]
    assert not any(e["kind"] == "seq" for e in g["edges"])


def test_nested_plan_parent_edges():
    """多层计划树：子目标挂到自己的父目标下。"""
    ob.record("s1", {"type": "task_start"})
    ob.record("s1", {"type": "plan_created", "root_goal_id": "R", "steps": [
        {"goal_id": "mid", "description": "中间层", "parent_id": "R"},
        {"goal_id": "leaf", "description": "叶子", "parent_id": "mid"},
    ]})
    g = ob.snapshot("s1")
    assert {"f": "root", "t": "mid", "kind": "sub"} in g["edges"]
    assert {"f": "mid", "t": "leaf", "kind": "sub"} in g["edges"]


def test_step_lifecycle_and_action_attach():
    _run_basic()
    ob.record("s1", {"type": "plan_step_start", "goal_id": "g1"})
    g = ob.snapshot("s1")
    assert next(n for n in g["nodes"] if n["id"] == "g1")["status"] == "running"

    # 工具调用挂到当前 running 的步骤下
    ob.record("s1", {"type": "step_action", "tool": "search", "success": True})
    g = ob.snapshot("s1")
    assert {"f": "g1", "t": "act0", "kind": "call"} in g["edges"]

    ob.record("s1", {"type": "plan_step_end", "goal_id": "g1", "success": True})
    g = ob.snapshot("s1")
    node = next(n for n in g["nodes"] if n["id"] == "g1")
    assert node["status"] == "ok" and node["t1"] is not None


def test_action_attributed_by_goal_id():
    """事件自带 goal_id 时按它归属（步骤已结束也能正确归位）。"""
    _run_basic()
    ob.record("s1", {"type": "plan_step_end", "goal_id": "g1", "success": True})
    ob.record("s1", {"type": "step_action", "goal_id": "g1", "tool": "search",
                     "success": True})
    g = ob.snapshot("s1")
    assert {"f": "g1", "t": "act0", "kind": "call"} in g["edges"]


def test_action_outside_step_window_attaches_to_root():
    """步骤窗口外的调用如实挂 root，不硬凑进某个步骤。"""
    _run_basic()
    ob.record("s1", {"type": "step_action", "tool": "terminal", "success": True})
    g = ob.snapshot("s1")
    assert {"f": "root", "t": "act0", "kind": "call"} in g["edges"]


def test_failure_and_backtrack_counters():
    _run_basic()
    ob.record("s1", {"type": "plan_step_end", "goal_id": "g1",
                     "success": False, "error": "boom"})
    ob.record("s1", {"type": "plan_backtrack", "goal_id": "g2", "reason": "重试"})
    g = ob.snapshot("s1")
    assert g["counters"]["failures"] == 1
    assert g["counters"]["backtracks"] == 1
    assert next(n for n in g["nodes"] if n["id"] == "g2")["status"] == "backtrack"


@pytest.mark.parametrize(("event", "status"), [
    ("task_complete", "ok"),
    ("task_error", "fail"),
    ("task_cancelled", "cancelled"),
    ("chat_done", "ok"),   # 对话模式收尾，否则图永远停在 running
])
def test_terminal_events_finalize(event, status):
    _run_basic()
    ob.record("s1", {"type": event})
    g = ob.snapshot("s1")
    assert g["status"] == status
    assert g["finished_at"] is not None


def test_running_step_is_closed_on_terminal_event():
    """任务结束时仍在 running 的步骤必须归位，避免图上永远转圈。"""
    _run_basic()
    ob.record("s1", {"type": "plan_step_start", "goal_id": "g1"})
    ob.record("s1", {"type": "task_cancelled"})
    g = ob.snapshot("s1")
    assert next(n for n in g["nodes"] if n["id"] == "g1")["status"] == "cancelled"


def test_new_task_replaces_graph_community_semantics():
    """社区版只保留当前任务：新 task_start 即替换，不累积历史。"""
    _run_basic()
    first = ob.snapshot("s1")["id"]
    ob.record("s1", {"type": "task_start", "interaction": "work"})
    g = ob.snapshot("s1")
    assert g["id"] != first
    assert len(g["nodes"]) == 1        # 只剩新的 root
    assert g["status"] == "running"


def test_snapshot_is_a_copy():
    """快照必须是深拷贝，调用方改动不得污染内部状态。"""
    _run_basic()
    snap = ob.snapshot("s1")
    snap["nodes"][0]["status"] = "tampered"
    snap["counters"]["steps"] = 999
    fresh = ob.snapshot("s1")
    assert fresh["nodes"][0]["status"] != "tampered"
    assert fresh["counters"]["steps"] == 2


def test_node_cap_protects_memory():
    ob.record("s1", {"type": "task_start"})
    for i in range(ob.MAX_NODES + 50):
        ob.record("s1", {"type": "step_action", "tool": f"t{i}", "success": True})
    g = ob.snapshot("s1")
    assert len(g["nodes"]) <= ob.MAX_NODES
    assert g["counters"]["truncated"] > 0


def test_session_cap_evicts_oldest():
    for i in range(ob.MAX_SESSIONS + 5):
        ob.record(f"s{i}", {"type": "task_start"})
    assert len(ob._graphs) <= ob.MAX_SESSIONS


def test_listener_fires_on_completion_and_isolates_errors():
    seen = []

    def good(g):
        seen.append(g["id"])

    def bad(g):
        raise RuntimeError("商业侧异常不得影响核心")

    ob.add_listener(bad)
    ob.add_listener(good)
    try:
        _run_basic()
        ob.record("s1", {"type": "task_complete"})
        assert len(seen) == 1
    finally:
        ob.remove_listener(bad)
        ob.remove_listener(good)


def test_unplanned_step_is_attached():
    """重规划产生的计划外步骤也要进图，而不是被丢弃。"""
    _run_basic()
    ob.record("s1", {"type": "plan_step_start", "goal_id": "gX",
                     "description": "临时补充", "tool": "shell"})
    g = ob.snapshot("s1")
    assert any(n["id"] == "gX" for n in g["nodes"])
    assert {"f": "root", "t": "gX", "kind": "sub"} in g["edges"]
