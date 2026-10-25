"""检查点管理 — 保存和恢复 Agent 状态。"""

from __future__ import annotations

import json
import threading
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

#: v1.11.16：可续跑的四种任务形态。multi / loop 的进度由 budget 成员写入
#: metadata.progress（multi={plan,steps,scratch,last_output,...}、
#: loop={iterations,last_output,feedback}），这里只负责发现与归一化，
#: 具体怎么接着跑由 agent.resume_from_checkpoint 决定。
KINDS = ("plan", "react", "multi", "loop")

#: metadata.run_mode / interaction 里表示 ReAct 系（工作 / 编程）的取值
_REACT_MODES = frozenset({"work", "coding", "react"})

#: 多智能体步骤"已完成"的多种写法（不同 orchestrator 版本用词不完全一致）
_STEP_DONE = frozenset({"completed", "done", "ok", "success", "skipped"})


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run_mode(md: dict[str, Any]) -> str:
    """任务的运行形态：优先新字段 run_mode，旧检查点回退 interaction。"""
    return str(md.get("run_mode") or md.get("interaction") or "").strip().lower()


def _progress_payload(md: dict[str, Any]) -> dict[str, Any]:
    """取 metadata.progress 的本体。

    兼容两种形态：``{"plan": [...], ...}``（扁平，契约推荐）与
    ``{"multi": {...}}`` / ``{"loop": {...}}``（按模式再嵌一层）——
    加一层壳的成本很低，而收错字段的代价是"续跑时进度全是 0"。
    """
    p = _as_dict(md.get("progress"))
    for key in ("multi", "loop", "react", "plan"):
        inner = _as_dict(p.get(key))
        if inner:
            return inner
    return p


def _multi_progress(md: dict[str, Any]) -> dict[str, Any]:
    """multi（协同）进度：已完成 / 总角色步骤数 + 最近输出。

    计数优先级（从可靠到兜底）：

    1. ``progress.steps_done`` / ``progress.steps_total`` —— budget 契约显式写入，
       最准（steps 列表可能只含已执行的步骤、plan 才是总量）；
    2. 从 ``steps`` 的 status/state/ok 字段数已完成数；
    3. 再不行：``steps`` 全部视为已完成，总量取 ``plan`` / ``steps`` 的最大长度。
    """
    p = _progress_payload(md)
    plan = _as_list(p.get("plan"))
    steps = _as_list(p.get("steps"))
    status_seen = False
    completed = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        marker = step.get("status", step.get("state"))
        if marker is not None:
            status_seen = True
        if (str(marker or "").lower() in _STEP_DONE) or step.get("ok") is True:
            completed += 1
    explicit_done = p.get("steps_done")
    if explicit_done is None:
        explicit_done = md.get("steps_done")
    explicit_total = p.get("steps_total")
    if explicit_total is None:
        explicit_total = md.get("steps_total")
    done = _as_int(explicit_done) if explicit_done is not None else (
        completed if status_seen or steps else 0)
    if explicit_total is not None:
        total = _as_int(explicit_total)
    else:
        total = max(len(plan), len(steps), done)
    done = max(0, min(done, total)) if total else max(0, done)
    return {
        "steps_done": done,
        "steps_total": max(0, total),
        "last_output": str(p.get("last_output") or "")[:500],
    }


def _loop_progress(md: dict[str, Any]) -> dict[str, Any]:
    """loop（循环）进度：已完成的迭代轮数 + 最近输出 / 反馈。"""
    p = _progress_payload(md)
    iterations = p.get("iterations")
    if iterations is None:
        iterations = md.get("iterations")
    return {
        "iterations": max(0, _as_int(iterations)),
        "last_output": str(p.get("last_output") or "")[:500],
        "feedback": str(p.get("feedback") or "")[:300],
    }


def _react_progress(md: dict[str, Any]) -> dict[str, Any]:
    """ReAct（工作/编程）进度：真实完成的动作 / 产物 / 未解决失败。"""
    react = _as_dict(md.get("react"))
    acts = _as_list(react.get("actions"))
    done = _as_int(react.get("completed_actions"),
                   sum(1 for a in acts if isinstance(a, dict) and a.get("ok")))
    failed = _as_int(react.get("failed_actions"))
    arts = [str(x) for x in _as_list(react.get("artifacts"))][:20]
    return {"completed_actions": max(0, done), "failed_actions": max(0, failed),
            "artifacts": arts}


