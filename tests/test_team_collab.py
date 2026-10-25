"""v1.11.15 团队协作强化：任务领域逻辑 / 在线成员注册表 / 团队 HTTP API。

覆盖三块：
1. ``automind/core/team.py`` —— 旧数据兼容、增删改、评论、认领、过滤、摘要；
2. ``automind/core/team_presence.py`` —— 注册 / 改名 / 按 sid 聚合 / 多标签页；
3. ``automind/server.py`` 团队区 —— 关键 API（TestClient）与广播事件契约。

隔离策略：API 测试把 server 的 ``_team_raw_load/_team_raw_save`` 换成内存列表、
昵称文件指向 tmp_path，并在每个用例前后清空进程级在线表 —— 不碰仓库真实数据。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.core import team
from automind.core import team_presence as presence

TODAY = "2025-06-15"


@pytest.fixture(autouse=True)
def _clean_presence():
    """在线表是进程级全局状态：用例之间必须互不污染。"""
    presence.reset()
    yield
    presence.reset()


# ═══════════════════════════════════════════════════════════════
# 1. 任务领域逻辑（team.py）
# ═══════════════════════════════════════════════════════════════


class TestNormalizeTask:
    def test_legacy_task_gets_defaults_and_keeps_id(self):
        old = {"id": "abc12345", "title": "旧任务", "assignee": "小王", "desc": "d",
               "status": "done", "creator": "小李", "created": "2025-01-02 10:00"}
        t = team.normalize_task(old)
        assert t["id"] == "abc12345"
        assert t["priority"] == "normal"
        assert t["due"] == "" and t["tags"] == []
        assert t["comments"] == [] and t["activity"] == []
        assert t["created"] == "2025-01-02 10:00"
        assert t["updated"] == "2025-01-02 10:00"      # 缺 updated 时回退 created

    def test_unknown_values_fall_back(self):
        t = team.normalize_task({"title": "x", "status": "???", "priority": "nope",
                                 "due": "not-a-date", "tags": "a, b, a, "})
        assert t["status"] == "todo" and t["priority"] == "normal" and t["due"] == ""
        assert t["tags"] == ["a", "b"]                  # 去空、去重、保序

    def test_due_accepts_datetime_and_iso(self):
        assert team.normalize_due("2025-06-15T10:00") == "2025-06-15"
        assert team.normalize_due("2025-06-15 10:00") == "2025-06-15"
        assert team.normalize_due("2025-13-40") == ""

    def test_comments_and_activity_are_normalized(self):
        t = team.normalize_task({
            "title": "x",
            "comments": [{"author": "a", "text": "hi"}, {"text": ""}, "bad"],
            "activity": [{"kind": "created", "detail": "d"}, "bad"],
        })
        assert len(t["comments"]) == 1 and t["comments"][0]["id"]
        assert len(t["activity"]) == 1 and t["activity"][0]["kind"] == "created"


class TestTaskOps:
    def test_create_defaults_and_activity(self):
        t = team.create_task(title="重构登录")
        assert len(t["id"]) == 8
        assert t["status"] == "todo" and t["priority"] == "normal"
        assert t["activity"][-1]["kind"] == "created"
        assert t["created"] and t["updated"]

    def test_create_requires_title(self):
        with pytest.raises(team.TeamError):
            team.create_task(title="   ")

    def test_update_whitelist_and_status_machine(self):
        t = team.create_task(title="A")
        moved = team.update_task(t, {"status": "doing"}, actor="小王")
        assert moved["status"] == "doing"
        assert moved["activity"][-1]["kind"] == "status"
        assert "todo" in moved["activity"][-1]["detail"]
        done = team.update_task(moved, {"status": "done"}, actor="小王")
        assert done["activity"][-1]["kind"] == "done"
        with pytest.raises(team.TeamError):
            team.update_task(done, {"status": "bogus"})
        with pytest.raises(team.TeamError):
            team.update_task(done, {"id": "hack"})      # id 不在白名单

    def test_update_fields(self):
        t = team.create_task(title="A", priority="low", assignee="小王")
        t = team.update_task(t, {"title": "B", "desc": "说明", "priority": "urgent",
                                 "assignee": "小李", "due": "2030-01-01",
                                 "tags": ["x"]}, actor="小李")
        assert (t["title"], t["desc"], t["priority"]) == ("B", "说明", "urgent")
        assert t["assignee"] == "小李" and t["due"] == "2030-01-01" and t["tags"] == ["x"]
        kinds = {a["kind"] for a in t["activity"]}
        assert {"edit", "priority", "assign", "due"} <= kinds
        # 空标题不允许
        with pytest.raises(team.TeamError):
            team.update_task(t, {"title": ""})
        # 无变化不该刷活动
        before = len(t["activity"])
        t2 = team.update_task(t, {"title": "B"})
        assert len(t2["activity"]) == before

    def test_comment_claim_delete(self):
        t = team.create_task(title="A")
        t = team.add_comment(t, "先看接口", author="小李")
        assert t["comments"][-1]["text"] == "先看接口"
        with pytest.raises(team.TeamError):
            team.add_comment(t, "   ")
        t = team.claim(t, "小王")
        assert t["assignee"] == "小王" and t["status"] == "doing"
        assert t["activity"][-1]["kind"] == "claim"
        # 幂等：重复认领不刷活动流
        n = len(t["activity"])
        assert len(team.claim(t, "小王")["activity"]) == n
        with pytest.raises(team.TeamError):
            team.claim(t, "")

        kept, removed = team.delete_task([t], t["id"])
        assert kept == [] and removed["id"] == t["id"]
        kept, removed = team.delete_task([t], "nope")
        assert len(kept) == 1 and removed is None


class TestFilterSummarize:
    @staticmethod
    def _tasks() -> list[dict]:
        a = team.create_task(title="重构登录", assignee="小王", priority="urgent",
                             due="2025-06-10")
        a["status"] = "done"
        a["activity"] = [{"time": "2025-06-15 09:00:00", "kind": "done",
                          "actor": "小王", "detail": "完成"}]
        a["updated"] = "2025-06-15 09:00"
        b = team.create_task(title="补测试", assignee="小王", due="2025-06-25",
                             tags=["测试"])
        c = team.create_task(title="整理文档", assignee="小李", priority="low",
                             due="2025-06-01")
        return [a, b, c]

    def test_filter_by_status_assignee_priority_and_q(self):
        tasks = self._tasks()
        assert [t["title"] for t in team.filter_tasks(tasks, status="todo")] == ["补测试", "整理文档"]
        assert len(team.filter_tasks(tasks, assignee="小王")) == 2
        assert len(team.filter_tasks(tasks, priority="low")) == 1
        assert len(team.filter_tasks(tasks, q="测试")) == 1
        assert team.filter_tasks(tasks, q="重构")[0]["title"] == "重构登录"

    def test_overdue_only_excludes_done_and_respects_today(self):
        tasks = self._tasks()
        rows = team.filter_tasks(tasks, overdue_only=True, today=TODAY)
        assert [t["title"] for t in rows] == ["整理文档"]

    def test_due_sort_empty_last(self):
        tasks = self._tasks() + [team.create_task(title="无截止")]
        rows = team.filter_tasks(tasks, due_sort=True)
        assert [t["title"] for t in rows] == ["整理文档", "重构登录", "补测试", "无截止"]

    def test_summarize(self):
        s = team.summarize(self._tasks(), today=TODAY)
        assert s["total"] == 3
        assert s["by_status"] == {"todo": 2, "doing": 0, "blocked": 0, "done": 1}
        assert s["by_priority"]["urgent"] == 1 and s["by_priority"]["low"] == 1
        assert s["overdue"] == 1
        assert s["done_today"] == 1 and s["done_7d"] == 1
        assert s["completion_rate"] == 33.3
        row = next(r for r in s["by_assignee"] if r["assignee"] == "小王")
        assert row == {"assignee": "小王", "total": 2, "done": 1, "doing": 0, "overdue": 0}
        assert s["recent_activity"] and s["recent_activity"][0]["task_title"]

    def test_summarize_empty(self):
        s = team.summarize([], today=TODAY)
        assert s["total"] == 0 and s["completion_rate"] == 0.0
        assert s["by_assignee"] == [] and s["recent_activity"] == []


class TestTeamStore:
    def test_injected_memory_storage(self):
        box: list[dict] = []
        store = team.TeamStore(loader=lambda: list(box), saver=lambda items: box.__setitem__(slice(None), items))
        t = store.create(title="A")
        assert store.load()[0]["id"] == t["id"]
        store.update(t["id"], {"status": "doing"})
        after = store.comment(t["id"], "hi", "me")
        assert after["comments"][-1]["text"] == "hi"
        claimed = store.claim(t["id"], "小王")
        assert claimed["assignee"] == "小王" and claimed["status"] == "doing"
        store.delete(t["id"])
        assert store.load() == []
        with pytest.raises(team.TaskNotFound):
            store.update("nope", {"status": "doing"})
        with pytest.raises(team.TaskNotFound):
            store.delete("nope")


class TestMemberNameFile:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / "team_members.json"
        team.save_member_name("s_a", "小王", path)
        team.save_member_name("s_b", "小李", path)
        assert team.load_member_names(path) == {"s_a": "小王", "s_b": "小李"}

    def test_broken_or_missing_file_is_empty(self, tmp_path):
        assert team.load_member_names(tmp_path / "none.json") == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert team.load_member_names(bad) == {}

    def test_empty_values_rejected(self, tmp_path):
        with pytest.raises(team.TeamError):
            team.save_member_name("", "x", tmp_path / "m.json")
        with pytest.raises(team.TeamError):
            team.save_member_name("s_a", "  ", tmp_path / "m.json")


# ═══════════════════════════════════════════════════════════════
# 2. 在线成员注册表（team_presence.py）
# ═══════════════════════════════════════════════════════════════


class TestPresence:
    def test_default_name_uses_sid_tail(self):
        entry = presence.register("c1", "s_ab12cd34")
        assert entry["name"] == "成员-cd34"
        assert presence.count() == 1

    def test_multi_tab_aggregates_to_one_member(self):
        presence.register("c1", "s_ab12cd34")
        presence.register("c2", "s_ab12cd34")
        members = presence.list_members()
        assert len(members) == 1 and members[0]["tabs"] == 2
        assert presence.count() == 1 and presence.client_count() == 2

    def test_register_does_not_duplicate_client(self):
        presence.register("c1", "s_a")
        presence.register("c1", "s_a")
        assert presence.client_count() == 1

    def test_rename_and_bind(self):
        presence.register("c1", "s_a")
        presence.register("c2", "s_b")
        assert presence.rename("s_a", "小王") == 1
        names = {m["name"] for m in presence.list_members()}
        assert "小王" in names
        # 切工作区：cid 的 sid 变更，显式名字保留
        assert presence.bind("c1", "s_c") is True
        assert presence.bind("c1", "s_c") is False      # 同 sid 不重复广播
        assert {m["sid"] for m in presence.list_members()} == {"s_c", "s_b"}

    def test_known_names_restore_after_reconnect(self):
        presence.load_names({"s_known": "老王"})
        presence.register("c9", "s_known")
        assert presence.list_members()[0]["name"] == "老王"
        presence.unregister("c9")
        assert presence.count() == 0
        # 重新连接（模拟页面刷新）仍显示保存过的名字
        presence.register("c10", "s_known")
        assert presence.list_members()[0]["name"] == "老王"

    def test_unregister_and_unknown_operations(self):
        presence.register("c1", "s_a", "小王")
        assert presence.unregister("c1")["name"] == "小王"
        assert presence.unregister("c1") is None
        assert presence.bind("missing", "s_a") is True  # 未登记时 bind 等价于登记
        assert presence.bind("", "s_a") is False
        assert presence.rename("", "x") == 0
        assert presence.rename("s_a", "") == 0


# ═══════════════════════════════════════════════════════════════
# 3. 团队 HTTP API（TestClient）
# ═══════════════════════════════════════════════════════════════


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离的团队 API：内存任务表 + 临时昵称文件 + 捕获广播事件。"""
    box: dict[str, list[dict]] = {"items": []}
    monkeypatch.setattr(srv, "_team_raw_load",
                        lambda: [dict(t) for t in box["items"]])
    monkeypatch.setattr(srv, "_team_raw_save",
                        lambda items: box.__setitem__(
                            "items", [dict(t) for t in items]))
    monkeypatch.setattr(srv, "_TEAM_MEMBERS_FILE", tmp_path / "team_members.json")
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "")
    events: list[dict] = []

    async def _fake_broadcast(data: dict) -> None:
        events.append(data)

    monkeypatch.setattr(srv, "_broadcast", _fake_broadcast)
    presence.reset()
    client = TestClient(srv.app, client=("127.0.0.1", 51000))
    yield client, box, events
    presence.reset()


def _create(client: TestClient, **fields) -> dict:
    body = {"title": "重构登录", "assignee": "小王", "priority": "high",
            "due": "2030-01-01", "tags": ["后端", "安全"], "creator": "小李",
            "session_id": "s_me", **fields}
    r = client.post("/api/team/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task"]


class TestTeamApi:
    def test_old_contract_still_works(self, api):
        """v1.11.14 的调用方式（只传 status）必须继续可用。"""
        client, _, _ = api
        task = _create(client)
        tid = task["id"]
        assert client.post(f"/api/team/tasks/{tid}",
                           json={"status": "doing"}).status_code == 200
        assert client.post(f"/api/team/tasks/{tid}",
                           json={"status": "done"}).json()["task"]["status"] == "done"
        assert client.post(f"/api/team/tasks/{tid}",
                           json={"status": "bogus"}).status_code == 400
        assert client.post("/api/team/tasks", json={"title": ""}).status_code == 400
        assert client.delete(f"/api/team/tasks/{tid}").status_code == 200
        assert client.delete(f"/api/team/tasks/{tid}").status_code == 404
        assert client.get("/api/team/tasks").json()["tasks"] == []

    def test_create_list_and_summary(self, api):
        client, _, events = api
        task = _create(client)
        assert task["status"] == "todo" and task["priority"] == "high"
        assert task["tags"] == ["后端", "安全"]
        assert task["activity"][-1]["kind"] == "created"
        # 广播契约：task_created + 全量 task + 平铺 title/assignee（旧前端可读）
        ev = events[-1]
        assert ev["type"] == "team_activity" and ev["kind"] == "task_created"
        assert ev["task"]["id"] == task["id"] and ev["title"] == "重构登录"
        assert ev["assignee"] == "小王"

        body = client.get("/api/team/tasks").json()
        assert len(body["tasks"]) == 1
        assert body["summary"]["total"] == 1
        assert body["summary"]["by_status"]["todo"] == 1

        # 过滤参数
        assert len(client.get("/api/team/tasks",
                              params={"status": "todo"}).json()["tasks"]) == 1
        assert client.get("/api/team/tasks",
                          params={"status": "done"}).json()["tasks"] == []
        assert len(client.get("/api/team/tasks",
                              params={"q": "登录"}).json()["tasks"]) == 1
        assert client.get("/api/team/tasks",
                          params={"priority": "urgent"}).json()["tasks"] == []
        assert client.get("/api/team/tasks",
                          params={"assignee": "老王"}).json()["tasks"] == []
        assert client.get("/api/team/tasks",
                          params={"overdue": "1"}).json()["tasks"] == []
        # summary 不随过滤变化（始终是全量口径）
        assert client.get("/api/team/tasks",
                          params={"status": "done"}).json()["summary"]["total"] == 1

        summary = client.get("/api/team/summary").json()["summary"]
        assert summary["total"] == 1 and "today" in summary

    def test_update_fields_comments_claim(self, api):
        client, _, events = api
        task = _create(client)
        tid = task["id"]

        r = client.post(f"/api/team/tasks/{tid}",
                        json={"priority": "urgent", "actor": "小王", "session_id": "s_me"})
        assert r.status_code == 200 and r.json()["task"]["priority"] == "urgent"
        assert events[-1]["kind"] == "task_updated"

        r = client.post(f"/api/team/tasks/{tid}", json={"status": "blocked", "actor": "小王"})
        assert r.json()["task"]["status"] == "blocked"
        assert events[-1]["kind"] == "task_moved"
        assert "todo" in events[-1]["detail"]

        r = client.post(f"/api/team/tasks/{tid}/comments",
                        json={"text": "先看接口", "author": "小李"})
        assert r.status_code == 200
        comment = r.json()["comment"]
        assert comment["text"] == "先看接口" and r.json()["task"]["comments"][0]["id"] == comment["id"]
        assert events[-1]["kind"] == "task_commented" and events[-1]["comment"]["text"] == "先看接口"
        assert client.post(f"/api/team/tasks/{tid}/comments",
                           json={"text": ""}).status_code == 400

        r = client.post(f"/api/team/tasks/{tid}/claim",
                        json={"member": "小王", "session_id": "s_me"})
        assert r.status_code == 200
        claimed = r.json()["task"]
        assert claimed["assignee"] == "小王" and claimed["status"] == "doing"
        assert events[-1]["kind"] == "task_claimed"

        # 未知任务
        assert client.post("/api/team/tasks/nope", json={"status": "done"}).status_code == 404
        assert client.post("/api/team/tasks/nope/comments",
                           json={"text": "x"}).status_code == 404
        assert client.post("/api/team/tasks/nope/claim",
                           json={"member": "x"}).status_code == 404

    def test_members_and_name_endpoint(self, api, tmp_path):
        client, _, events = api
        _create(client)
        presence.register("c1", "s_me", "小王")
        presence.register("c2", "s_other", "老王")

        body = client.get("/api/team/members").json()
        names = {m["name"] for m in body["members"]}
        assert "老王" in names and body["online_count"] >= 1
        myself = next(m for m in body["members"] if m["sid"] == "s_me")
        assert myself["online"] is True and myself["total"] == 1

        r = client.post("/api/team/members/name",
                        json={"sid": "s_me", "name": "王小王"})
        assert r.status_code == 200 and r.json()["name"] == "王小王"
        assert any(m["name"] == "王小王" for m in presence.list_members())
        assert (tmp_path / "team_members.json").is_file()
        assert team.load_member_names(tmp_path / "team_members.json")["s_me"] == "王小王"
        assert any(e["type"] == "team_presence" for e in events)
        # 参数校验
        assert client.post("/api/team/members/name", json={"name": "x"}).status_code == 400
        assert client.post("/api/team/members/name",
                           json={"sid": "s_me", "name": " "}).status_code == 400

    def test_legacy_storage_data_is_normalized(self, api):
        client, box, _ = api
        box["items"] = [{"id": "old00001", "title": "旧任务", "assignee": "老李",
                         "desc": "", "status": "done", "creator": "老张",
                         "created": "2025-01-02 10:00"}]
        body = client.get("/api/team/tasks").json()
        t = body["tasks"][0]
        assert t["id"] == "old00001" and t["priority"] == "normal"
        assert t["comments"] == [] and t["activity"] == []
        assert body["summary"]["total"] == 1
        # 旧任务照样能按旧方式更新状态
        assert client.post("/api/team/tasks/old00001",
                           json={"status": "doing"}).status_code == 200

    def test_done_event_protocol_unchanged(self):
        """旧 team_activity task_done 的字段一个都不能变（前端与集成依赖）。"""
        ev = srv._team_event("s_me", "重构登录", {"success": True, "interaction": "coding"})
        assert ev["type"] == "team_activity" and ev["kind"] == "task_done"
        assert ev["sid"] == "s_me" and ev["task"] == "重构登录"
        assert ev["success"] is True and ev["interaction"] == "coding"
        assert isinstance(ev["changed_files"], int)
        assert ev["time"]


class TestWsPresence:
    """ws 连接 / bind / 断开都广播在线成员（多标签页聚合为一个人）。"""

    def test_connect_bind_and_disconnect_broadcast(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "_team_raw_load", lambda: [])
        monkeypatch.setattr(srv, "_team_raw_save", lambda _items: None)
        monkeypatch.setattr(srv, "_TEAM_MEMBERS_FILE", tmp_path / "team_members.json")
        monkeypatch.setattr(srv, "_AUTH_TOKEN", "")
        monkeypatch.setattr(srv, "_ws_clients", {"all": []})   # 连接表也要隔离
        presence.reset()
        client = TestClient(srv.app, client=("127.0.0.1", 51000))
        with client.websocket_connect("/ws") as ws1:
            assert ws1.receive_json()["type"] == "connected"
            with client.websocket_connect("/ws") as ws2:
                assert ws2.receive_json()["type"] == "connected"
                # 新连接登记后，已连着的 ws1 收到成员表；两条未 bind 的连接
                # 同属 default → 聚合为一个人（tabs=2）
                ev = ws1.receive_json()
                assert ev["type"] == "team_presence"
                assert len(ev["members"]) == 1 and ev["members"][0]["tabs"] == 2
                # bind 声明工作区级 sid：成员拆成两个，双方都收到更新
                ws2.send_json({"action": "bind", "session_id": "s_me_c1",
                               "member_id": "s_me"})
                ev2 = ws2.receive_json()
                assert ev2["type"] == "team_presence"
                assert "s_me" in {m["sid"] for m in ev2["members"]}
                assert ws1.receive_json()["type"] == "team_presence"
        # 连接全部断开后在线表清空（广播本身没有接收方，不抛错即可）
        assert presence.count() == 0
