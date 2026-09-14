"""执行证据落盘（Session Trace）—— 把 Agent 的真实行为写成可事后取证的 JSONL。

为什么要有它：`automind/core/observability.py` 此前只有内存里的实时 DAG
（`record` / `snapshot` / `reset`），进程一重启全没；仓库里唯一落盘的
`srv.log` 是 uvicorn 的**访问日志**，只有 HTTP 状态码，看不到任何 Agent 行为。
于是 B 端排障、SLA 举证、失败归因全都无据可查 —— 用户问"这次任务到底调了哪些
工具、哪一步开始不对、花了多少钱"，除了内存里那张图什么都拿不出来。

设计取向（明确取舍，避免变成又一个日志黑洞）：

  · **一条事件一行 JSON**（JSONL）—— 可 `tail -f` 实时看，可流式解析，
    单行损坏不影响其余行；不用一次性 load 整个文件。
  · **按 run 切文件**：``<data_dir>/traces/<session>/<run_id>.jsonl``。
    一个会话的多次任务各自成文件，取某个任务的证据不必过滤整个历史；
    每会话保留 ``trace_max_runs`` 个（默认 20），超出淘汰最旧的。
  · **双重上限**：单文件字节上限（超出后只累加 ``dropped`` 计数，不再写正文）
    与全局目录配额（超出淘汰最旧文件），保证轨迹不会把磁盘吃满。
  · **不阻断主流程**：所有写入失败（磁盘满、权限、路径非法）都只记一行
    logger 警告，绝不让轨迹问题影响任务本身。
  · **敏感信息脱敏**：写入前对 key 名做 ``redact``（复用 ``core/redact.py``
    的敏感字段语义），API Key / token / 密码不会进轨迹文件。
  · **纯本地、零 LLM 调用**，因此可以默认开启。

与观测中心的关系：观测中心（社区版）负责"看清这次任务在做什么"的实时视图；
轨迹负责"事后还能把这次任务原样摆出来"。两者共用同一份事件流（`record()`），
不额外产生事件。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.paths import traces_dir

logger = get_logger("automind.core.trace")

#: 要脱敏的字段名（小写包含匹配）。轨迹文件是给人看/给审计用的，
#: 但它会长期留在磁盘上，密钥不该在里面。
_SENSITIVE_KEYS = (
    "api_key", "apikey", "api-key", "authorization", "auth", "token",
    "password", "passwd", "secret", "cookie", "session_key", "private_key",
    "access_key", "credential",
)

#: 事件里体积可能很大的字段 —— 截断，避免单条事件把轨迹写爆
_TRUNCATE_KEYS = ("output", "prompt", "reply", "text", "delta", "content", "detail",
                  "issues", "reason", "feedback", "error", "stderr", "stdout")
_PER_FIELD_MAX = 4000


def _cfg() -> dict[str, Any]:
    """从 ExecutionConfig / 环境变量取轨迹开关（环境变量优先）。"""
    out = {
        "enabled": True,
        "dir": str(traces_dir()),
        "max_run_bytes": 8 * 1024 * 1024,
        "max_runs": 20,
        "max_total_bytes": 256 * 1024 * 1024,
    }
    try:
        from automind.core.config import ExecutionConfig

        ex = ExecutionConfig()
        out.update({
            "enabled": bool(ex.trace_enabled),
            "dir": ex.trace_dir or out["dir"],
            "max_run_bytes": int(ex.trace_max_run_bytes),
            "max_runs": int(ex.trace_max_runs),
        })
    except Exception:
        pass
    env_on = os.environ.get("AUTOMIND_TRACE")
    if env_on is not None:
        out["enabled"] = env_on.strip().lower() not in ("0", "false", "off", "no")
    env_dir = os.environ.get("AUTOMIND_TRACE_DIR")
    if env_dir:
        out["dir"] = env_dir
    return out


def _sanitize(value: Any, key: str = "") -> Any:
    """递归脱敏 + 截断，保证结果可 JSON 序列化。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            ks = str(k).lower()
            if any(s in ks for s in _SENSITIVE_KEYS):
                out[str(k)] = "***"
            else:
                out[str(k)] = _sanitize(v, str(k))
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, key) for v in value[:50]]
    if isinstance(value, (str, bytes)):
        s = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if len(s) > _PER_FIELD_MAX:
            return s[:_PER_FIELD_MAX] + f"…[轨迹截断，共 {len(s)} 字符]"
        return s
    if value is None or isinstance(value, (int, float, bool)):
        return value
    try:
        return _sanitize(str(value), key)
    except Exception:
        return "<不可序列化>"


