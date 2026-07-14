"""RAG 知识库 — 上传文档、自动分段 + embedding、对话中自动检索。

社区版（免费）：
    - 上传 PDF / Word(.docx) / Markdown / TXT 到默认知识库
    - 自动分段 + 特征哈希 embedding（离线可用；装了 chromadb 则用 chromadb 持久化）
    - Agent 在对话中自动检索相关片段注入上下文
    - 限额：5 个文档 / 10MB 总量 / 单一知识库

专业版（rag_pro 特性，由 automind-pro 解锁）：
    - 无限文档 / 200MB 总量
    - 多知识库管理（按主题分类）
    - Reranker 二阶段重排（精度提升）
    - 引用溯源（回答标注来源文档与段落）
    - 定时自动重新 embedding
    - 可选 Milvus / Pinecone / Qdrant 向量后端
"""

from automind.rag.kb import KnowledgeStore, get_store

__all__ = ["KnowledgeStore", "get_store"]
