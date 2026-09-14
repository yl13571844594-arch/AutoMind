"""目录级并发写冲突治理 —— 按路径串行化 + 外来改动可检出。

解决的问题（v1.6.3 及更早）：会话隔离解决了**执行态**串扰，但克隆仍共享同一
项目目录。两个会话（或同一会话的两个并发任务）同时改同一文件时：

  · 文件写入本身是交错的 —— A 读到内容、B 覆盖、A 再写回去，B 的成果消失；
  · 回滚记录是按文件记的，且记的是「我写之前的内容」——A 的回滚会把 B 的
    修改一起回退掉，**静默地**替对方"恢复"了一个对方从没写过的版本。

本模块提供三层防护，逐层收紧：

1. **进程内按路径异步锁**（:func:`path_lock`）—— 同一路径的写入串行化，
   避免 `read → 覆盖 → 写回` 交错；不同路径互不阻塞，不牺牲并行度。
2. **来源追踪 + 外来改动检出**（:meth:`WriteGuard.check`）—— 记住"每个文件
   最近是谁写的、什么时候"，当我要覆盖一个**别的会话刚改过、而我从未读过**
   的文件时，给出确定性结论（而不是赌运气）。
3. **确定性前置条件**（`expected_hash` / `expected_version`）—— 模型可声明
   "我要改的是我读到的那一版"，不匹配就按失败返回并附上当前内容，让模型
   基于真实状态重做，而不是覆盖掉别人的成果。

策略（`ExecutionConfig.write_conflict_policy`）：
  · ``off``   —— 只加锁与追踪，不做冲突判定（行为等价于旧版）；
  · ``warn``  —— 默认。冲突时**仍然写入**，但结果里带 ``conflict`` 字段与
    明确提示，模型/用户都能看见"你刚覆盖了别人的改动"；
  · ``block`` —— 冲突时拒绝写入，返回当前内容与建议（最保守）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

#: 记住的路径数量上限（超出后淘汰最久未动的条目，保护内存）
MAX_TRACKED = 4000

#: 同一路径的写锁表（进程级；路径字符串为键）
_locks: dict[str, asyncio.Lock] = {}


def _now() -> float:
    return time.time()


def content_hash(text: str | bytes) -> str:
    """稳定的内容指纹（写入前置条件与冲突比对都用它）。"""
    data = text.encode("utf-8", errors="replace") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()[:16]


@asynccontextmanager
async def path_lock(key: str):
    """按路径串行化写入；同路径排队，不同路径并行。"""
    lock = _locks.get(key)
    if lock is None:
        lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        yield


@dataclass
class PathInfo:
    """某个文件最近一次的读写来源。"""

    last_writer: str = ""          # 会话 id（"" = 未知/非会话写入）
    last_write_ts: float = 0.0
    last_write_tool: str = ""
    #: 会话 id → 该会话最后一次读到/写到此文件的时间
    touched: dict[str, float] = field(default_factory=dict)

    def touch(self, sid: str, ts: float) -> None:
        if sid:
            # 只保留最近 8 个会话的接触记录
            if len(self.touched) > 8:
                oldest = min(self.touched, key=lambda k: self.touched[k])
                self.touched.pop(oldest, None)
            self.touched[sid] = ts


#: 路径 → 来源信息
_paths: dict[str, PathInfo] = {}


def _info(path: str) -> PathInfo:
    info = _paths.get(path)
    if info is None:
        if len(_paths) >= MAX_TRACKED:
            oldest = min(_paths, key=lambda k: max(
                _paths[k].last_write_ts, max(_paths[k].touched.values(), default=0.0)))
            _paths.pop(oldest, None)
        info = PathInfo()
        _paths[path] = info
    return info


class WriteGuard:
    """一次文件写入前/后的冲突判定与来源登记。"""

    def __init__(self, session: str = "", policy: str = "warn",
                 project_root: str = "") -> None:
        self.session = session or ""
        self.policy = (policy or "warn").lower()
        self.project_root = project_root

    # ── 登记 ────────────────────────────────────────────────

    def note_read(self, path: str) -> None:
        """登记"本会话读过这个文件"。"""
        _info(str(path)).touch(self.session, _now())

    def note_write(self, path: str, tool: str = "") -> None:
        """登记"本会话刚写了这个文件"（用于后续回滚/冲突判定）。"""
        info = _info(str(path))
        info.last_writer = self.session
        info.last_write_ts = _now()
        info.last_write_tool = tool
        info.touch(self.session, info.last_write_ts)

    # ── 判定 ────────────────────────────────────────────────

    def foreign_writer(self, path: str) -> tuple[str, float] | None:
        """返回 (别的会话 id, 写入时间)，本会话是最近写入者时返回 None。"""
        if not self.policy or self.policy == "off" or not self.session:
            return None
        info = _paths.get(str(path))
        if info is None or not info.last_writer:
            return None
        if info.last_writer == self.session:
            return None
        my_last = info.touched.get(self.session, 0.0)
        # 我在对方写入之后读过 → 我看的是最新内容，不算冲突
        if my_last >= info.last_write_ts:
            return None
        return info.last_writer, info.last_write_ts

    def check(self, path: str, expected_hash: str | None = None) -> dict[str, Any]:
        """写入前的确定性校验。

        Returns:
            ``{"ok": bool, "reason": str, "foreign": str, "age_s": float}``；
            ``ok=False`` 表示按当前策略**不应继续写入**。
        """
        result: dict[str, Any] = {"ok": True, "reason": "", "foreign": "", "age_s": 0.0}
        foreign = self.foreign_writer(path)
        if foreign is not None:
            writer, ts = foreign
            result.update({
                "foreign": writer,
                "age_s": round(max(0.0, _now() - ts), 1),
            })
            reason = (
                f"该文件在 {result['age_s']} 秒前被**另一个会话**(`{writer}`)写过，"
                f"而本会话之后没有读过它 —— 直接覆盖会丢弃对方的改动。"
                f"建议先重新读取该文件（`file_read`）确认当前内容再修改。"
            )
            if self.policy == "block":
                result.update({"ok": False, "reason": reason})
            else:
                result["reason"] = reason
        if expected_hash:
            try:
                from pathlib import Path
                current = Path(path).read_text(encoding="utf-8")
            except Exception:
                current = None
            if current is None:
                result.update({
                    "ok": False,
                    "reason": "无法读取文件当前内容以校验 expected_hash。",
                })
            elif content_hash(current) != expected_hash:
                result.update({
                    "ok": False,
                    "reason": (
                        f"内容校验失败：当前内容指纹 {content_hash(current)} "
                        f"与期望的 {expected_hash} 不一致 —— 文件在你读取之后已被改动。"
                        f"请重新读取后再修改。"),
                })
        return result


def reset_for_tests() -> None:
    """清空来源登记与锁表（仅测试用）。"""
    _paths.clear()
    _locks.clear()
