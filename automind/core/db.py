"""SQLite 持久化层 — v1.1 起替代零散 JSON 平面文件。

动机：任务历史 / 对话会话 / 团队任务 / 限额 / 知识库片段此前各自维护
JSON 文件（全量重写、并发脆弱、量大后 IO 放大）。统一收敛到单个
SQLite 库 ``.automind/automind.db``（WAL 模式，进程内线程安全）：

    kv               通用键值（限额计数 / 知识库设置与热度 / 迁移标记）
    task_history     任务历史（滚动上限由调用方控制）
    chat_sessions    多用户对话会话（sid → messages JSON）
    team_tasks       团队任务看板
    kb_kbs / kb_docs / kb_chunks   知识库（库 / 文档 / 片段+向量）
    kb_search_log    知识库检索审计日志（企业版）

迁移策略（零感知）：首次打开时若对应表为空且旧 JSON 文件存在，自动导入；
旧文件保留原地作为备份，不删除。**配置与 API Key 仍在
``.automind_config.json``**（体量小、用户会手工编辑，保持纯文本可读）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_history (
    pos    INTEGER PRIMARY KEY AUTOINCREMENT,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    sid      TEXT PRIMARY KEY,
    messages TEXT NOT NULL,
    updated  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS team_tasks (
    pos  INTEGER PRIMARY KEY AUTOINCREMENT,
    id   TEXT UNIQUE NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_kbs (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_docs (
    id     TEXT PRIMARY KEY,
    kb     TEXT NOT NULL,
    name   TEXT NOT NULL,
    size   INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    time   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id     TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    kb     TEXT NOT NULL,
    seq    INTEGER NOT NULL DEFAULT 0,
    text   TEXT NOT NULL,
    vec    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_kb  ON kb_chunks(kb);
CREATE TABLE IF NOT EXISTS kb_search_log (
    pos    INTEGER PRIMARY KEY AUTOINCREMENT,
    time   TEXT NOT NULL,
    source TEXT NOT NULL,
    query  TEXT NOT NULL,
    hits   TEXT NOT NULL
);
"""


class Database:
    """线程安全的 SQLite 包装（单连接 + 互斥锁，WAL 模式）。"""

    def __init__(self, path: str | Path = Path(".automind") / "automind.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── 基础操作 ────────────────────────────────────────
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 键值 ────────────────────────────────────────────
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0][0])
        except Exception:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)))

    # ── 任务历史 ────────────────────────────────────────
    def history_load(self) -> list[dict]:
        return [json.loads(r[0]) for r in
                self.query("SELECT record FROM task_history ORDER BY pos")]

    def history_append(self, record: dict, cap: int = 200) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO task_history(record) VALUES(?)",
                               (json.dumps(record, ensure_ascii=False),))
            self._conn.execute(
                "DELETE FROM task_history WHERE pos NOT IN "
                "(SELECT pos FROM task_history ORDER BY pos DESC LIMIT ?)", (cap,))
            self._conn.commit()

    def history_replace(self, records: list[dict]) -> None:
        """整表重写（删除单条 / 清空后同步内存态用）。"""
        with self._lock:
            self._conn.execute("DELETE FROM task_history")
            self._conn.executemany(
                "INSERT INTO task_history(record) VALUES(?)",
                [(json.dumps(r, ensure_ascii=False),) for r in records])
            self._conn.commit()

    # ── 对话会话 ────────────────────────────────────────
    def session_load(self, sid: str) -> list | None:
        rows = self.query("SELECT messages FROM chat_sessions WHERE sid=?", (sid,))
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except Exception:
            return None

    def session_save(self, sid: str, messages: list) -> None:
        self.execute(
            "INSERT INTO chat_sessions(sid,messages,updated) "
            "VALUES(?,?,datetime('now','localtime')) "
            "ON CONFLICT(sid) DO UPDATE SET messages=excluded.messages, "
            "updated=excluded.updated",
            (sid, json.dumps(messages, ensure_ascii=False)))

    def session_delete(self, sid: str) -> None:
        self.execute("DELETE FROM chat_sessions WHERE sid=?", (sid,))

    # ── 团队任务 ────────────────────────────────────────
    def team_load(self) -> list[dict]:
        return [json.loads(r[0]) for r in
                self.query("SELECT data FROM team_tasks ORDER BY pos")]

    def team_replace(self, items: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM team_tasks")
            self._conn.executemany(
                "INSERT INTO team_tasks(id,data) VALUES(?,?)",
                [(str(t.get("id", i)), json.dumps(t, ensure_ascii=False))
                 for i, t in enumerate(items)])
            self._conn.commit()


# ── 进程级单例 ──────────────────────────────────────────

_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database()
        return _db


def reset_for_tests(path: str | Path | None = None) -> Database | None:
    """测试用：关闭并重建（可指向临时路径）。传 None 仅重置单例。"""
    global _db
    with _db_lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
        _db = Database(path) if path else None
        return _db


def migrate_json_once(db: Database, flag: str, legacy: Path,
                      importer) -> bool:
    """一次性 JSON → SQLite 迁移：目标为空且旧文件存在时导入。

    ``importer(data)`` 负责写库；旧文件保留原地作为备份。返回是否执行了迁移。
    """
    if db.kv_get("migrated:" + flag):
        return False
    db.kv_set("migrated:" + flag, True)   # 先置位：失败也不反复重试
    if not legacy.exists():
        return False
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("db_migrate_read_failed", flag=flag, error=str(e))
        return False
    try:
        importer(data)
        logger.info("db_migrated", flag=flag, source=str(legacy))
        return True
    except Exception as e:
        logger.warning("db_migrate_failed", flag=flag, error=str(e))
        return False
