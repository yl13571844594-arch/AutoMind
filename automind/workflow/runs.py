"""工作流运行记录 —— 按步落盘、断点续跑、最近运行（v1.8.0，R-104）。

为什么不直接复用 ``state.checkpoint.CheckpointManager``
------------------------------------------------------
那个管理器存的是 ``AgentState``（计划树 + ReAct 进度），文件名里嵌着会话 id，
续跑语义是"把进度交给模型接着想"。工作流的续跑语义完全不同：**逐步对账**——
哪几步已经成功、它们的输出是什么，续跑时这些步骤一次都不许再执行
（第 1–4 步可能已经改过生产系统）。硬塞进 AgentState 只会让两边的字段都变得含糊。

因此这里单独存，但**复用同一套状态词**（见 checkpoint.py），界面与文档只需要
认一套：

    running      运行中（服务重启后仍是这个状态 = 被打断，列表里会显示为可续跑）
    completed    全部成功
    partial      跑完了但有步骤失败 / 被 abort 中止   → 可续跑
    interrupted  被取消或进程内异常                   → 可续跑
    resumed      已被一次续跑接手                     → 不再可续跑
    dismissed    用户选择忽略                         → 不再可续跑

落盘方式与检查点一致：先写临时文件再原子替换，读者看到的要么是旧版要么是新版。

保留策略（v1.9.0）
-----------------
社区版按**条数**（最近 50 次）；专业版按**天数**（90 天，企业版可配置），
见 ``quota.workflow_history`` / ``workflow_history_days``。按天保留意味着记录会
多到几千条，列表与看板不能每次把所有 JSON 全文重读一遍 —— 摘要按
（文件名, mtime）缓存，文件没变就不再解析。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.workflow.runs")

RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_PARTIAL = "partial"
RUN_INTERRUPTED = "interrupted"
RUN_RESUMED = "resumed"
RUN_DISMISSED = "dismissed"

#: 可以续跑的状态
RESUMABLE = frozenset({RUN_RUNNING, RUN_PARTIAL, RUN_INTERRUPTED})

#: 单个步骤输出超过这个体积（序列化后字节数）就不存全文：
#: 续跑时该步骤会重新执行，而不是拿一份截断的输出去喂后面的步骤。
MAX_OUTPUT_BYTES = 1_000_000

#: 默认保留的运行记录条数（社区版"最近 50 次"，见 quota.workflow_history）
DEFAULT_KEEP = 50


def checkpoint_status(run_status: str) -> str:
    """执行器的整轮状态 → 落盘状态词。"""
    return {
        "ok": RUN_COMPLETED, "dry_run": RUN_COMPLETED,
        "partial": RUN_PARTIAL, "aborted": RUN_PARTIAL, "failed": RUN_PARTIAL,
        "cancelled": RUN_INTERRUPTED,
    }.get(run_status, RUN_PARTIAL)


class WorkflowRunStore:
    """工作流运行记录的存取（一次运行一个 JSON 文件）。"""

    def __init__(self, root: str | Path | None = None, *, keep: int | None = DEFAULT_KEEP,
                 keep_days: float | None = None) -> None:
        if root is None:
            from automind.core.paths import workflow_runs_dir

            root = workflow_runs_dir()
        self.root = Path(root)
        #: 按条数保留（None / 0 = 不按条数限）
        self.keep = keep
        #: 按天数保留（None = 不按天数限）；两者都给时都生效
        self.keep_days = keep_days
        #: 可重入：checkpoint 的"读-改-写"整段持锁，其中又会调用 save()
        self._lock = threading.RLock()
        #: 摘要缓存：文件名 → (mtime_ns, 摘要)。见模块开头"保留策略"
        self._summary_cache: dict[str, tuple[int, dict[str, Any]]] = {}

    # ── 基础读写 ──────────────────────────────────────

    def _path(self, run_id: str) -> Path:
        safe = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("run_id 不合法")
        return self.root / f"{safe}.json"

    def load(self, run_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(run_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("workflow_run_unreadable", run_id=run_id, error=str(exc))
            return None
        return data if isinstance(data, dict) else None

    def save(self, record: dict[str, Any]) -> None:
        run_id = str(record.get("run_id") or "")
        path = self._path(run_id)
        record["updated_at"] = time.time()
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(path)

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        record = self.load(run_id)
        if record is None:
            return None
        record.update(fields)
        self.save(record)
        return record

    # ── 生命周期 ──────────────────────────────────────

    def start(self, *, run_id: str, workflow: str, file: str, source: str,
              source_digest: str, inputs: dict[str, Any], session_id: str,
              resumed_from: str = "", trigger: str = "manual", trigger_key: str = "",
              on_restart: str = "notify", retry_left: int = 0,
              attempt: int = 1) -> dict[str, Any]:
        record = {
            "run_id": run_id, "workflow": workflow, "file": file, "source": source,
            "source_digest": source_digest, "inputs": inputs, "session_id": session_id,
            "status": RUN_RUNNING, "run_status": "", "trigger": trigger,
            # v1.9.0：触发运行的来源与无人值守策略（重启续跑 / 失败重试）
            "trigger_key": trigger_key, "on_restart": on_restart,
            "retry_left": int(retry_left), "attempt": int(attempt),
            "started_at": time.time(), "finished_at": None,
            "records": {}, "outputs": {}, "oversized": [],
            "resumed_from": resumed_from, "resumed_by": "", "error": "", "report": None,
        }
        self.save(record)
        self.prune()
        return record

    def checkpoint(self, run_id: str, snapshot: dict[str, Any]) -> None:
        """执行器每个顶层步骤（及 parallel 组内每个子步骤）结束后调用。

        整段"读-改-写"持锁，并按快照序号丢弃迟到的旧快照：并行组里几个子步骤
        几乎同时结束时，两次落盘在线程池里的先后是不确定的。
        """
        with self._lock:
            self._checkpoint_locked(run_id, snapshot)

    def _checkpoint_locked(self, run_id: str, snapshot: dict[str, Any]) -> None:
        record = self.load(run_id)
        if record is None:
            return
        seq = int(snapshot.get("seq") or 0)
        if seq and seq < int(record.get("checkpoint_seq") or 0):
            return
        record["checkpoint_seq"] = seq
        outputs: dict[str, Any] = {}
        oversized: list[str] = []
        for sid, value in (snapshot.get("outputs") or {}).items():
            size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
            if size > MAX_OUTPUT_BYTES:
                oversized.append(sid)
            else:
                outputs[sid] = value
        record["records"] = snapshot.get("records") or {}
        record["outputs"] = outputs
        record["oversized"] = oversized
        self.save(record)

    def finish(self, run_id: str, report: dict[str, Any]) -> dict[str, Any] | None:
        run_status = str(report.get("status") or "")
        return self.update(run_id, status=checkpoint_status(run_status),
                           run_status=run_status, finished_at=time.time(),
                           report=report, error=str(report.get("error") or ""))

    def interrupt(self, run_id: str, reason: str) -> None:
        self.update(run_id, status=RUN_INTERRUPTED, finished_at=time.time(),
                    error=reason)

    def recover_stale(self, live: set[str]) -> list[str]:
        """服务启动时：把"running 但已不在跑"的记录改成 interrupted（D5：只提示，不自动续跑）。"""
        touched: list[str] = []
        for record in self._iter():
            if record.get("status") == RUN_RUNNING and record.get("run_id") not in live:
                record["status"] = RUN_INTERRUPTED
                record["error"] = record.get("error") or "服务重启时该运行尚未结束"
                self.save(record)
                touched.append(str(record.get("run_id")))
        return touched

    # ── 续跑 ──────────────────────────────────────────

    def resume_plan(self, run_id: str) -> tuple[dict[str, Any] | None, str]:
        """给出续跑所需的 ``resume`` 参数；不能续跑时返回 ``(None, 原因)``。"""
        record = self.load(run_id)
        if record is None:
            return None, "找不到这次运行记录（可能已被清理）"
        status = str(record.get("status") or "")
        if status == RUN_COMPLETED:
            return None, "这次运行已全部成功，没有需要续跑的步骤"
        if status == RUN_RESUMED:
            return None, (f"这次运行已被续跑过（续跑记录 {record.get('resumed_by') or '未知'}），"
                          "请在最新的那次运行上继续")
        if status == RUN_DISMISSED:
            return None, "这次运行已被标记为忽略"
        if status not in RESUMABLE:
            return None, f"当前状态 {status!r} 不支持续跑"
        steps: dict[str, Any] = {}
        outputs = record.get("outputs") or {}
        for sid, rec in (record.get("records") or {}).items():
            if not isinstance(rec, dict) or rec.get("status") != "ok":
                continue
            if sid not in outputs:          # 输出太大没存全文 → 该步重跑
                continue
            steps[sid] = {"output": outputs[sid],
                          "output_digest": rec.get("output_digest") or ""}
        return {"from_run": run_id, "steps": steps}, ""

    # ── 查询 ──────────────────────────────────────────

    def _iter(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("run_id"):
                out.append(data)
        out.sort(key=lambda r: float(r.get("started_at") or 0), reverse=True)
        return out

    def summaries(self, *, since: float = 0.0) -> list[dict[str, Any]]:
        """全部运行的摘要（新的在前），按 mtime 缓存 —— 列表与看板用这个，不读全文。"""
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        alive: set[str] = set()
        for path in self.root.glob("*.json"):
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            alive.add(path.name)
            hit = self._summary_cache.get(path.name)
            if hit is None or hit[0] != mtime:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not (isinstance(data, dict) and data.get("run_id")):
                    continue
                hit = (mtime, summarize(data))
                self._summary_cache[path.name] = hit
            row = hit[1]
            if since and float(row.get("started_at") or 0) < since:
                continue
            out.append(dict(row))
        for name in set(self._summary_cache) - alive:
            self._summary_cache.pop(name, None)
        out.sort(key=lambda r: float(r.get("started_at") or 0), reverse=True)
        return out

    def list(self, *, limit: int = 50, file: str = "", workflow: str = "",
             trigger_key: str = "") -> list[dict[str, Any]]:
        rows = []
        for row in self.summaries():
            if file and row.get("file") != file:
                continue
            if workflow and row.get("workflow") != workflow:
                continue
            if trigger_key and row.get("trigger_key") != trigger_key:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def latest_by_file(self) -> dict[str, dict[str, Any]]:
        """每个工作流文件最近一次运行的摘要（列表视图的"最近运行"列）。"""
        out: dict[str, dict[str, Any]] = {}
        for row in self.summaries():
            key = str(row.get("file") or "")
            if key and key not in out:
                out[key] = row
        return out

    def prune(self) -> int:
        """按条数 / 天数清理旧记录；正在跑的绝不删。"""
        if not self.keep and not self.keep_days:
            return 0
        rows = self.summaries()
        doomed: list[dict[str, Any]] = []
        if self.keep:
            doomed.extend(rows[self.keep:])
        if self.keep_days:
            cutoff = time.time() - float(self.keep_days) * 86400
            doomed.extend(r for r in rows if float(r.get("started_at") or 0) < cutoff)
        removed = 0
        done: set[str] = set()
        for row in doomed:
            run_id = str(row.get("run_id") or "")
            if not run_id or run_id in done or row.get("status") == RUN_RUNNING:
                continue
            done.add(run_id)
            try:
                self._path(run_id).unlink()
                removed += 1
            except (OSError, ValueError):
                pass
        return removed


def summarize(record: dict[str, Any]) -> dict[str, Any]:
    """运行记录 → 列表用的摘要（不带步骤输出，体积小）。"""
    report = record.get("report") or {}
    summary = report.get("summary") or {}
    records = record.get("records") or {}
    # 只数顶层步骤（parallel 组内的子步骤带 parent，单独落盘是为了子步骤级续跑）
    done = sum(1 for r in records.values()
               if isinstance(r, dict) and r.get("status") == "ok" and not r.get("parent"))
    failed_step = next((s for s in report.get("steps") or []
                        if isinstance(s, dict) and s.get("status") in ("failed", "aborted")), None)
    return {
        "run_id": record.get("run_id"), "workflow": record.get("workflow"),
        "file": record.get("file"), "status": record.get("status"),
        "run_status": record.get("run_status"), "trigger": record.get("trigger"),
        "trigger_key": record.get("trigger_key") or "",
        "attempt": record.get("attempt") or 1,
        # 看板用（v1.9.0，R-207）：与报告逐条对得上的几项
        "duration_ms": report.get("duration_ms"),
        "tokens": int(((report.get("tokens") or {}).get("total")) or 0),
        "failed_step": (failed_step or {}).get("id", ""),
        "failed_error": str((failed_step or {}).get("error") or "")[:300],
        "session_id": record.get("session_id"),
        "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
        "resumed_from": record.get("resumed_from"), "resumed_by": record.get("resumed_by"),
        "error": record.get("error"), "steps_done": done,
        "steps_total": summary.get("total"),
        "resumable": record.get("status") in RESUMABLE,
    }


__all__ = [
    "MAX_OUTPUT_BYTES",
    "RESUMABLE",
    "RUN_COMPLETED",
    "RUN_DISMISSED",
    "RUN_INTERRUPTED",
    "RUN_PARTIAL",
    "RUN_RESUMED",
    "RUN_RUNNING",
    "WorkflowRunStore",
    "checkpoint_status",
    "summarize",
]
