"""v0.7 新功能测试 — 工作区管理 / 文件改动日志与回滚 / 任务历史持久化。"""

from __future__ import annotations

import asyncio
import json

import pytest

from automind.tools.file_editor import (
    JOURNAL,
    FileChangeJournal,
    FileEditTool,
    FileWriteTool,
)


@pytest.fixture(autouse=True)
def _clean_journal():
    JOURNAL.clear()
    yield
    JOURNAL.clear()


class TestFileChangeJournal:
    def test_write_records_creation_and_rollback_deletes(self, tmp_path):
        f = tmp_path / "new.txt"
        tool = FileWriteTool()
        r = asyncio.run(tool.execute(path=str(f), content="hello"))
        assert r.success and f.exists()
        entries = JOURNAL.entries()
        assert entries and entries[0]["created"] is True
        assert JOURNAL.rollback(str(f)) is True
        assert not f.exists()

    def test_overwrite_records_preimage_and_restores(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("original", encoding="utf-8")
        tool = FileWriteTool()
        asyncio.run(tool.execute(path=str(f), content="v2"))
        asyncio.run(tool.execute(path=str(f), content="v3"))
        assert f.read_text(encoding="utf-8") == "v3"
        # 回滚 = 恢复到最早前像（原始内容）
        assert JOURNAL.rollback(str(f)) is True
        assert f.read_text(encoding="utf-8") == "original"

    def test_edit_records_preimage(self, tmp_path):
        f = tmp_path / "b.py"
        f.write_text("x = 1\n", encoding="utf-8")
        tool = FileEditTool()
        r = asyncio.run(tool.execute(path=str(f), old_string="x = 1", new_string="x = 2"))
        assert r.success
        assert JOURNAL.rollback(str(f)) is True
        assert f.read_text(encoding="utf-8") == "x = 1\n"

    def test_rollback_all(self, tmp_path):
        j = FileChangeJournal()
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("A2", encoding="utf-8")
        b.write_text("B2", encoding="utf-8")
        j.record(str(a), "A1", "file_write")
        j.record(str(b), None, "file_write")  # b 是新建
        assert j.rollback_all() == 2
        assert a.read_text(encoding="utf-8") == "A1"
        assert not b.exists()

    def test_entries_hide_preimage(self, tmp_path):
        JOURNAL.record(str(tmp_path / "x"), "secret-content", "file_write")
        for e in JOURNAL.entries():
            assert "before" not in e

    def test_cap(self):
        j = FileChangeJournal()
        for i in range(j.MAX_ENTRIES + 50):
            j.record(f"f{i}", "x", "file_write")
        assert len(j._entries) == j.MAX_ENTRIES


class TestWorkspaceApi:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from automind import server
        monkeypatch.setattr(server._store, "config_file",
                             tmp_path / "cfg.json", raising=False)
        return TestClient(server.app)

    def test_workspace_crud_and_switch(self, client, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        # 添加
        r = client.post("/api/workspaces", json={"name": "测试区", "path": str(d)})
        assert r.status_code == 200
        # 列表
        r = client.get("/api/workspaces").json()
        assert any(w["name"] == "测试区" for w in r["workspaces"])
        # 切换
        r = client.post("/api/workspaces/switch", json={"name": "测试区"})
        assert r.status_code == 200
        assert r.json()["project"] == str(d.resolve())
        # 切回默认
        r = client.post("/api/workspaces/switch", json={"name": ""})
        assert r.status_code == 200
        # 删除
        r = client.delete("/api/workspaces/%E6%B5%8B%E8%AF%95%E5%8C%BA")
        assert r.json()["deleted"] == 1

    def test_add_rejects_bad_dir(self, client):
        r = client.post("/api/workspaces", json={"name": "x", "path": "Z:/no/such/dir"})
        assert r.status_code == 400

    def test_switch_unknown_404(self, client):
        r = client.post("/api/workspaces/switch", json={"name": "不存在"})
        assert r.status_code == 404


class TestChangesApi:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        from automind import server
        return TestClient(server.app)

    def test_changes_list_and_rollback(self, client, tmp_path):
        f = tmp_path / "c.txt"
        f.write_text("v2", encoding="utf-8")
        JOURNAL.record(str(f), "v1", "file_write")
        r = client.get("/api/changes").json()
        assert any(c["path"] == str(f) for c in r["changes"])
        r = client.post("/api/changes/rollback", json={"path": str(f)})
        assert r.status_code == 200
        assert f.read_text(encoding="utf-8") == "v1"

    def test_rollback_unknown_404(self, client):
        r = client.post("/api/changes/rollback", json={"path": "no/such/file"})
        assert r.status_code == 404


class TestHistoryPersistence:
    def test_push_history_persists_and_caps(self, tmp_path, monkeypatch):
        from automind import server
        monkeypatch.setattr(server, "_HISTORY_FILE", tmp_path / "hist.json")
        monkeypatch.setattr(server, "_task_history", [])
        for i in range(server._HISTORY_CAP + 10):
            server._push_history({"session_id": f"s{i}", "task": f"t{i}",
                                  "output": "o", "success": True})
        assert len(server._task_history) == server._HISTORY_CAP
        data = json.loads((tmp_path / "hist.json").read_text(encoding="utf-8"))
        assert len(data) == server._HISTORY_CAP
        assert data[-1]["session_id"] == f"s{server._HISTORY_CAP + 9}"
        assert "time" in data[-1]