class TraceRecorder:
    """把一个会话的事件流写成 JSONL 证据文件。

    线程/任务安全：写入用 ``threading.Lock`` 串行化（服务端事件来自同一事件
    循环，但 CLI / 后台线程也可能调用；加锁成本可忽略）。
    """

    def __init__(self, root: str | Path | None = None, enabled: bool | None = None,
                 max_run_bytes: int | None = None, max_runs: int | None = None) -> None:
        cfg = _cfg()
        self.root = Path(root or cfg["dir"])
        self.enabled = cfg["enabled"] if enabled is None else bool(enabled)
        self.max_run_bytes = int(max_run_bytes or cfg["max_run_bytes"])
        self.max_runs = int(max_runs or cfg["max_runs"])
        self.max_total_bytes = int(cfg["max_total_bytes"])
        self._handles: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._written: dict[str, int] = {}
        self._dropped: dict[str, int] = {}
        #: 便于自检与报告：最近一次落盘的文件路径
        self.last_path: str = ""
        self.write_errors = 0

    # ── 路径 ────────────────────────────────────────────────

    @staticmethod
    def _safe(part: str) -> str:
        keep = [c for c in str(part) if c.isalnum() or c in "-_.@"]
        return ("".join(keep) or "default")[:64]

    def path_for(self, session_id: str, run_id: str) -> Path:
        return self.root / self._safe(session_id) / f"{self._safe(run_id)}.jsonl"

    # ── 写入 ────────────────────────────────────────────────

    def record(self, session_id: str, run_id: str, event: dict[str, Any]) -> str:
        """追加一条事件；返回落盘路径（禁用/失败时返回空串）。"""
        if not self.enabled or not isinstance(event, dict):
            return ""
        path = self.path_for(session_id or "default", run_id or "run")
        key = str(path)
        try:
            line = json.dumps({
                "ts": round(time.time(), 3),
                "session_id": session_id or "default",
                "run_id": run_id or "run",
                **_sanitize(event),
            }, ensure_ascii=False)
        except Exception as e:                       # 序列化失败不该丢事件
            line = json.dumps({"ts": round(time.time(), 3), "type": "trace_encode_error",
                               "error": str(e)}, ensure_ascii=False)
        with self._lock:
            n = self._written.get(key, 0)
            if n and n + len(line) > self.max_run_bytes:
                # 超上限：只记一次"开始丢弃"，之后不再写正文（避免反复写标记）
                self._dropped[key] = self._dropped.get(key, 0) + 1
                if self._dropped[key] == 1:
                    self._append_raw(path, key, json.dumps({
                        "ts": round(time.time(), 3), "type": "trace_truncated",
                        "message": (f"轨迹文件已达上限 {self.max_run_bytes} 字节，"
                                    f"后续事件只计数不再写入正文。")},
                        ensure_ascii=False))
                return key
            ok = self._append_raw(path, key, line)
            if not ok:
                return ""
        self.last_path = key
        return key

    def _append_raw(self, path: Path, key: str, line: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._written[key] = self._written.get(key, 0) + len(line) + 1
            return True
        except Exception as e:
            self.write_errors += 1
            # 只警告前 3 次，避免磁盘故障时日志刷屏（轨迹问题不得淹没主日志）
            if self.write_errors <= 3:
                logger.warning("trace_write_failed", path=str(path), error=str(e))
            return False

    # ── 收尾与轮转 ──────────────────────────────────────────

    def finish(self, session_id: str, run_id: str, summary: dict[str, Any] | None = None) -> str:
        """写一条 run_end 收尾记录并做一次轮转；返回落盘路径。"""
        path = self.record(session_id, run_id, {
            "type": "trace_end", "summary": _sanitize(summary or {})})
        self.prune(session_id)
        return path or self.path_for(session_id or "default", run_id or "run").as_posix()

    def dropped(self, session_id: str, run_id: str) -> int:
        return self._dropped.get(str(self.path_for(session_id, run_id)), 0)

    def prune(self, session_id: str | None = None) -> dict[str, int]:
        """按"每会话保留 N 个 run"与"全局字节配额"淘汰最旧轨迹文件。"""
        removed, freed = 0, 0
        try:
            sessions = ([self.root / self._safe(session_id)] if session_id
                        else [d for d in self.root.iterdir() if d.is_dir()]
                        if self.root.exists() else [])
            for d in sessions:
                files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                               reverse=True)
                for old in files[self.max_runs:]:
                    freed += old.stat().st_size
                    old.unlink(missing_ok=True)
                    removed += 1
            if self.root.exists():
                allf = sorted(self.root.rglob("*.jsonl"),
                              key=lambda p: p.stat().st_mtime)
                total = sum(p.stat().st_size for p in allf)
                for p in allf:
                    if total <= self.max_total_bytes:
                        break
                    size = p.stat().st_size
                    p.unlink(missing_ok=True)
                    total -= size
                    freed += size
                    removed += 1
        except Exception as e:
            logger.warning("trace_prune_failed", error=str(e))
        return {"removed": removed, "freed_bytes": freed}

    def list_runs(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """列出该会话已落盘的轨迹文件（新的在前）。"""
        d = self.root / self._safe(session_id)
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime,
                        reverse=True)[:limit]:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "run_id": p.stem, "bytes": st.st_size,
                "updated_at": round(st.st_mtime, 3), "path": str(p),
            })
        return out

    def tail(self, session_id: str, run_id: str, limit: int = 200,
             event_types: list[str] | None = None) -> list[dict[str, Any]]:
        """读取某次运行的最后 N 条事件（可按类型过滤）。"""
        p = self.path_for(session_id, run_id)
        if not p.is_file():
            return []
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            logger.warning("trace_read_failed", path=str(p), error=str(e))
            return []
        want = {t for t in (event_types or []) if t}
        out: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if want and str(obj.get("type") or "") not in want:
                continue
            out.append(obj)
        return out[-limit:]

    def stats(self) -> dict[str, Any]:
        """轨迹占用概览（供 /api/observe/traces 与健康检查展示）。"""
        runs, total = 0, 0
        if self.root.exists():
            for p in self.root.rglob("*.jsonl"):
                try:
                    total += p.stat().st_size
                    runs += 1
                except OSError:
                    continue
        return {
            "enabled": self.enabled, "dir": str(self.root), "runs": runs,
            "bytes": total, "sessions": len([d for d in self.root.iterdir()
                                             if d.is_dir()]) if self.root.exists() else 0,
            "write_errors": self.write_errors,
        }


#: 进程级默认记录器（懒构造：读配置依赖导入顺序，首次用时再建）
_default: TraceRecorder | None = None


def get_recorder() -> TraceRecorder:
    """返回进程级轨迹记录器（首次调用时按配置构造）。"""
    global _default
    if _default is None:
        _default = TraceRecorder()
    return _default


def reset_for_tests(root: str | Path | None = None, enabled: bool = True) -> TraceRecorder:
    """重建进程级记录器（测试用：把轨迹写到临时目录）。"""
    global _default
    _default = TraceRecorder(root=root, enabled=enabled)
    return _default
