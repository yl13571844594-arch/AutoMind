"""知识库存储与检索 — 分段 + 特征哈希 embedding + 余弦检索。

存储布局（``.automind/kb/``）：
    index.json   知识库清单 + 文档元数据 + 设置
    chunks.json  全部片段（text + 向量），JSON 为唯一事实源 —— 离线零依赖；
                 安装 chromadb 后自动同步写入 chroma 集合（加速大库检索）。

嵌入器复用 ``automind.memory.long_term._SimpleEmbedder``（特征哈希，
离线确定可用）；可通过 ``KnowledgeStore(embedder=...)`` 注入任何实现
``.embed(text) -> list[float]`` 的对象（如神经网络嵌入）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.memory.long_term import _SimpleEmbedder
from automind.rag.parser import chunk_text, extract_text

logger = get_logger("automind.rag")

DEFAULT_KB = "default"


class KnowledgeStore:
    """单实例知识库存储：多库（专业版）、文档、片段与向量检索。

    v1.1 起持久化为 SQLite（``<root>/kb.db``，WAL）；旧版 ``index.json`` /
    ``chunks.json`` 首次打开自动一次性导入并保留原文件作备份。
    内存中维护全量索引与片段（检索热路径零 IO），写操作增量落库。
    """

    def __init__(self, root: str | Path | None = None, embedder: Any = None) -> None:
        if root is None:
            from automind.core.paths import kb_dir
            root = kb_dir()
        self.root = Path(root)
        self._embedder = embedder or _SimpleEmbedder()
        self._lock = threading.Lock()
        self._index: dict = {"kbs": [{"id": DEFAULT_KB, "name": "默认知识库"}],
                             "docs": [], "settings": {}}
        self._chunks: list[dict] = []   # {id, doc_id, kb, seq, text, vec}
        from automind.core.db import Database
        self._db = Database(self.root / "kb.db")
        self._load()

    # ── 持久化（SQLite + 旧 JSON 一次性迁移）───────────
    def _load(self) -> None:
        try:
            self._migrate_legacy_json()
            kbs = [{"id": r[0], "name": r[1]}
                   for r in self._db.query("SELECT id,name FROM kb_kbs ORDER BY rowid")]
            if not any(k["id"] == DEFAULT_KB for k in kbs):
                kbs.insert(0, {"id": DEFAULT_KB, "name": "默认知识库"})
                self._db.execute("INSERT OR IGNORE INTO kb_kbs(id,name) VALUES(?,?)",
                                 (DEFAULT_KB, "默认知识库"))
            docs = [{"id": r[0], "kb": r[1], "name": r[2], "size": r[3],
                     "chunks": r[4], "time": r[5]}
                    for r in self._db.query(
                        "SELECT id,kb,name,size,chunks,time FROM kb_docs ORDER BY rowid")]
            self._index = {
                "kbs": kbs, "docs": docs,
                "settings": self._db.kv_get("kb:settings", {}) or {},
                "hit_stats": self._db.kv_get("kb:hit_stats", {}) or {},
                "search_count": self._db.kv_get("kb:search_count", 0) or 0,
            }
            self._chunks = [
                {"id": r[0], "doc_id": r[1], "kb": r[2], "seq": r[3],
                 "text": r[4], "vec": json.loads(r[5])}
                for r in self._db.query(
                    "SELECT id,doc_id,kb,seq,text,vec FROM kb_chunks ORDER BY rowid")]
        except Exception as e:
            logger.warning("kb_load_failed", error=str(e))

    def _migrate_legacy_json(self) -> None:
        """旧 index.json / chunks.json → SQLite（一次性；旧文件保留作备份）。"""
        if self._db.kv_get("migrated:kb"):
            return
        self._db.kv_set("migrated:kb", True)
        idx_f, chk_f = self.root / "index.json", self.root / "chunks.json"
        if not idx_f.exists():
            return
        try:
            index = json.loads(idx_f.read_text(encoding="utf-8")) or {}
            chunks = (json.loads(chk_f.read_text(encoding="utf-8"))
                      if chk_f.exists() else []) or []
            for kb in index.get("kbs", []):
                self._db.execute("INSERT OR IGNORE INTO kb_kbs(id,name) VALUES(?,?)",
                                 (kb["id"], kb.get("name", kb["id"])))
            self._db.executemany(
                "INSERT OR IGNORE INTO kb_docs(id,kb,name,size,chunks,time) "
                "VALUES(?,?,?,?,?,?)",
                [(d["id"], d.get("kb", DEFAULT_KB), d.get("name", "?"),
                  d.get("size", 0), d.get("chunks", 0), d.get("time", ""))
                 for d in index.get("docs", [])])
            self._db.executemany(
                "INSERT OR IGNORE INTO kb_chunks(id,doc_id,kb,seq,text,vec) "
                "VALUES(?,?,?,?,?,?)",
                [(c["id"], c["doc_id"], c.get("kb", DEFAULT_KB), c.get("seq", 0),
                  c.get("text", ""), json.dumps(c.get("vec", [])))
                 for c in chunks])
            for key in ("settings", "hit_stats", "search_count"):
                if key in index:
                    self._db.kv_set("kb:" + key, index[key])
            logger.info("kb_migrated_to_sqlite",
                        docs=len(index.get("docs", [])), chunks=len(chunks))
        except Exception as e:
            logger.warning("kb_migrate_failed", error=str(e))

    def _save_meta(self) -> None:
        """轻量元数据（设置/热度/检索计数）落库。"""
        try:
            self._db.kv_set("kb:settings", self._index.get("settings", {}))
            self._db.kv_set("kb:hit_stats", self._index.get("hit_stats", {}))
            self._db.kv_set("kb:search_count", self._index.get("search_count", 0))
        except Exception as e:
            logger.warning("kb_save_failed", error=str(e))

    # ── 知识库管理（多库为专业版能力，路由层做门控）──────
    def list_kbs(self) -> list[dict]:
        counts: dict[str, int] = {}
        sizes: dict[str, int] = {}
        for d in self._index["docs"]:
            counts[d["kb"]] = counts.get(d["kb"], 0) + 1
            sizes[d["kb"]] = sizes.get(d["kb"], 0) + d.get("size", 0)
        return [{**kb, "docs": counts.get(kb["id"], 0), "size": sizes.get(kb["id"], 0)}
                for kb in self._index["kbs"]]

    def create_kb(self, name: str) -> dict:
        with self._lock:
            kb = {"id": "kb_" + uuid.uuid4().hex[:8], "name": name.strip()[:40]}
            self._index["kbs"].append(kb)
            self._db.execute("INSERT INTO kb_kbs(id,name) VALUES(?,?)",
                             (kb["id"], kb["name"]))
            return kb

    def delete_kb(self, kb_id: str) -> int:
        """删除知识库及其全部文档；默认库不可删。返回删除的文档数。"""
        if kb_id == DEFAULT_KB:
            raise ValueError("默认知识库不可删除")
        with self._lock:
            before = len(self._index["docs"])
            self._index["kbs"] = [k for k in self._index["kbs"] if k["id"] != kb_id]
            self._index["docs"] = [d for d in self._index["docs"] if d["kb"] != kb_id]
            self._chunks = [c for c in self._chunks if c["kb"] != kb_id]
            self._db.execute("DELETE FROM kb_kbs WHERE id=?", (kb_id,))
            self._db.execute("DELETE FROM kb_docs WHERE kb=?", (kb_id,))
            self._db.execute("DELETE FROM kb_chunks WHERE kb=?", (kb_id,))
            return before - len(self._index["docs"])

    # ── 文档 ───────────────────────────────────────────
    def list_docs(self, kb_id: str | None = None) -> list[dict]:
        docs = self._index["docs"]
        if kb_id:
            docs = [d for d in docs if d["kb"] == kb_id]
        return list(docs)

    def total_size(self) -> int:
        return sum(d.get("size", 0) for d in self._index["docs"])

    def doc_count(self) -> int:
        return len(self._index["docs"])

    def add_document(self, filename: str, data: bytes, kb_id: str = DEFAULT_KB) -> dict:
        """解析 → 分段 → embedding → 入库。返回文档元数据。"""
        if not any(k["id"] == kb_id for k in self._index["kbs"]):
            raise ValueError(f"知识库不存在：{kb_id}")
        text = extract_text(filename, data)
        pieces = chunk_text(text)
        if not pieces:
            raise ValueError("文档未提取到有效内容")
        doc_id = "doc_" + uuid.uuid4().hex[:10]
        with self._lock:
            new_chunks = []
            for i, piece in enumerate(pieces):
                new_chunks.append({
                    "id": f"{doc_id}_{i}", "doc_id": doc_id, "kb": kb_id,
                    "seq": i, "text": piece, "vec": self._embedder.embed(piece),
                })
            self._chunks.extend(new_chunks)
            meta = {"id": doc_id, "kb": kb_id, "name": filename,
                    "size": len(data), "chunks": len(pieces),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._index["docs"].append(meta)
            self._db.execute(
                "INSERT INTO kb_docs(id,kb,name,size,chunks,time) VALUES(?,?,?,?,?,?)",
                (doc_id, kb_id, filename, len(data), len(pieces), meta["time"]))
            self._db.executemany(
                "INSERT INTO kb_chunks(id,doc_id,kb,seq,text,vec) VALUES(?,?,?,?,?,?)",
                [(c["id"], c["doc_id"], c["kb"], c["seq"], c["text"],
                  json.dumps(c["vec"])) for c in new_chunks])
        logger.info("kb_doc_added", doc=filename, chunks=len(pieces), kb=kb_id)
        return meta

    def delete_document(self, doc_id: str) -> bool:
        with self._lock:
            before = len(self._index["docs"])
            self._index["docs"] = [d for d in self._index["docs"] if d["id"] != doc_id]
            self._chunks = [c for c in self._chunks if c["doc_id"] != doc_id]
            changed = len(self._index["docs"]) != before
            if changed:
                self._db.execute("DELETE FROM kb_docs WHERE id=?", (doc_id,))
                self._db.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
            return changed

    def reembed_all(self) -> int:
        """重算全部片段的向量（切换嵌入器/定期刷新用）。返回片段数。"""
        with self._lock:
            for c in self._chunks:
                c["vec"] = self._embedder.embed(c["text"])
            self._db.executemany(
                "UPDATE kb_chunks SET vec=? WHERE id=?",
                [(json.dumps(c["vec"]), c["id"]) for c in self._chunks])
            self._index["settings"]["last_reembed"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_meta()
            return len(self._chunks)

    # ── 检索 ───────────────────────────────────────────
    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def search(self, query: str, top_k: int = 3, kb_id: str | None = None,
               min_score: float = 0.12, rerank: bool = False,
               hybrid: bool = False) -> list[dict]:
        """向量检索；``rerank=True``（专业版）做词面重叠二阶段重排；
        ``hybrid=True``（企业版）做向量 + 词法 BM25-lite 加权融合 ——
        关键词精确匹配与语义相似双通道召回，精度进一步提升。

        返回 [{text, score, doc_id, doc_name, seq, kb}]，按相关度降序。
        """
        qv = self._embedder.embed(query)
        q_toks = set(_SimpleEmbedder._tokens(query))
        pool = [c for c in self._chunks if (not kb_id or c["kb"] == kb_id)]

        def lex_score(c: dict) -> float:
            toks = set(_SimpleEmbedder._tokens(c["text"]))
            return len(q_toks & toks) / max(1, len(q_toks))

        if hybrid:
            # 融合打分：0.55 向量 + 0.45 词法（词法通道保证专名/代码精确命中）
            scored = [(0.55 * self._cosine(qv, c["vec"]) + 0.45 * lex_score(c), c)
                      for c in pool]
            scored = [(s, c) for s, c in scored if s >= min_score * 0.8]
        else:
            scored = [(self._cosine(qv, c["vec"]), c) for c in pool]
            scored = [(s, c) for s, c in scored if s >= min_score]
        scored.sort(key=lambda x: -x[0])
        cands = scored[: top_k * 4 if rerank else top_k]
        if rerank and cands:
            def rr(item):
                s, c = item
                return 0.7 * s + 0.3 * lex_score(c)
            cands.sort(key=rr, reverse=True)
            cands = cands[:top_k]
        names = {d["id"]: d["name"] for d in self._index["docs"]}
        results = [{"text": c["text"], "score": round(s, 4), "doc_id": c["doc_id"],
                    "doc_name": names.get(c["doc_id"], "?"), "seq": c["seq"],
                    "kb": c["kb"]}
                   for s, c in cands]
        self._record_hits(results)
        return results

    # ── 命中统计 / 检索日志（企业版分析与审计的数据源）────
    def _record_hits(self, results: list[dict]) -> None:
        try:
            with self._lock:
                stats = self._index.setdefault("hit_stats", {})
                for r in results:
                    stats[r["doc_id"]] = stats.get(r["doc_id"], 0) + 1
                self._index["search_count"] = self._index.get("search_count", 0) + 1
                self._save_meta()
        except Exception:
            pass

    def hit_stats(self) -> dict:
        """文档热度：每个文档被检索命中的次数 + 总检索次数。"""
        stats = self._index.get("hit_stats", {})
        names = {d["id"]: d["name"] for d in self._index["docs"]}
        return {
            "search_count": self._index.get("search_count", 0),
            "docs": sorted(
                ({"doc_id": k, "doc_name": names.get(k, "(已删除)"), "hits": v}
                 for k, v in stats.items()),
                key=lambda x: -x["hits"]),
        }

    def log_search(self, query: str, results: list[dict],
                   source: str = "api") -> None:
        """检索审计日志（企业版）：SQLite 表存储，容量 500 条滚动。"""
        try:
            hits = json.dumps(
                [{"doc": r["doc_name"], "seq": r["seq"], "score": r["score"]}
                 for r in results], ensure_ascii=False)
            self._db.execute(
                "INSERT INTO kb_search_log(time,source,query,hits) VALUES(?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), source, query[:300], hits))
            self._db.execute(
                "DELETE FROM kb_search_log WHERE pos NOT IN "
                "(SELECT pos FROM kb_search_log ORDER BY pos DESC LIMIT 500)")
        except Exception:
            pass

    def search_log(self, limit: int = 100) -> list[dict]:
        try:
            rows = self._db.query(
                "SELECT time,source,query,hits FROM kb_search_log "
                "ORDER BY pos DESC LIMIT ?", (limit,))
            return [{"time": r[0], "source": r[1], "query": r[2],
                     "hits": json.loads(r[3])} for r in rows]
        except Exception:
            return []

    # ── 设置（专业版：向量后端 / 定时重嵌入）──────────
    def get_settings(self) -> dict:
        return dict(self._index.get("settings", {}))

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            self._index.setdefault("settings", {}).update(patch)
            self._save_meta()
            return dict(self._index["settings"])


_store: KnowledgeStore | None = None


def get_store() -> KnowledgeStore:
    """进程级单例（服务端使用）。"""
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store


def reset_for_tests(root: str | Path | None = None) -> None:
    global _store
    _store = KnowledgeStore(root) if root else None
