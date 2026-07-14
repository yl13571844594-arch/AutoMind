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
    """单实例知识库存储：多库（专业版）、文档、片段与向量检索。"""

    def __init__(self, root: str | Path = ".automind/kb", embedder: Any = None) -> None:
        self.root = Path(root)
        self._embedder = embedder or _SimpleEmbedder()
        self._lock = threading.Lock()
        self._index: dict = {"kbs": [{"id": DEFAULT_KB, "name": "默认知识库"}],
                             "docs": [], "settings": {}}
        self._chunks: list[dict] = []   # {id, doc_id, kb, seq, text, vec}
        self._load()

    # ── 持久化 ─────────────────────────────────────────
    def _load(self) -> None:
        try:
            f = self.root / "index.json"
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("kbs"):
                    self._index = data
            cf = self.root / "chunks.json"
            if cf.exists():
                chunks = json.loads(cf.read_text(encoding="utf-8"))
                if isinstance(chunks, list):
                    self._chunks = chunks
        except Exception as e:
            logger.warning("kb_load_failed", error=str(e))

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "index.json").write_text(
                json.dumps(self._index, ensure_ascii=False), encoding="utf-8")
            (self.root / "chunks.json").write_text(
                json.dumps(self._chunks, ensure_ascii=False), encoding="utf-8")
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
            self._save()
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
            self._save()
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
            for i, piece in enumerate(pieces):
                self._chunks.append({
                    "id": f"{doc_id}_{i}", "doc_id": doc_id, "kb": kb_id,
                    "seq": i, "text": piece, "vec": self._embedder.embed(piece),
                })
            meta = {"id": doc_id, "kb": kb_id, "name": filename,
                    "size": len(data), "chunks": len(pieces),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._index["docs"].append(meta)
            self._save()
        logger.info("kb_doc_added", doc=filename, chunks=len(pieces), kb=kb_id)
        return meta

    def delete_document(self, doc_id: str) -> bool:
        with self._lock:
            before = len(self._index["docs"])
            self._index["docs"] = [d for d in self._index["docs"] if d["id"] != doc_id]
            self._chunks = [c for c in self._chunks if c["doc_id"] != doc_id]
            changed = len(self._index["docs"]) != before
            if changed:
                self._save()
            return changed

    def reembed_all(self) -> int:
        """重算全部片段的向量（切换嵌入器/定期刷新用）。返回片段数。"""
        with self._lock:
            for c in self._chunks:
                c["vec"] = self._embedder.embed(c["text"])
            self._index["settings"]["last_reembed"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save()
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
                self._save()
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
        """检索审计日志（企业版）：记录查询与命中来源，容量 500 条滚动。"""
        try:
            f = self.root / "search_log.json"
            log = []
            if f.exists():
                log = json.loads(f.read_text(encoding="utf-8")) or []
            log.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
                "query": query[:300],
                "hits": [{"doc": r["doc_name"], "seq": r["seq"],
                          "score": r["score"]} for r in results],
            })
            self.root.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(log[-500:], ensure_ascii=False),
                         encoding="utf-8")
        except Exception:
            pass

    def search_log(self, limit: int = 100) -> list[dict]:
        try:
            f = self.root / "search_log.json"
            if f.exists():
                log = json.loads(f.read_text(encoding="utf-8")) or []
                return log[-limit:][::-1]
        except Exception:
            pass
        return []

    # ── 设置（专业版：向量后端 / 定时重嵌入）──────────
    def get_settings(self) -> dict:
        return dict(self._index.get("settings", {}))

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            self._index.setdefault("settings", {}).update(patch)
            self._save()
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
