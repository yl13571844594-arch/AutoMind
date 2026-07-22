"""SQLite 持久化层测试 — automind/core/db.py 与各接线点的迁移/读写。"""

from __future__ import annotations

import json

from automind.core.db import Database, migrate_json_once


class TestDatabase:
    def test_kv_roundtrip(self, tmp_path):
        db = Database(tmp_path / "t.db")
        assert db.kv_get("missing", "dft") == "dft"
        db.kv_set("k", {"a": 1, "中文": "值"})
        assert db.kv_get("k") == {"a": 1, "中文": "值"}
        db.kv_set("k", [1, 2, 3])   # 覆盖更新
        assert db.kv_get("k") == [1, 2, 3]
        db.close()

    def test_history_append_cap_and_replace(self, tmp_path):
        db = Database(tmp_path / "t.db")
        for i in range(15):
            db.history_append({"n": i}, cap=10)
        data = db.history_load()
        assert len(data) == 10
        assert data[0]["n"] == 5 and data[-1]["n"] == 14
        db.history_replace([{"n": 99}])
        assert db.history_load() == [{"n": 99}]
        db.close()

    def test_sessions(self, tmp_path):
        db = Database(tmp_path / "t.db")
        assert db.session_load("alice") is None
        db.session_save("alice", [{"role": "user", "content": "你好"}])
        assert db.session_load("alice")[0]["content"] == "你好"
        db.session_save("alice", [])   # 清空也是有效记录（不再回读旧 JSON）
        assert db.session_load("alice") == []
        db.session_delete("alice")
        assert db.session_load("alice") is None
        db.close()

    def test_team_tasks(self, tmp_path):
        db = Database(tmp_path / "t.db")
        db.team_replace([{"id": "a", "title": "任务A"}, {"id": "b", "title": "任务B"}])
        items = db.team_load()
        assert [t["id"] for t in items] == ["a", "b"]
        db.team_replace([])
        assert db.team_load() == []
        db.close()

    def test_persistence_across_reopen(self, tmp_path):
        p = tmp_path / "t.db"
        db = Database(p)
        db.kv_set("x", 1)
        db.history_append({"a": 1})
        db.close()
        db2 = Database(p)
        assert db2.kv_get("x") == 1
        assert db2.history_load() == [{"a": 1}]
        db2.close()


class TestJsonMigration:
    def test_migrate_once_and_backup_kept(self, tmp_path):
        db = Database(tmp_path / "t.db")
        legacy = tmp_path / "old.json"
        legacy.write_text(json.dumps([{"n": 1}, {"n": 2}]), encoding="utf-8")
        done = migrate_json_once(db, "hist", legacy,
                                 lambda data: db.history_replace(data))
        assert done and db.history_load() == [{"n": 1}, {"n": 2}]
        assert legacy.exists()   # 旧文件保留作备份
        # 第二次不再迁移（标记已置位）
        db.history_replace([])
        assert not migrate_json_once(db, "hist", legacy,
                                     lambda data: db.history_replace(data))
        assert db.history_load() == []
        db.close()

    def test_migrate_missing_or_broken_file(self, tmp_path):
        db = Database(tmp_path / "t.db")
        assert not migrate_json_once(db, "a", tmp_path / "nope.json", lambda _d: None)
        broken = tmp_path / "broken.json"
        broken.write_text("{invalid", encoding="utf-8")
        assert not migrate_json_once(db, "b", broken, lambda _d: None)
        db.close()


