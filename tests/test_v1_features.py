"""v1.0 新功能测试：版本限额 / RAG 知识库 / 语义缓存 / 模型路由。"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "pro"))

try:
    import automind_pro  # noqa: F401  商业扩展；仓库含 pro/ 时可用，社区 CI 无
    _HAS_PRO = True
except ImportError:
    _HAS_PRO = False

requires_pro = pytest.mark.skipif(
    not _HAS_PRO, reason="automind_pro 商业扩展未安装（社区 CI 跳过 Pro 特性测试）")

from automind.core import quota as quota_mod
from automind.rag.kb import KnowledgeStore
from automind.rag.parser import chunk_text, extract_text


@pytest.fixture(autouse=True)
def _reset_edition():
    yield
    quota_mod.reset_for_tests()


# ── 限额 ────────────────────────────────────────────────


class TestQuota:
    def test_community_rules(self):
        assert quota_mod.QUOTA_RULES["community"] == {"daily_tasks": 100, "workspaces": 3}
        assert quota_mod.QUOTA_RULES["pro"] == {"daily_tasks": None, "workspaces": 30}
        assert quota_mod.QUOTA_RULES["enterprise"] == {"daily_tasks": None, "workspaces": None}

    def test_consume_and_refund(self):
        quota_mod.reset_for_tests()
        used0 = quota_mod.tasks_used_today()
        ok, reason = quota_mod.try_consume_task()
        assert ok and reason == ""
        assert quota_mod.tasks_used_today() == used0 + 1
        quota_mod.refund_task()
        assert quota_mod.tasks_used_today() == used0

    def test_daily_limit_hit(self, monkeypatch):
        quota_mod.reset_for_tests()
        monkeypatch.setitem(quota_mod.QUOTA_RULES["community"], "daily_tasks", 2)
        assert quota_mod.try_consume_task()[0]
        assert quota_mod.try_consume_task()[0]
        ok, reason = quota_mod.try_consume_task()
        assert not ok and "上限" in reason
        monkeypatch.setitem(quota_mod.QUOTA_RULES["community"], "daily_tasks", 100)

    def test_workspace_limit(self):
        ok, _ = quota_mod.check_workspace(2)
        assert ok
        ok, reason = quota_mod.check_workspace(3)
        assert not ok and "工作区" in reason

    def test_snapshot_shape(self):
        snap = quota_mod.snapshot()
        assert snap["edition"] == "community"
        assert "used" in snap["daily_tasks"] and "limit" in snap["daily_tasks"]


# ── RAG 解析与分段 ──────────────────────────────────────


class TestParser:
    def test_txt(self):
        assert extract_text("a.txt", "你好 world".encode()) == "你好 world"

    def test_md(self):
        assert "# 标题" in extract_text("a.md", b"# \xe6\xa0\x87\xe9\xa2\x98\ncontent"
                                        .replace(b"\xe6\xa0\x87\xe9\xa2\x98", "标题".encode()))

    def test_unsupported(self):
        with pytest.raises(ValueError):
            extract_text("a.exe", b"MZ")

    def test_chunking_short(self):
        chunks = chunk_text("段落一\n\n段落二", chunk_size=100)
        assert chunks == ["段落一\n段落二"]

    def test_chunking_long(self):
        text = "\n\n".join(f"这是第{i}段，" + "内容" * 60 for i in range(5))
        chunks = chunk_text(text, chunk_size=200, overlap=20)
        assert len(chunks) > 3
        assert all(len(c) <= 200 for c in chunks)


# ── 知识库存储与检索 ────────────────────────────────────


class TestKnowledgeStore:
    def test_add_search_delete(self, tmp_path):
        store = KnowledgeStore(root=tmp_path / "kb")
        meta = store.add_document(
            "python.md",
            "Python 是一种解释型编程语言，适合快速开发。\n\n"
            "FastAPI 是现代 Python Web 框架，性能出色。".encode())
        assert meta["chunks"] >= 1
        store.add_document(
            "cooking.txt",
            "红烧肉的做法：五花肉切块，焯水后炒糖色，加料酒生抽炖煮四十分钟。".encode())
        assert store.doc_count() == 2

        hits = store.search("Python Web 框架", top_k=2)
        assert hits and hits[0]["doc_name"] == "python.md"
        assert hits[0]["score"] > 0

        # 引用溯源字段
        assert {"doc_id", "doc_name", "seq", "text", "score"} <= set(hits[0])

        # Reranker 路径可执行
        hits_rr = store.search("Python Web 框架", top_k=2, rerank=True)
        assert hits_rr

        assert store.delete_document(meta["id"])
        assert store.doc_count() == 1

    def test_persistence(self, tmp_path):
        root = tmp_path / "kb"
        KnowledgeStore(root=root).add_document("a.txt", "内容甲乙丙".encode())
        store2 = KnowledgeStore(root=root)
        assert store2.doc_count() == 1

    def test_multi_kb(self, tmp_path):
        store = KnowledgeStore(root=tmp_path / "kb")
        kb = store.create_kb("专题库")
        store.add_document("x.txt", "特定主题内容".encode(), kb_id=kb["id"])
        assert store.list_docs(kb["id"])
        assert store.delete_kb(kb["id"]) == 1
        with pytest.raises(ValueError):
            store.delete_kb("default")

    def test_reembed(self, tmp_path):
        store = KnowledgeStore(root=tmp_path / "kb")
        store.add_document("a.txt", "重新嵌入测试内容".encode())
        assert store.reembed_all() >= 1

    def test_hybrid_search_exact_keyword(self, tmp_path):
        """企业版混合检索：词法通道保证专名/型号精确命中。"""
        store = KnowledgeStore(root=tmp_path / "kb")
        store.add_document("spec.txt", "设备型号 XR-9000 的额定功率为 750W。".encode())
        store.add_document("other.txt", "会议室预订流程：先在系统提交申请。".encode())
        hits = store.search("XR-9000 功率", top_k=1, hybrid=True)
        assert hits and hits[0]["doc_name"] == "spec.txt"

    def test_hit_stats_and_search_log(self, tmp_path):
        """企业版：文档热度统计 + 检索审计日志。"""
        store = KnowledgeStore(root=tmp_path / "kb")
        store.add_document("a.txt", "知识库热度统计测试内容".encode())
        hits = store.search("热度统计", top_k=1)
        stats = store.hit_stats()
        assert stats["search_count"] >= 1
        assert stats["docs"] and stats["docs"][0]["hits"] >= 1
        store.log_search("热度统计", hits, source="api")
        log = store.search_log()
        assert log and log[0]["query"] == "热度统计"
        assert log[0]["hits"][0]["doc"] == "a.txt"


# ── 语义缓存（专业版实现，直接实例化测试）──────────────


@requires_pro
class TestSemanticCache:
    def _make(self, tmp_path, **kw):
        from automind_pro.semantic_cache import SemanticCacheFeature
        return SemanticCacheFeature(store_path=tmp_path / "cache.json", **kw)

    def test_miss_then_hit(self, tmp_path):
        cache = self._make(tmp_path)
        assert cache.lookup("什么是快速排序算法？") is None
        cache.store("什么是快速排序算法？", "快排是一种分治排序……", tokens=120)
        hit = cache.lookup("什么是快速排序算法？")
        assert hit and hit["reply"].startswith("快排")
        # 相似问法也应命中（特征哈希相似度）
        hit2 = cache.lookup("什么是快速排序算法")
        assert hit2 is not None

    def test_unrelated_not_hit(self, tmp_path):
        cache = self._make(tmp_path)
        cache.store("什么是快速排序算法？", "快排……", tokens=10)
        assert cache.lookup("今天北京天气怎么样") is None

    def test_stats_and_capacity(self, tmp_path):
        basic = self._make(tmp_path)
        adv = self._make(tmp_path, advanced=True)
        assert basic.capacity < adv.capacity
        s = basic.stats()
        assert {"enabled", "entries", "hit_rate", "saved_tokens"} <= set(s)


# ── 模型路由 ────────────────────────────────────────────


@requires_pro
class TestModelRouter:
    def _make(self, tmp_path, **kw):
        from automind_pro.model_router import ModelRouterFeature
        return ModelRouterFeature(store_path=tmp_path / "router.json", **kw)

    def test_complexity_scoring(self):
        from automind_pro.model_router import score_complexity
        assert score_complexity("你好") < score_complexity(
            "首先阅读代码，然后重构 def main() 并修复报错，最后跑测试", "coding")

    def test_disabled_returns_none(self, tmp_path):
        assert self._make(tmp_path).select("你好") is None

    def test_two_tier_routing(self, tmp_path):
        router = self._make(tmp_path, max_tiers=2)
        cfg, err = router.update({"enabled": True, "tiers": [
            {"name": "轻量", "provider": "deepseek", "model": "deepseek-chat", "max_score": 35},
            {"name": "强力", "provider": "deepseek", "model": "deepseek-reasoner", "max_score": 100},
        ]})
        assert err == "" and cfg["enabled"]
        low = router.select("你好")
        assert low["model"] == "deepseek-chat"
        high = router.select("首先阅读整个项目代码，然后重构 class Agent 并修复全部报错，"
                             "接着补充单元测试，最后运行 pytest 验证" * 3, interaction="coding")
        assert high["model"] == "deepseek-reasoner"

    def test_pro_tier_cap(self, tmp_path):
        router = self._make(tmp_path, max_tiers=2)
        _, err = router.update({"tiers": [
            {"provider": "a", "model": "m1", "max_score": 30},
            {"provider": "a", "model": "m2", "max_score": 60},
            {"provider": "a", "model": "m3", "max_score": 100},
        ]})
        assert "2 级" in err

    def test_enterprise_unlimited_tiers(self, tmp_path):
        router = self._make(tmp_path, max_tiers=None)
        _, err = router.update({"tiers": [
            {"provider": "a", "model": f"m{i}", "max_score": s}
            for i, s in enumerate((20, 40, 60, 80, 100))
        ]})
        assert err == ""


# ── 服务端路由存在性（社区版降级 403）──────────────────


class TestServerRoutes:
    def test_new_routes_registered(self):
        import automind.server as server
        paths = {getattr(r, "path", "") for r in server.app.routes}
        for p in ("/api/kb", "/api/kb/upload", "/api/kb/search", "/api/quota",
                  "/api/cache", "/api/router", "/api/costs", "/api/kb/kbs"):
            assert p in paths, f"缺少路由 {p}"

    @pytest.mark.asyncio
    async def test_kb_upload_limits(self, tmp_path, monkeypatch):
        import automind.rag.kb as kb_mod
        import automind.server as server
        monkeypatch.setattr(kb_mod, "_store", KnowledgeStore(root=tmp_path / "kb"))
        b64 = base64.b64encode("测试内容".encode()).decode()
        resp = await server.api_kb_upload({"name": "t.txt", "content_b64": b64})
        assert resp["status"] == "ok"
        # 非默认库（社区版）→ 403
        resp2 = await server.api_kb_upload({"name": "t2.txt", "content_b64": b64,
                                            "kb": "kb_x"})
        assert resp2.status_code == 403
