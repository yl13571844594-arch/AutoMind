"""数据工具 —— 数据库查询 / 文件检索 / 归档解压。

安全要点（三个工具各有一处必须守住的地方）：
  · db_query —— 社区版**只读 SQLite**，SQL 由模型生成，一律走参数化绑定；
    写操作与非 SQLite 数据库属 ``data_pro``。
  · file_search —— 限定在项目根之内，不给绝对路径漫游。
  · archive —— 解压必须防 zip-slip（归档成员名是攻击者可控的 ``../../``）。
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import (
    BlockedTarget,
    bad,
    err,
    ok,
    require,
    safe_extract_path,
)
from automind.tools.base import AbstractTool

# ── db_query ────────────────────────────────────────────────

#: 只读语句前缀。其余（INSERT/UPDATE/DELETE/DROP/ATTACH…）属写操作
_READ_ONLY = ("select", "with", "explain", "pragma table_info", "pragma table_list")


class DbQueryTool(AbstractTool):
    """查询数据库。社区版：只读 SQLite。"""

    name = "db_query"
    description = (
        "Query a database and return rows. Community edition supports read-only "
        "SQLite (SELECT/WITH/EXPLAIN plus schema introspection). "
        "Write statements and non-SQLite engines (PostgreSQL/MySQL/SQL Server) "
        "require the Pro edition. Always pass user values via `params`, never by "
        "string-formatting them into `sql`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "database": {"type": "string", "description": "Path to the SQLite .db file."},
            "sql": {"type": "string", "description": "SQL statement (use ? placeholders)."},
            "params": {
                "type": "array",
                "description": "Values bound to the ? placeholders, in order.",
            },
            "max_rows": {"type": "number", "description": "Row cap (default 200, max 2000)."},
            "action": {
                "type": "string",
                "enum": ["query", "tables", "schema"],
                "description": "query (default), tables (list tables), schema (columns of a table).",
            },
            "table": {"type": "string", "description": "Table name for the schema action."},
        },
        "required": ["database"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "query").lower()
        db = Path(str(kwargs.get("database", ""))).expanduser()
        try:
            if not db.is_file():
                return bad(self.name, f"数据库文件不存在：{db}")
            cap = max(1, min(int(kwargs.get("max_rows") or 200), 2000))

            # file:...?mode=ro —— 由 SQLite 自己保证只读，比在应用层猜 SQL 更可靠
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                if action == "tables":
                    rows = conn.execute(
                        "SELECT name, type FROM sqlite_master "
                        "WHERE type IN ('table','view') ORDER BY name").fetchall()
                    return ok(self.name, database=str(db),
                              tables=[dict(r) for r in rows], count=len(rows))
                if action == "schema":
                    tbl = str(kwargs.get("table") or "")
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tbl):
                        return bad(self.name, "table 名称非法（只允许字母数字下划线）")
                    # 表名不能参数化绑定，故用严格正则白名单代替
                    rows = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    if not rows:
                        return bad(self.name, f"表不存在或无列信息：{tbl}")
                    return ok(self.name, database=str(db), table=tbl,
                              columns=[dict(r) for r in rows])

                sql = str(kwargs.get("sql") or "").strip()
                if not sql:
                    return bad(self.name, "缺少 sql")
                low = sql.lower().lstrip("( \n\t")
                if not any(low.startswith(p) for p in _READ_ONLY):
                    require("data_pro")     # 写操作是专业版能力
                params = list(kwargs.get("params") or [])
                cur = conn.execute(sql, params)
                rows = cur.fetchmany(cap)
                cols = [d[0] for d in (cur.description or [])]
                return ok(self.name, database=str(db), columns=cols,
                          rows=[list(r) for r in rows], row_count=len(rows),
                          truncated=len(rows) == cap)
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            # 只读连接下的写操作会走到这里，给出可操作的说明
            msg = str(e)
            if "readonly" in msg.lower():
                return bad(self.name, "当前为只读连接，写操作需专业版（data_pro）")
            return bad(self.name, f"SQL 执行失败：{msg}")
        except Exception as e:
            return err(self.name, e)


# ── file_search ─────────────────────────────────────────────

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}
_TEXT_SUFFIX = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c",
                ".cpp", ".h", ".cs", ".rb", ".php", ".sh", ".ps1", ".sql", ".md",
                ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html",
                ".css", ".xml", ".csv"}


class FileSearchTool(AbstractTool):
    """按文件名或内容检索项目文件。"""

    name = "file_search"
    description = (
        "Find files by name pattern and/or search their contents with a regex. "
        "Scoped to the project directory; build/vendor directories are skipped. "
        "Returns matching paths with line numbers and matched lines."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Filename glob, e.g. '*.py'."},
            "contains": {"type": "string", "description": "Regex to search inside files."},
            "path": {"type": "string", "description": "Subdirectory to search (relative to project root)."},
            "max_results": {"type": "number", "description": "Default 100, max 1000."},
            "ignore_case": {"type": "boolean"},
        },
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 5

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            root = self.project_root
            sub = str(kwargs.get("path") or "").strip()
            if sub:
                target = (root / sub).resolve()
                # 限定在项目根内 —— 与 file_read/file_write 同一套边界
                if target != root and root not in target.parents:
                    return bad(self.name, "path 超出项目目录范围")
                root = target
            if not root.is_dir():
                return bad(self.name, f"目录不存在：{root}")

            glob_pat = str(kwargs.get("pattern") or "*")
            cap = max(1, min(int(kwargs.get("max_results") or 100), 1000))
            rx = None
            if kwargs.get("contains"):
                flags = re.IGNORECASE if kwargs.get("ignore_case") else 0
                try:
                    rx = re.compile(str(kwargs["contains"]), flags)
                except re.error as e:
                    return bad(self.name, f"正则表达式无效：{e}")

            hits: list[dict] = []
            scanned = 0
            for p in root.rglob("*"):
                if len(hits) >= cap:
                    break
                if not p.is_file() or any(d in _SKIP_DIRS for d in p.parts):
                    continue
                if not fnmatch.fnmatch(p.name, glob_pat):
                    continue
                rel = str(p.relative_to(self.project_root))
                if rx is None:
                    hits.append({"path": rel, "size": p.stat().st_size})
                    continue
                if p.suffix.lower() not in _TEXT_SUFFIX or p.stat().st_size > 5_000_000:
                    continue                  # 不去正则匹配二进制/超大文件
                scanned += 1
                try:
                    for i, line in enumerate(
                            p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if rx.search(line):
                            hits.append({"path": rel, "line": i, "text": line.strip()[:300]})
                            if len(hits) >= cap:
                                break
                except OSError:
                    continue
            return ok(self.name, root=str(root), matches=hits, count=len(hits),
                      files_scanned=scanned, truncated=len(hits) >= cap)
        except Exception as e:
            return err(self.name, e)


# ── archive ─────────────────────────────────────────────────

class ArchiveTool(AbstractTool):
    """打包与解包（zip / tar.gz）。"""

    name = "archive"
    description = (
        "Create and extract archives (.zip, .tar, .tar.gz). "
        "Actions: create, extract, list. Extraction is protected against path "
        "traversal (zip-slip) — members resolving outside the target directory are rejected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "extract", "list"]},
            "path": {"type": "string", "description": "Archive path."},
            "sources": {
                "type": "array", "items": {"type": "string"},
                "description": "Files/directories to pack (create).",
            },
            "dest": {"type": "string", "description": "Destination directory (extract)."},
            "format": {"type": "string", "enum": ["zip", "tar.gz", "tar"],
                       "description": "Archive format for create (default: infer from path)."},
        },
        "required": ["action", "path"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 35
    #: 解压炸弹防线：单文件与总解压体积上限
    MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        path = Path(str(kwargs.get("path", ""))).expanduser()
        try:
            if action == "create":
                return self._create(path, kwargs)
            if not path.is_file():
                return bad(self.name, f"归档文件不存在：{path}")
            if action == "list":
                return self._list(path)
            if action == "extract":
                return self._extract(path, kwargs)
            return bad(self.name, f"不支持的 action：{action}")
        except BlockedTarget as e:
            return bad(self.name, str(e), blocked=True)
        except Exception as e:
            return err(self.name, e)

    @staticmethod
    def _is_tar(path: Path) -> bool:
        return path.suffix.lower() in (".tar", ".tgz") or path.name.lower().endswith(
            (".tar.gz", ".tar.bz2", ".tar.xz"))

    def _create(self, path: Path, kw: dict) -> ToolResult:
        sources = [Path(str(s)).expanduser() for s in (kw.get("sources") or [])]
        if not sources:
            return bad(self.name, "create 需要提供 sources")
        missing = [str(s) for s in sources if not s.exists()]
        if missing:
            return bad(self.name, f"以下路径不存在：{', '.join(missing)}")
        fmt = str(kw.get("format") or ("tar.gz" if self._is_tar(path) else "zip"))
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        if fmt == "zip":
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for s in sources:
                    if s.is_dir():
                        for f in s.rglob("*"):
                            if f.is_file():
                                z.write(f, f.relative_to(s.parent))
                                n += 1
                    else:
                        z.write(s, s.name)
                        n += 1
        else:
            mode = "w:gz" if fmt == "tar.gz" else "w"
            with tarfile.open(path, mode) as t:
                for s in sources:
                    t.add(s, arcname=s.name)
                    n += 1
        return ok(self.name, path=str(path), format=fmt, entries=n,
                  size=path.stat().st_size,
                  message=f"已打包 {n} 个条目 → {path.name}")

    def _list(self, path: Path) -> ToolResult:
        if self._is_tar(path):
            with tarfile.open(path) as t:
                names = [{"name": m.name, "size": m.size} for m in t.getmembers()]
        else:
            with zipfile.ZipFile(path) as z:
                names = [{"name": i.filename, "size": i.file_size} for i in z.infolist()]
        return ok(self.name, path=str(path), entries=names[:2000], count=len(names))

    def _extract(self, path: Path, kw: dict) -> ToolResult:
        dest = Path(str(kw.get("dest") or path.parent / path.stem)).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        extracted, total = [], 0

        if self._is_tar(path):
            with tarfile.open(path) as t:
                members = t.getmembers()
                for m in members:
                    # 逐个校验：目录穿越 + 符号链接逃逸（tar 特有，zip 一般没有）
                    safe_extract_path(dest, m.name)
                    if m.issym() or m.islnk():
                        raise BlockedTarget(f"归档含链接项，已拒绝解压：{m.name}")
                    total += m.size
                    if total > self.MAX_TOTAL_BYTES:
                        raise BlockedTarget("解压后体积超过上限，疑似压缩炸弹，已中止")
                for m in members:
                    t.extract(m, dest)
                    extracted.append(m.name)
        else:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                for i in infos:
                    safe_extract_path(dest, i.filename)
                    total += i.file_size
                    if total > self.MAX_TOTAL_BYTES:
                        raise BlockedTarget("解压后体积超过上限，疑似压缩炸弹，已中止")
                for i in infos:
                    z.extract(i, dest)
                    extracted.append(i.filename)

        return ok(self.name, path=str(path), dest=str(dest),
                  extracted=extracted[:500], count=len(extracted),
                  bytes=total, message=f"已解压 {len(extracted)} 个条目 → {dest}")