class TestStoreSessionsSqlite:
    """server_store.Store 会话持久化：SQLite 落库 + 旧 JSON 惰性迁移。"""

    def _store(self, tmp_path):
        from automind.server_store import Store
        s = Store()
        s.config_file = tmp_path / "cfg.json"
        s.chat_file = tmp_path / "chat.json"     # sessions.db 落同目录
        s.chats_dir = tmp_path / "chats"
        return s

    def test_save_and_reload_via_sqlite(self, tmp_path):
        s = self._store(tmp_path)
        s.get_session_history("u1").append({"role": "user", "content": "hi"})
        s.save_session_history("u1")
        assert (tmp_path / "sessions.db").exists()
        # 新 Store 实例（模拟重启）从 SQLite 恢复
        s2 = self._store(tmp_path)
        assert s2.get_session_history("u1")[0]["content"] == "hi"

    def test_legacy_json_lazy_migration(self, tmp_path):
        s = self._store(tmp_path)
        # 构造旧版会话 JSON 文件
        legacy = s.session_file("old_user")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps([{"role": "user", "content": "旧数据"}]),
                          encoding="utf-8")
        hist = s.get_session_history("old_user")
        assert hist[0]["content"] == "旧数据"
        # 已迁移进 SQLite：删除旧文件后新实例仍能读到
        s2 = self._store(tmp_path)
        assert s2.get_session_history("old_user")[0]["content"] == "旧数据"
        assert legacy.exists()   # 旧文件保留

    def test_reset_clears_session(self, tmp_path):
        s = self._store(tmp_path)
        s.get_session_history("u2").append({"role": "user", "content": "x"})
        s.save_session_history("u2")
        s.session_histories["u2"] = []
        s.save_session_history("u2")
        s2 = self._store(tmp_path)
        assert s2.get_session_history("u2") == []


class TestKbSqlite:
    """知识库 SQLite 持久化与旧 JSON 迁移。"""

    def test_kb_persists_across_reopen(self, tmp_path):
        from automind.rag.kb import KnowledgeStore
        store = KnowledgeStore(root=tmp_path / "kb")
        store.add_document("a.txt", "SQLite 持久化测试内容".encode())
        kb = store.create_kb("专题")
        store.update_settings({"backend": "builtin"})
        assert (tmp_path / "kb" / "kb.db").exists()
        # 重开：文档/库/设置全部恢复
        store2 = KnowledgeStore(root=tmp_path / "kb")
        assert store2.doc_count() == 1
        assert any(k["id"] == kb["id"] for k in store2.list_kbs())
        assert store2.get_settings().get("backend") == "builtin"
        hits = store2.search("SQLite 持久化", top_k=1)
        assert hits and hits[0]["doc_name"] == "a.txt"

    def test_kb_legacy_json_migration(self, tmp_path):
        from automind.memory.long_term import _SimpleEmbedder
        root = tmp_path / "kb"
        root.mkdir(parents=True)
        emb = _SimpleEmbedder()
        # 构造旧版 index.json / chunks.json
        (root / "index.json").write_text(json.dumps({
            "kbs": [{"id": "default", "name": "默认知识库"}],
            "docs": [{"id": "doc_x", "kb": "default", "name": "旧文档.txt",
                      "size": 10, "chunks": 1, "time": "2026-01-01 00:00:00"}],
            "settings": {"backend": "builtin"},
        }, ensure_ascii=False), encoding="utf-8")
        (root / "chunks.json").write_text(json.dumps([
            {"id": "doc_x_0", "doc_id": "doc_x", "kb": "default", "seq": 0,
             "text": "旧版JSON知识内容", "vec": emb.embed("旧版JSON知识内容")},
        ], ensure_ascii=False), encoding="utf-8")

        from automind.rag.kb import KnowledgeStore
        store = KnowledgeStore(root=root)
        assert store.doc_count() == 1
        hits = store.search("旧版JSON知识", top_k=1)
        assert hits and hits[0]["doc_name"] == "旧文档.txt"
        assert (root / "index.json").exists()   # 备份保留

    def test_kb_search_log_sqlite(self, tmp_path):
        from automind.rag.kb import KnowledgeStore
        store = KnowledgeStore(root=tmp_path / "kb")
        store.add_document("a.txt", "审计日志测试".encode())
        hits = store.search("审计日志", top_k=1)
        store.log_search("审计日志", hits, source="chat")
        log = store.search_log()
        assert log and log[0]["source"] == "chat"
        # 重开后日志仍在
        store2 = KnowledgeStore(root=tmp_path / "kb")
        assert store2.search_log()


class TestQuotaSqlite:
    def test_quota_persists_in_kv(self, tmp_path, monkeypatch):
        from automind.core import db as db_mod
        from automind.core import quota
        db = db_mod.reset_for_tests(tmp_path / "automind.db")
        try:
            monkeypatch.setattr(quota, "_loaded", True)
            quota.reset_for_tests()
            quota.try_consume_task()
            quota.try_consume_task()
            assert db.kv_get("quota", {}).get("tasks") == 2
        finally:
            db_mod.reset_for_tests(None)
            quota.reset_for_tests()