def _resumable_info(meta: dict[str, Any], state: Any) -> dict[str, Any] | None:
    """把一份 AgentState 归一化成"可续跑摘要"；不可续跑返回 None。

    v1.11.16 起统一支持 plan / react / multi / loop 四种 kind：
    返回 ``{id,session_id,updated,task,status,interaction,run_mode,kind,
    stop_reason,progress,...}``，其中 ``progress`` 的键随 kind 而定；
    同时保留 plan/react 的旧扁平字段（done/total/remaining/artifacts），
    旧前端不需要改也能继续显示。
    """
    md = _as_dict(getattr(state, "metadata", {}))
    status = str(md.get("status") or "")
    if status in _TERMINAL_RUN_STATUS:
        return None
    mode = _run_mode(md)
    plan = getattr(state, "plan", None)
    task = str(md.get("task") or "").strip()
    info: dict[str, Any] = {
        "id": meta["id"], "session_id": str(getattr(state, "session_id", "") or ""),
        "updated": meta.get("created"), "status": status or "running",
        "interaction": str(md.get("interaction") or ""), "run_mode": mode,
        "stop_reason": str(md.get("stop_reason") or ""),
        "budget_exhausted": bool(md.get("budget_exhausted")),
    }

    if mode in ("multi", "loop"):
        # 协同 / 循环检查点：进度在 metadata.progress 里，plan 树上没有可跑的步骤
        progress = _multi_progress(md) if mode == "multi" else _loop_progress(md)
        kind = mode
    elif plan is not None:
        pstatus = getattr(getattr(plan, "status", None), "value", "")
        progress = _plan_progress(plan)
        if pstatus in _TERMINAL_PLAN_STATUS or progress["remaining"] <= 0:
            return None
        task = task or str(getattr(plan, "task_description", "") or "")
        kind = "plan"
    else:
        react = _as_dict(md.get("react"))
        if not task:
            return None                     # 没有任务原文就无从续跑（也没有可交给模型的提示）
        # react 形态的判定要兼容很老的检查点（v1.7.5/1.7.6）：那时还没有
        # metadata.interaction，只有 task+running/partial —— 这类检查点同样
        # 只能走"把任务文本 + 已完成进度交给模型"的 ReAct 续跑路径。
        reactish = bool(react) or mode in _REACT_MODES or (not mode and bool(task))
        if not reactish:
            # 不是已知的 ReAct 形态、也没有 react 进度：宁可不提示，也不给一个点了会跑错的断点
            return None
        progress = _react_progress(md)
        kind = "react"
    if not task and kind != "plan":
        return None
    info.update({"task": task[:4000], "kind": kind, "progress": progress})
    # 旧前端（v1.11.15 及以前）只认这些扁平字段：保持兼容
    if kind == "plan":
        info.update({"done": progress["done"], "total": progress["total"],
                     "remaining": progress["remaining"]})
    elif kind == "react":
        info.update({"done": progress["completed_actions"],
                     "failed": progress["failed_actions"],
                     "artifacts": progress["artifacts"]})
    elif kind == "multi":
        info.update({"steps_done": progress["steps_done"],
                     "steps_total": progress["steps_total"],
                     "done": progress["steps_done"], "total": progress["steps_total"],
                     "remaining": max(0, progress["steps_total"] - progress["steps_done"])})
    elif kind == "loop":
        info.update({"iterations": progress["iterations"],
                     "last_output": progress["last_output"]})
    return info



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


#: 检查点 id 分配锁与进程内自增序号。
#:
#: 为什么不能只用 ``time.time_ns()``（v1.11.16 实测，budget 成员发现）：
#: Windows 上 ``time.time_ns()`` 的底层时钟分辨率可能只有毫秒级，同一 tick
#: 内连续调用会返回**完全相同**的值 —— 两次 save 算出相同 id，后一份会静默
#: 覆盖前一份（"预算耗尽写检查点 → 立刻续跑又写一份"就撞过；生产按 15s
#: 节流风险低，但这是断点续跑的正确性隐患）。
#:
#: id 格式保持 ``ckpt_<sid>_<秒>_<6 位微秒 + 4 位自增序号>``（末段共 10 位纯
#: 数字）—— ``session_of()`` 用 ``rsplit("_", 2)`` 解析，只要求两段
#: ``isdigit()``，因此位数变化对旧/新检查点都兼容。
_ID_LOCK = threading.Lock()
#: 进程内自增序号（0000~9999 循环；同一微秒内 1 万次保存才会绕回，
#: 由文件存在性检查兜底，正常执行节奏下不可能触发）
_ID_SEQ = 0
_ID_SEQ_MOD = 10_000


