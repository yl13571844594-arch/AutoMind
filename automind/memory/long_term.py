"""长期记忆 — ChromaDB 向量存储 + RAG 检索。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.core.logging import get_logger

_logger = get_logger("automind.memory.long_term")


class LongTermMemory:
    """长期记忆 — 基于 ChromaDB 的向量存储。

    存储:
        - 历史交互 (用户问题 + Agent 回答)
        - 经验教训 (Reflexion)
        - 代码片段与模式

    特性:
        - 相似度搜索 (RAG)
        - 元数据过滤
        - 自动降级为内存模式 (无需 ChromaDB)
    """

    def __init__(
        self,
        persist_dir: str = ".automind/chroma",
        embedding_fn: Any = None,
        collection_name: str = "automind_memory",
    ) -> None:
        self._persist_dir = persist_dir
        self._embedding_fn = embedding_fn or _SimpleEmbedder()
        self._collection_name = collection_name
        self._collection = None
        self._client = None  # 持有 chromadb client 引用，供 close() 显式释放
        self._in_memory_store: list[dict[str, Any]] = []
        self._chroma_available = self._init_chroma()

    def _init_chroma(self) -> bool:
        try:
            import chromadb
            Path(self._persist_dir).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self._persist_dir)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            return True
        except ImportError:
            _logger.info("chroma_unavailable", reason="chromadb 未安装，降级为内存向量存储")
            return False
        except Exception as e:
            _logger.warning("chroma_init_failed", persist_dir=self._persist_dir, error=str(e))
            return False

    def close(self) -> None:
        """释放 ChromaDB 连接与内存存储（幂等，可重复调用）。"""
        self._collection = None
        if self._client is not None:
            try:
                # chromadb PersistentClient 数据已随写入持久化；
                # reset 系统缓存句柄（部分版本无此 API，忽略即可）
                self._client.clear_system_cache()  # type: ignore[attr-defined]
            except Exception as e:
                _logger.debug("chroma_cache_clear_skipped", error=str(e))
            self._client = None
        self._chroma_available = False
        self._in_memory_store.clear()

    async def add(
        self,
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """添加文档到长期记忆。

        Returns:
            文档 ID 列表。
        """
        if not ids:
            import uuid
            ids = [uuid.uuid4().hex[:16] for _ in documents]

        if self._chroma_available and self._collection:
            try:
                embeddings = [self._embedding_fn.embed(d) for d in documents]
                self._collection.add(
                    documents=documents,
                    # B-08 修复：[{}] * n 会让所有元素共享同一个 dict，
                    # 改用列表推导为每个文档生成独立 metadata。
                    metadatas=metadatas or [{} for _ in documents],
                    ids=ids,
                    embeddings=embeddings,
                )
                return ids
            except Exception as e:
                _logger.warning("chroma_add_failed", count=len(documents),
                                error=str(e), fallback="memory")

        # 内存模式
        for i, doc in enumerate(documents):
            self._in_memory_store.append({
                "id": ids[i],
                "document": doc,
                "metadata": metadatas[i] if metadatas else {},
                "embedding": self._embedding_fn.embed(doc),
            })
        return ids

    async def search(
        self,
        query: str,
        k: int = 5,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """相似度搜索 (RAG 检索)。

        Returns:
            匹配的文档列表 [{id, document, metadata, score}]
        """
        if self._chroma_available and self._collection:
            try:
                query_embedding = self._embedding_fn.embed(query)
                results = self._collection.query(
                    query_embeddings=[query_embedding],
                    n_results=k,
                    where=filter_metadata,
                )
                return [
                    {
                        "id": results["ids"][0][i],
                        "document": results["documents"][0][i],
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "score": 1 - results["distances"][0][i] if results["distances"] else 0,
                    }
                    for i in range(len(results["ids"][0]))
                ]
            except Exception as e:
                _logger.warning("chroma_search_failed", error=str(e), fallback="memory")

        # 内存模式 — 余弦相似度
        query_emb = self._embedding_fn.embed(query)
        scored = []
        for entry in self._in_memory_store:
            score = self._cosine_sim(query_emb, entry["embedding"])
            scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": e["id"], "document": e["document"], "metadata": e["metadata"], "score": s}
            for s, e in scored[:k]
        ]

    async def delete(self, ids: list[str]) -> None:
        """删除文档。"""
        if self._chroma_available and self._collection:
            try:
                self._collection.delete(ids=ids)
            except Exception as e:
                _logger.warning("chroma_delete_failed", count=len(ids), error=str(e))
        self._in_memory_store = [
            e for e in self._in_memory_store if e["id"] not in ids
        ]

    async def update(
        self,
        id_: str,
        document: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新文档。"""
        await self.delete([id_])
        await self.add([document], [metadata or {}], [id_])

    def count(self) -> int:
        """返回存储的文档数量。"""
        if self._chroma_available and self._collection:
            try:
                return self._collection.count()
            except Exception as e:
                _logger.warning("chroma_count_failed", error=str(e))
        return len(self._in_memory_store)

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


class _SimpleEmbedder:
    """特征哈希（feature hashing）嵌入器 — 真实可用的离线语义嵌入。

    相比旧实现（对整串做一次 SHA256，任何 1 字符差异即得完全不同向量、
    余弦相似度失去意义），本实现将文本切成 **词 + 字符三元组** 词元，
    经带符号的哈希技巧散布到固定维向量并做 L2 归一化：
    共享词元越多，余弦相似度越高——即真正反映文本相似性（HashingVectorizer 同理）。

    完全确定、离线、无需 API Key。若需神经网络嵌入，可通过
    ``LongTermMemory(embedding_fn=...)`` 注入实现了 ``.embed(text)->list[float]`` 的对象。
    """

    _DIM = 256

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    @staticmethod
    def _tokens(text: str) -> list[str]:
        import re
        text = (text or "").lower()
        words = re.findall(r"[\w一-鿿]+", text)
        toks = list(words)
        # 字符三元组：捕获拼写/子词相似性（对中文亦有效）
        compact = "".join(words)
        toks += [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
        return toks

    def embed(self, text: str) -> list[float]:
        import hashlib
        vec = [0.0] * self._dim
        for tok in self._tokens(text):
            h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "big")
            idx = h % self._dim
            sign = 1.0 if (h >> 8) & 1 else -1.0  # 带符号哈希，降低碰撞偏置
            vec[idx] += sign
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec
