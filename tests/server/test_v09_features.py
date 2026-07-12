"""v0.9 功能测试 — 代码编辑器 API / 专家系统 / 团队协作。"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from automind.core.experts import COMMUNITY_MAX_CUSTOM, ExpertStore  # noqa: E402


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """隔离配置/专家/团队存储与项目根的服务器夹具。"""
    import automind.server as server
    from automind.tools.file_editor import JOURNAL
    monkeypatch.setattr(server, "_AUTH_TOKEN", "")
    monkeypatch.setattr(server._store, "config_file", tmp_path / "cfg.json",
                        raising=False)
    monkeypatch.setattr(server._experts, "_path", tmp_path / "experts.json")
    monkeypatch.setattr(server, "_TEAM_FILE", tmp_path / "team.json")
    # 项目根 → tmp 沙箱
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (proj / "sub").mkdir()
    (proj / "sub" / "a.txt").write_text("aaa", encoding="utf-8")

    class _FakeAgent:
        class config:  # noqa: N801
            project_root = str(proj)
    monkeypatch.setattr(server, "_agent", _FakeAgent())
    JOURNAL.clear()
    yield TestClient(server.app), server, proj
    JOURNAL.clear()


class TestEditorFilesApi:
    def test_tree_lists_files(self, srv):
        c, _, _ = srv
        r = c.get("/api/files/tree").json()
        paths = [e["path"] for e in r["entries"]]
        assert "main.py" in paths and "sub" in paths and "sub/a.txt" in paths

    def test_read_and_write_roundtrip(self, srv):
        c, _, proj = srv
        assert c.get("/api/files/read?path=main.py").json()["content"] == "print('hi')\n"
        r = c.post("/api/files/write", json={"path": "main.py", "content": "x = 1\n"})
        assert r.status_code == 200
        assert (proj / "main.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_write_records_journal_and_diff(self, srv):
        c, _, _ = srv
        c.post("/api/files/write", json={"path": "main.py", "content": "x = 2\n"})
        d = c.get("/api/changes/diff?path=main.py").json()
        assert d["before"] == "print('hi')\n" and d["after"] == "x = 2\n"

    def test_traversal_blocked(self, srv):
        c, _, _ = srv
        assert c.get("/api/files/read?path=../../etc/passwd").status_code == 403
        assert c.post("/api/files/write",
                      json={"path": "../evil.txt", "content": "x"}).status_code == 403

    def test_diff_404_without_changes(self, srv):
        c, _, _ = srv
        assert c.get("/api/changes/diff?path=sub/a.txt").status_code == 404


class TestExperts:
    def test_official_catalog_has_ten(self, srv):
        c, _, _ = srv
        d = c.get("/api/experts").json()
        assert len(d["official"]) == 10
        assert d["custom_limit"] == COMMUNITY_MAX_CUSTOM

    def test_install_activate_inject(self, srv):
        c, server, _ = srv
        assert c.post("/api/experts/install", json={"id": "qa-engineer"}).status_code == 200
        assert c.post("/api/experts/activate", json={"id": "qa-engineer"}).status_code == 200
        out = server._apply_expert("补测试")
        assert "测试工程师" in out and out.endswith("补测试")
        # 用量统计 +1
        e = server._experts.get("qa-engineer")
        assert e["usage"] == 1
        # 取消激活后不再注入
        c.post("/api/experts/activate", json={"id": ""})
        assert server._apply_expert("补测试") == "补测试"

    def test_community_limit_three(self, srv):
        c, _, _ = srv
        for i in range(COMMUNITY_MAX_CUSTOM):
            r = c.post("/api/experts", json={"name": f"e{i}", "prompt": "p"})
            assert r.status_code == 200
        r = c.post("/api/experts", json={"name": "e4", "prompt": "p"})
        assert r.status_code == 400 and "专业版" in r.json()["error"]

    def test_share_requires_pro(self, srv):
        c, _, _ = srv
        r = c.post("/api/experts", json={"name": "s", "prompt": "p", "shared": True})
        assert r.status_code == 403 and r.json()["feature"] == "experts_pro"

    def test_locked_pro_routes(self, srv):
        c, _, _ = srv
        assert c.get("/api/experts/export").status_code == 403
        assert c.get("/api/experts/stats").status_code == 403
        assert c.get("/api/experts/pending").status_code == 403

    def test_delete_clears_active(self, srv):
        c, server, _ = srv
        c.post("/api/experts/install", json={"id": "pm"})
        c.post("/api/experts/activate", json={"id": "pm"})
        c.delete("/api/experts/pm")
        assert server._read_config().get("active_expert") == ""


class TestExpertStoreUnit:
    def test_unlimited_bypasses_cap(self, tmp_path):
        store = ExpertStore(store_path=tmp_path / "e.json")
        for i in range(COMMUNITY_MAX_CUSTOM + 2):
            expert, err = store.create({"name": f"n{i}", "prompt": "p"},
                                       owner="o", unlimited=True)
            assert err == ""
        assert store.custom_count() == COMMUNITY_MAX_CUSTOM + 2

    def test_needs_approval_flag(self, tmp_path):
        store = ExpertStore(store_path=tmp_path / "e.json")
        expert, _ = store.create({"name": "n", "prompt": "p", "shared": True},
                                 owner="o", unlimited=True, needs_approval=True)
        assert expert["approved"] is False


class TestTeamTasks:
    def test_crud_and_status_flow(self, srv):
        c, _, _ = srv
        r = c.post("/api/team/tasks", json={"title": "重构登录", "assignee": "小王"})
        assert r.status_code == 200
        tid = r.json()["task"]["id"]
        assert len(c.get("/api/team/tasks").json()["tasks"]) == 1
        assert c.post(f"/api/team/tasks/{tid}", json={"status": "doing"}).status_code == 200
        assert c.post(f"/api/team/tasks/{tid}", json={"status": "done"}).status_code == 200
        assert c.post(f"/api/team/tasks/{tid}", json={"status": "bogus"}).status_code == 400
        assert c.delete(f"/api/team/tasks/{tid}").status_code == 200
        assert c.get("/api/team/tasks").json()["tasks"] == []

    def test_title_required(self, srv):
        c, _, _ = srv
        assert c.post("/api/team/tasks", json={"title": ""}).status_code == 400
