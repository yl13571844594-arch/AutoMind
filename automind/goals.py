"""长期目标（v1.11.0「目标交付模式」）—— 跨任务、跨会话持续推进的目标。

此前产品里的"目标"只有**单个任务内**的计划节点（``core.types.Goal``）：任务
一结束它就随之消失。用户的真实工作却常常是"一个目标 = 很多轮任务"——
"把这个仓库的测试覆盖率做到 80%"、"本周交付一份竞品分析"。每轮都得重新交代
一遍背景、期望交付物和已经做到哪，否则 Agent 对"之前推进到哪了"一无所知。

这里给每个工作区维护一份目标清单：

* **目标实体**：标题 + 期望交付物 + 验收标准 + 状态（进行中 / 暂停 / 已完成 / 归档）；
* **推进记录**：每轮挂在该目标下的任务结束时，记下任务、成败、结果摘要与
  模型给出的「目标进度」行 —— 详情页即"这个目标推进到哪了"的时间线；
* **上下文注入**：带着目标发消息时，把目标、交付物、验收标准与最近几轮进度
  作为**事实**注入（不是指令），模型据此接着推进，而不是从头再来。

不另起调度器：无人值守的长期推进交给工作流的触发器（定时 / webhook）——
目标层只做"聚合与叙事"，避免绕开工作流已有的治理与审计。

按 ``owner``（浏览器级会话 ID）+ ``workspace``（工作区后缀）隔离，
与会话目录（``automind.conversations``）同一口径。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.goals")

STATUSES = ("active", "paused", "done", "archived")
STATUS_ZH = {"active": "进行中", "paused": "已暂停", "done": "已完成", "archived": "已归档"}

#: 每个目标最多保留多少条推进记录（更早的滚掉，计数仍累计）
RUNS_CAP = 200
#: 注入上下文时带最近几轮推进记录
CONTEXT_RUNS = 5
#: 单条推进记录里结果摘要的长度上限
SUMMARY_CHARS = 400
#: 模型在回答末尾写的进度行（中英文冒号都认）
_PROGRESS_RE = re.compile(r"目标进度\s*[:：]\s*(.+)")
_DONE_MARK = "【目标已达成】"

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _clean(s: str, n: int = 64) -> str:
    return _SAFE.sub("", s or "")[:n]


def extract_progress(output: str) -> tuple[str, bool]:
    """从任务输出里取出模型写的「目标进度：…」行，以及是否声明目标已达成。"""
    text = output or ""
    note = ""
    for m in _PROGRESS_RE.finditer(text):
        note = m.group(1).strip()            # 取最后一处（回答末尾那行）
    return note[:300], _DONE_MARK in text


def _summary(output: str) -> str:
    """结果摘要：取输出末尾（结论通常在最后），压掉多余空白。"""
    flat = " ".join(str(output or "").split())
    return flat if len(flat) <= SUMMARY_CHARS else "…" + flat[-SUMMARY_CHARS:]


class GoalStore:
    """目标清单的持久化（单个 JSON 文件，进程内加锁、原子替换写入）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from automind.core.paths import goals_file
            path = goals_file()
        self.path = Path(path)
        self._lock = threading.RLock()

    # ── 底层读写 ───────────────────────────────────────
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            # 文件损坏不该让整个目标功能（以及带着目标的任务）跑不起来
            logger.warning("goals_load_failed", path=str(self.path), error=str(e))
            return {}

    def _dump(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ── 读 ─────────────────────────────────────────────
    def get(self, gid: str) -> dict[str, Any] | None:
        with self._lock:
            g = self._load().get(str(gid or ""))
            return dict(g) if g else None

    def list(self, owner: str, workspace: str) -> list[dict[str, Any]]:
        owner, workspace = _clean(owner), _clean(workspace)
        with self._lock:
            items = [dict(g) for g in self._load().values()
                     if g.get("owner") == owner and g.get("workspace") == workspace]
        order = {s: i for i, s in enumerate(STATUSES)}
        items.sort(key=lambda g: (order.get(g.get("status"), 9), -float(g.get("updated") or 0)))
        return items

    # ── 写 ─────────────────────────────────────────────
    def create(self, owner: str, workspace: str, title: str, deliverable: str = "",
               criteria: str = "") -> dict[str, Any]:
        title = (title or "").strip()[:120]
        if not title:
            raise ValueError("目标标题不能为空")
        now = time.time()
        goal = {
            "id": "g_" + uuid.uuid4().hex[:10],
            "owner": _clean(owner), "workspace": _clean(workspace),
            "title": title, "deliverable": (deliverable or "").strip()[:2000],
            "criteria": (criteria or "").strip()[:2000],
            "status": "active", "progress": "", "notes": "",
            "suggest_done": False, "created": now, "updated": now,
            "run_count": 0, "success_count": 0, "tokens": 0, "runs": [],
        }
        with self._lock:
            data = self._load()
            data[goal["id"]] = goal
            self._dump(data)
        return dict(goal)

    def update(self, gid: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            data = self._load()
            g = data.get(gid)
            if g is None:
                return None
            for k in ("title", "deliverable", "criteria", "notes", "progress"):
                if k in fields and fields[k] is not None:
                    limit = 120 if k == "title" else 2000
                    val = str(fields[k]).strip()[:limit]
                    if k == "title" and not val:
                        continue
                    g[k] = val
            if "status" in fields:
                st = str(fields["status"] or "")
                if st not in STATUSES:
                    raise ValueError(f"状态只能是：{' / '.join(STATUSES)}")
                g["status"] = st
                if st == "done":
                    g["suggest_done"] = False
                    g.setdefault("done_at", time.time())
            g["updated"] = time.time()
            self._dump(data)
            return dict(g)

    def delete(self, gid: str) -> bool:
        with self._lock:
            data = self._load()
            if data.pop(gid, None) is None:
                return False
            self._dump(data)
            return True

    def record_run(self, gid: str, *, task: str, success: bool | None, output: str = "",
                   status: str = "", interaction: str = "", tokens: int = 0,
                   session_id: str = "", duration_ms: float = 0.0) -> dict[str, Any] | None:
        """一轮挂在目标下的任务结束了 —— 记一条推进记录（未登记的目标直接忽略）。

        ``status``：ok / failed / cancelled / error（success 为 None 时据此判定）。
        """
        note, says_done = extract_progress(output)
        with self._lock:
            data = self._load()
            g = data.get(gid)
            if g is None:
                return None
            run = {
                "at": time.time(), "task": " ".join(str(task or "").split())[:300],
                "status": status or ("ok" if success else "failed"),
                "success": bool(success), "summary": _summary(output),
                "progress": note, "interaction": interaction,
                "tokens": int(tokens or 0), "session_id": session_id,
                "duration_ms": round(float(duration_ms or 0), 1),
            }
            g.setdefault("runs", []).append(run)
            del g["runs"][:-RUNS_CAP]
            g["run_count"] = int(g.get("run_count") or 0) + 1
            if success:
                g["success_count"] = int(g.get("success_count") or 0) + 1
            g["tokens"] = int(g.get("tokens") or 0) + int(tokens or 0)
            if note:
                g["progress"] = note
            if says_done and g.get("status") != "done":
                # 只"建议"完成：目标是否达成由人确认，模型自称达成不等于验收通过
                g["suggest_done"] = True
            if g.get("status") == "paused":
                g["status"] = "active"           # 又推进了一轮 = 重新进行中
            g["updated"] = time.time()
            self._dump(data)
            return dict(g)

    # ── 上下文注入 ─────────────────────────────────────
    @staticmethod
    def context_block(goal: dict[str, Any]) -> str:
        """带着目标发消息时注入的上下文块 —— 给事实，不替模型做决定。"""
        lines = [f"【目标交付模式】本轮任务服务于一个长期目标（已推进 {goal.get('run_count', 0)} 轮）：",
                 f"- 目标：{goal.get('title', '')}"]
        if goal.get("deliverable"):
            lines.append(f"- 期望交付物：{goal['deliverable']}")
        if goal.get("criteria"):
            lines.append(f"- 验收标准：{goal['criteria']}")
        if goal.get("progress"):
            lines.append(f"- 当前进度（上一轮的总结）：{goal['progress']}")
        if goal.get("notes"):
            lines.append(f"- 用户备注：{goal['notes'][:600]}")
        runs = [r for r in goal.get("runs") or [] if isinstance(r, dict)][-CONTEXT_RUNS:]
        if runs:
            lines.append("- 最近几轮推进（旧 → 新）：")
            for r in runs:
                mark = {"ok": "✓", "failed": "✗", "cancelled": "⏹", "error": "⚠"}.get(
                    r.get("status", ""), "·")
                tail = f" → {r['progress']}" if r.get("progress") else ""
                lines.append(f"  {mark} {r.get('task', '')[:100]}{tail}")
        lines += [
            "请围绕这个目标推进本轮任务：已经完成的部分不要重做，优先补齐离交付物最近的缺口。",
            "在回答的最后单独写一行「目标进度：<本轮推进了什么；离交付还差什么>」。"
            f"只有当交付物已经齐备、验收标准全部满足时，才在该行之后另起一行写「{_DONE_MARK}」。",
        ]
        return "\n".join(lines)


_store: GoalStore | None = None


def get_store() -> GoalStore:
    global _store
    if _store is None:
        _store = GoalStore()
    return _store