def _allocate_checkpoint_id(directory: Path, session_id: str) -> tuple[str, Path]:
    """分配一个**进程内唯一**的检查点 id；返回 ``(id, 文件路径)``。

    末段 = 6 位微秒（沿用旧实现的 6 位数字口径）+ 4 位进程内自增序号：
    同一 clock tick 内连续 save 会得到不同 id，**不会再静默覆盖**。
    跨进程 / 重启后的极小概率碰撞由"目标文件是否已存在"兜底
    （碰撞时换下一个序号重试，而不是覆盖）。
    """
    global _ID_SEQ
    now_ns = time.time_ns()
    sec = now_ns // 1_000_000_000
    micro = (now_ns // 1_000) % 1_000_000       # 秒内微秒（6 位）
    with _ID_LOCK:
        for _ in range(_ID_SEQ_MOD):
            _ID_SEQ = (_ID_SEQ + 1) % _ID_SEQ_MOD
            checkpoint_id = f"ckpt_{session_id}_{sec}_{micro:06d}{_ID_SEQ:04d}"
            file_path = directory / f"{checkpoint_id}.json"
            if not file_path.exists() and not file_path.with_suffix(".json.tmp").exists():
                return checkpoint_id, file_path
        # 极端兜底：不静默覆盖 —— 宁可让这次旁路落盘失败（调用方会记日志）
        raise RuntimeError("无法分配唯一检查点 id（同微秒检查点异常多）")


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
        # v1.7.5：id 里补上"微秒"段。原先是秒级时间戳，同一秒内保存两次
        # （按步落盘之后这完全可能）会**静默互相覆盖**，丢掉一份状态。
        # v1.11.16：仅靠 time_ns() 的余数在 Windows 上不够（同一 clock tick
        # 内返回值可能完全相同），改为进程内单调分配，见 _allocate_checkpoint_id。
        checkpoint_id, file_path = _allocate_checkpoint_id(
            self.checkpoint_dir, str(state.session_id))

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
        """列出所有检查点（最新的在前）。

        v1.11.16：改按**文件 mtime** 倒序。此前按文件名倒序，而文件名以
        ``ckpt_<session_id>_`` 开头 —— 不同会话的检查点会被按 sid 分成几段，
        全局"最新在前的顺序"根本是错的（``list_resumable`` 依赖这个顺序）。
        """
        checkpoints = []
        for f in self.checkpoint_dir.glob("ckpt_*.json"):
            stat = f.stat()
            checkpoints.append({
                "id": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "created": stat.st_mtime,
            })
        checkpoints.sort(key=lambda c: (c["created"], c["id"]), reverse=True)
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

        v1.11.16：可续跑形态从两种扩到四种（:data:`KINDS`）：

        * ``plan``  —— 计划里还有没完成的目标：原计划接着跑，已完成的跳过；
        * ``react`` —— ReAct（编程/工作）路径没有计划，但检查点里记着已经
          **真实完成**的动作与产物：续跑时把这份进度交给模型，而不是让它
          从一句原任务文本重新摸索一遍；
        * ``multi`` —— 协同模式：metadata.progress 里记着角色步骤与白板，
          续跑跳过已完成的 role 步骤后重新汇总；
        * ``loop``  —— 循环模式：metadata.progress 里记着已完成的迭代轮数，
          续跑从下一轮继续。

        返回的 ``progress`` 键随 kind 而定；plan/react 同时保留旧扁平字段，
        旧前端（v1.11.15）不需要改也能继续显示。
        """
        meta = self.latest_for_session(session_id)
        if meta is None:
            return None
        try:
            state = self.load_sync(meta["id"])
        except Exception:
            return None
        return _resumable_info(meta, state)

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

        v1.11.16：不再只遍历 plan 检查点 —— ReAct 的 partial 检查点重启后
        永远不出现"上次未完成"入口，正是本次要修的问题；multi / loop 同理。
        过滤规则与 :meth:`resumable_for_session` 完全一致（共用
        :func:`_resumable_info`），并且：

        * **每个会话只保留最新的一条**：更早的 partial 已被后来的任务取代，
          列出来只会让用户点错、续到旧任务上；
        * 状态为 completed / dismissed / resumed 的不出现；
        * 排序按文件 mtime 倒序（最新的在最前）；
        * 损坏的检查点会被跳过而不是让整个扫描抛异常。
        """
        out: list[dict[str, Any]] = []
        seen_sessions: set[str] = set()
        for meta in self.list_checkpoints():
            # 文件名里就能解析出会话 id：即使这份 JSON 损坏，也要把这个会话
            # 标记为"已看过最新一份"，否则会退回去把更早（已被取代）的
            # 未完成检查点翻出来当成可用断点。
            sid = session_of(meta["id"])
            if sid and sid in seen_sessions:
                continue
            try:
                state = self.load_sync(meta["id"])
            except Exception:
                if sid:
                    seen_sessions.add(sid)
                continue
            sid = str(getattr(state, "session_id", "") or sid)
            if sid in seen_sessions:
                continue
            # 每个会话只看最新一份：最新那份不可续 = 这个会话没有可续任务
            seen_sessions.add(sid)
            info = _resumable_info(meta, state)
            if info is None:
                continue
            out.append({**meta, **info})
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

