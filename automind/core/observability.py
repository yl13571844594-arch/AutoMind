"""观测中心核心 — 由任务事件流实时构建执行 DAG（社区版基础能力）。

职责边界（与商业版的分工）：
    - **社区版**：本模块只保留「当前任务」的实时 DAG（每个会话一张图，
      新任务开始即替换），只读、不落盘、不聚合历史 —— 够用于「看清这次
      任务在做什么」，且零额外存储成本；
    - **专业版/企业版**：``automind_pro`` 通过 :func:`add_listener` 订阅
      已完成的图快照，自行做历史留存、实时看板聚合与导出。核心不含任何
      商业逻辑，未安装商业包时监听器列表为空，行为完全不变。

数据来源是 agent 已有的事件流（``plan_created`` / ``plan_step_start`` /
``plan_step_end`` / ``plan_backtrack`` / ``step_action`` / ``task_*``），
不需要 agent 侧改动，也不增加任何 LLM 调用。

线程模型：服务端事件在单一事件循环内串行投递，故不加锁；仅对节点数量
设上限，避免超长任务把内存撑爆。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

#: 单张图的节点上限（超出后仅计数不再新增节点，保护内存）
MAX_NODES = 400

#: 已完成图的监听器（商业版订阅；社区版恒为空）
_listeners: list[Callable[[dict], None]] = []

#: 会话 -> 当前任务图（社区版只留当前一张）
_graphs: dict[str, dict[str, Any]] = {}

#: 会话数上限：防止大量一次性 session_id 堆积（企业版多用户场景）
MAX_SESSIONS = 64

#: 运行序号（生成 run id）
_seq: dict[str, int] = {"n": 0}


def _now_ms() -> int:
    return int(time.time() * 1000)


def add_listener(fn: Callable[[dict], None]) -> None:
    """订阅「任务图完成」事件（商业版历史/看板用）。"""
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn: Callable[[dict], None]) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _new_graph(session_id: str, interaction: str = "") -> dict[str, Any]:
    _seq["n"] += 1
    return {
        "id": f"run{_seq['n']}-{_now_ms()}",
        "session_id": session_id,
        "interaction": interaction,
        "task": "",
        "status": "running",
        "started_at": _now_ms(),
        "finished_at": None,
        "nodes": [{"id": "root", "kind": "task", "label": "任务", "status": "running",
                   "t0": _now_ms(), "t1": None, "tool": None, "error": ""}],
        "edges": [],
        "counters": {"steps": 0, "actions": 0, "backtracks": 0, "failures": 0,
                     "truncated": 0},
    }


def _find(graph: dict, node_id: str) -> dict | None:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    return None


def _add_node(graph: dict, node: dict) -> dict | None:
    """追加节点；超过上限则只累加 truncated 计数并返回 None。"""
    if len(graph["nodes"]) >= MAX_NODES:
        graph["counters"]["truncated"] += 1
        return None
    graph["nodes"].append(node)
    return node


def _add_edge(graph: dict, src: str, dst: str, kind: str = "seq") -> None:
    edge = {"f": src, "t": dst, "kind": kind}
    if edge not in graph["edges"]:
        graph["edges"].append(edge)


def _running_step(graph: dict) -> dict | None:
    """当前处于 running 的步骤节点（工具调用挂到它下面）。"""
    for n in reversed(graph["nodes"]):
        if n["kind"] == "step" and n["status"] == "running":
            return n
    return None


def record(session_id: str, event: dict) -> None:
    """把一条任务事件并入该会话的当前 DAG（未知事件安全忽略）。"""
    etype = event.get("type") or ""
    if not etype:
        return
    sid = session_id or "default"

    if etype == "task_start":
        if len(_graphs) >= MAX_SESSIONS and sid not in _graphs:
            oldest = min(_graphs, key=lambda k: _graphs[k].get("started_at", 0))
            _graphs.pop(oldest, None)
        _graphs[sid] = _new_graph(sid, event.get("interaction") or "")
        return

    graph = _graphs.get(sid)
    if graph is None:
        return   # 没有 task_start 的孤立事件（如历史回放）直接丢弃

    if etype == "plan_created":
        graph["task"] = event.get("task") or graph["task"]
        root = _find(graph, "root")
        if root is not None and graph["task"]:
            root["label"] = graph["task"][:60]   # 根节点显示任务本身更有信息量
        # 按计划树的真实父子关系连边。**不要**把叶子串成链：叶子之间通常
        # 没有先后依赖（实测会并行/交错执行），串链会显示出并不存在的依赖。
        root_goal_id = str(event.get("root_goal_id") or "")
        for step in event.get("steps") or []:
            gid = str(step.get("goal_id") or "")
            if not gid or _find(graph, gid):
                continue
            node = _add_node(graph, {
                "id": gid, "kind": "step", "label": step.get("description") or "",
                "status": "pending", "t0": None, "t1": None,
                "tool": step.get("tool"), "error": "",
            })
            if node is None:
                break
            parent = str(step.get("parent_id") or "")
            # 父目标是根目标、或父不在图中（中间层未下发）→ 直接挂 root
            if not parent or parent == root_goal_id or not _find(graph, parent):
                parent = "root"
            _add_edge(graph, parent, gid, "sub")
            graph["counters"]["steps"] += 1

    elif etype == "plan_step_start":
        gid = str(event.get("goal_id") or "")
        node = _find(graph, gid)
        if node is None and gid:
            # 计划外补充的步骤（重规划）：挂到 root 之后
            node = _add_node(graph, {
                "id": gid, "kind": "step", "label": event.get("description") or "",
                "status": "pending", "t0": None, "t1": None,
                "tool": event.get("tool"), "error": "",
            })
            if node is not None:
                _add_edge(graph, "root", gid, "sub")
                graph["counters"]["steps"] += 1
        if node is not None:
            node["status"] = "running"
            node["t0"] = _now_ms()

    elif etype == "plan_step_end":
        node = _find(graph, str(event.get("goal_id") or ""))
        if node is not None:
            ok = bool(event.get("success"))
            node["status"] = "ok" if ok else "fail"
            node["t1"] = _now_ms()
            node["error"] = (event.get("error") or "")[:300]
            if not ok:
                graph["counters"]["failures"] += 1

    elif etype == "plan_backtrack":
        node = _find(graph, str(event.get("goal_id") or ""))
        graph["counters"]["backtracks"] += 1
        if node is not None:
            node["status"] = "backtrack"
            node["error"] = (event.get("reason") or "")[:300]

    elif etype == "step_action":
        # 优先用事件自带的 goal_id 归属（agent 在步骤窗口内才有值）；
        # 缺失时退回"当前 running 的步骤"，再退回 root —— 步骤窗口外发生的
        # 调用如实挂在 root 上，不硬凑到某个步骤里。
        gid = str(event.get("goal_id") or "")
        parent = (_find(graph, gid) if gid else None) or _running_step(graph) \
            or _find(graph, "root")
        idx = graph["counters"]["actions"]
        node = _add_node(graph, {
            "id": f"act{idx}", "kind": "action",
            "label": event.get("tool") or "工具调用",
            "status": "ok" if event.get("success", True) else "fail",
            "t0": _now_ms(), "t1": _now_ms(),
            "tool": event.get("tool"),
            "error": "" if event.get("success", True) else str(event.get("output") or "")[:300],
        })
        if node is not None:
            graph["counters"]["actions"] += 1
            if parent is not None:
                _add_edge(graph, parent["id"], node["id"], "call")
            if not event.get("success", True):
                graph["counters"]["failures"] += 1

    elif etype in ("task_complete", "task_error", "task_cancelled", "chat_done"):
        # chat_done 是对话模式的收尾（无计划步骤，图上只有 root）——
        # 一并归位，否则该会话的图会永远停在 running。
        graph["status"] = {"task_complete": "ok", "task_error": "fail",
                           "task_cancelled": "cancelled", "chat_done": "ok"}[etype]
        graph["finished_at"] = _now_ms()
        root = _find(graph, "root")
        if root is not None:
            root["status"] = graph["status"]
            root["t1"] = graph["finished_at"]
            if etype == "task_error":
                root["error"] = str(event.get("error") or "")[:300]
        # 收尾：仍挂着 running 的步骤按任务结局归位，避免图上永远转圈
        for n in graph["nodes"]:
            if n["kind"] == "step" and n["status"] == "running":
                n["status"] = "cancelled" if etype == "task_cancelled" else "fail"
                n["t1"] = graph["finished_at"]
        for fn in list(_listeners):
            try:
                fn(snapshot(sid) or {})
            except Exception:   # 商业侧异常不得影响社区核心
                pass


def snapshot(session_id: str) -> dict | None:
    """当前任务图的只读快照（深拷贝，调用方修改不影响内部状态）。"""
    graph = _graphs.get(session_id or "default")
    if graph is None:
        return None
    return {
        **graph,
        "nodes": [dict(n) for n in graph["nodes"]],
        "edges": [dict(e) for e in graph["edges"]],
        "counters": dict(graph["counters"]),
        "elapsed_ms": (graph["finished_at"] or _now_ms()) - graph["started_at"],
    }


def reset(session_id: str | None = None) -> None:
    """清空某会话（或全部）的图 —— 仅测试与会话销毁时使用。"""
    if session_id is None:
        _graphs.clear()
    else:
        _graphs.pop(session_id, None)
