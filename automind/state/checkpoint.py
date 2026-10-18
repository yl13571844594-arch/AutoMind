"""检查点管理 — 保存和恢复 Agent 状态。"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from automind.core.types import AgentState

#: 计划处于这些状态时视为"已收尾"，没有续跑的必要
_TERMINAL_PLAN_STATUS = frozenset({"completed", "aborted", "failed"})

#: 检查点 metadata.status：任务收尾时写入。只有这些状态**不可**续跑
#: （running = 跑的途中被杀/断线；partial = 交了部分交付清单；interrupted =
#: 被取消或抛错 —— 这三种都值得接着做）
_TERMINAL_RUN_STATUS = frozenset({"completed", "dismissed", "resumed"})


def session_of(checkpoint_id: str) -> str:
    """从检查点 id 里取出会话 id：``ckpt_<sid>_<秒>_<微秒>``。

    v1.7.7：此前按前缀 ``ckpt_<sid>_`` 匹配，而会话 id 本身可以含下划线 ——
    ``s_abc`` 会把 ``s_abc_c1234``（同一浏览器的另一个会话）的检查点也算进来，
    续跑就可能接到别的会话的任务上。按"去掉末尾两段数字"精确还原。
    """
    stem = checkpoint_id[5:] if checkpoint_id.startswith("ckpt_") else checkpoint_id
    parts = stem.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0]
    return ""


class CheckpointManager:
    """检查点管理器 — 序列化/反序列化 AgentState。

    使用示例::

        mgr = CheckpointManager(".automind/checkpoints")
        checkpoint_id = await mgr.save(state)
        state = await mgr.load(checkpoint_id)

    v1.7.5 起，检查点从"任务结束后写一次的归档"变成**执行途中按步落盘**，
    因此这个类多了三件配套能力：

    * id 唯一化 —— 同一秒内的两次保存不再互相覆盖（原来是秒级时间戳）；
    * :meth:`latest_for_session` —— 按会话找回最近一个检查点；
    * :meth:`list_resumable` —— 列出**还没跑完**的检查点，供重启后提示用户。
    """

    def __init__(self, checkpoint_dir: str | Path = ".automind/checkpoints") -> None:
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, state: AgentState) -> str:
        """保存当前状态为检查点。

        Returns:
            检查点文件路径。
        """
        state.last_updated = datetime.now(UTC)
        # v1.7.5：id 里补上毫秒。原先是秒级时间戳，同一秒内保存两次
        # （按步落盘之后这完全可能）会**静默互相覆盖**，丢掉一份状态。
        checkpoint_id = f"ckpt_{state.session_id}_{int(time.time())}_{int(time.time_ns() % 1_000_000):06d}"
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"

        data = state.model_dump(mode="json")
        # 原子落盘：按步保存意味着写入频率高得多，中途被杀很可能留下
        # 半截 JSON，而那样一份"损坏的检查点"会让下次启动的扫描直接抛异常。
        # 先写临时文件再替换，保证读者看到的要么是旧版要么是新版。
        tmp_path = file_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            # B-04 修复：data 已由 model_dump(mode="json") 转为 JSON 兼容结构，
            # 不再使用 default=str 兜底——避免把不可序列化对象静默转成不可逆字符串。
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(file_path)

        return checkpoint_id

    async def load(self, checkpoint_id: str) -> AgentState:
        """从检查点恢复状态。"""
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"
        if not file_path.exists():
            from automind.core.exceptions import CheckpointNotFoundError
            raise CheckpointNotFoundError(f"Checkpoint not found: {checkpoint_id}")

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        return AgentState.model_validate(data)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有检查点（最新的在前）。"""
        checkpoints = []
        for f in sorted(self.checkpoint_dir.glob("ckpt_*.json"), reverse=True):
            stat = f.stat()
            checkpoints.append({
                "id": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "created": stat.st_mtime,
            })
        return checkpoints

    def latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        """返回某个会话最新的检查点（没有则 None）。

        会话 id 内嵌在文件名里，所以这里不需要解析 JSON —— 重启后想
        "把某会话接到上次"时，这条路径必须是廉价的。
        """
        if not session_id:
            return None
        hits = [c for c in self.list_checkpoints()
                if session_of(c["id"]) == session_id]
        return hits[0] if hits else None       # list_checkpoints 已按新→旧排序

    def resumable_for_session(self, session_id: str) -> dict[str, Any] | None:
        """该会话**最近一次任务**若没跑完，返回续跑所需的摘要；否则 None。

        只看最新的那个检查点：它代表"这个会话最后在做什么"。更早的检查点
        即便没跑完，也已经被后来的任务取代了，接着做它们只会把人搞糊涂。

        两种可续跑的形态（v1.7.7）：

        * ``plan``  —— 计划里还有没完成的目标：原计划接着跑，已完成的跳过；
        * ``react`` —— ReAct（编程/工作）路径没有计划，但检查点里记着已经
          **真实完成**的动作与产物：续跑时把这份进度交给模型，而不是让它
          从一句原任务文本重新摸索一遍。
        """
        meta = self.latest_for_session(session_id)
        if meta is None:
            return None
        try:
            state = self.load_sync(meta["id"])
        except Exception:
            return None
        md = dict(getattr(state, "metadata", {}) or {})
        status = str(md.get("status") or "")
        if status in _TERMINAL_RUN_STATUS:
            return None
        info: dict[str, Any] = {
            "id": meta["id"], "session_id": session_id, "updated": meta["created"],
            "task": str(md.get("task") or ""), "status": status or "running",
            "interaction": str(md.get("interaction") or ""),
            "stop_reason": str(md.get("stop_reason") or ""),
        }
        plan = getattr(state, "plan", None)
        if plan is not None:
            pstatus = getattr(getattr(plan, "status", None), "value", "")
            progress = _plan_progress(plan)
            if pstatus not in _TERMINAL_PLAN_STATUS and progress["remaining"] > 0:
                info.update(kind="plan", **progress)
                info["task"] = info["task"] or getattr(plan, "task_description", "") or ""
                return info
        react = md.get("react") or {}
        if isinstance(react, dict) and (react.get("actions") or info["task"]):
            info.update(kind="react",
                        done=int(react.get("completed_actions") or 0),
                        failed=int(react.get("failed_actions") or 0),
                        artifacts=list(react.get("artifacts") or [])[:20])
            return info if info["task"] else None
        return None

    def mark(self, checkpoint_id: str, status: str) -> bool:
        """改写某个检查点的 metadata.status（如用户选择"忽略"时标 dismissed）。"""
        path = self.checkpoint_dir / f"{checkpoint_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("metadata", {})["status"] = status
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            return False

    def list_resumable(self) -> list[dict[str, Any]]:
        """列出**尚未跑完**的检查点（按时间倒序）。

        用于重启后的"上次有个任务没跑完，要接着做吗？"提示。
        刻意**不自动续跑** —— 那会用掉真实 token，必须由用户决定。

        损坏的检查点会被跳过而不是让整个扫描抛异常：按步落盘之后文件更多，
        一个坏文件不该让其它可恢复的任务一起消失。
        """
        out: list[dict[str, Any]] = []
        for meta in self.list_checkpoints():
            try:
                state = self.load_sync(meta["id"])
            except Exception:
                continue
            plan = getattr(state, "plan", None)
            if plan is None:
                continue
            status = getattr(getattr(plan, "status", None), "value", "")
            if status in _TERMINAL_PLAN_STATUS:
                continue
            progress = _plan_progress(plan)
            if progress["remaining"] <= 0:
                continue
            out.append({**meta, "session_id": state.session_id,
                        "task": getattr(plan, "task_description", "") or "",
                        **progress})
        return out

    def load_sync(self, checkpoint_id: str) -> AgentState:
        """同步读取（供不做 async 的扫描路径使用）。"""
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"
        with open(file_path, encoding="utf-8") as f:
            return AgentState.model_validate(json.load(f))

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """删除检查点。"""
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    async def prune(self, max_checkpoints: int = 20, keep_sessions: int = 64) -> int:
        """删除旧检查点：保留全局最新 N 个 + **每个会话最新的 1 个**。

        v1.7.7：此前只按全局数量裁剪 —— 多个会话并发跑时，一个会话的按步落盘
        很快就会把另一个会话"最后那个没跑完的检查点"挤掉，Web 端的续跑入口
        于是时有时无。每个会话的最新一份是续跑的依据，必须留下（最多
        keep_sessions 个会话，防止无限增长）。
        """
        checkpoints = sorted(
            self.checkpoint_dir.glob("ckpt_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        keep: set = set(checkpoints[:max_checkpoints])
        seen: set[str] = set()
        for cp in checkpoints:
            sid = session_of(cp.stem)
            if sid and sid not in seen and len(seen) < keep_sessions:
                seen.add(sid)
                keep.add(cp)
        removed = 0
        for cp in checkpoints:
            if cp in keep:
                continue
            try:
                cp.unlink()
                removed += 1
            except OSError:
                pass
        return removed


def _plan_progress(plan: Any) -> dict[str, int]:
    """统计计划里的目标进度（total / done / remaining）。"""
    goals: list[Any] = []
    root = getattr(plan, "root_goal", None)
    if root is not None:
        goals = list(getattr(root, "children", []) or [])
    if not goals:
        goals = list(getattr(plan, "goals", []) or [])
    done = 0
    for g in goals:
        status = getattr(getattr(g, "status", None), "value", "")
        if status == "completed":
            done += 1
    return {"total": len(goals), "done": done,
            "remaining": max(0, len(goals) - done)}

