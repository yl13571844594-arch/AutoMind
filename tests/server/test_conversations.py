"""v1.7.6 会话目录：多会话 / 归档（摘要进长期记忆）/ 上下文延续 / 多用户隔离。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import automind.conversations as conv_mod
from automind.conversations import (
    DEFAULT_TITLE,
    ConversationStore,
    auto_title,
    extractive_summary,
)
from automind.core.db import Database


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(Database(tmp_path / "conv.db"))
    monkeypatch.setattr(conv_mod, "_store", s)
    return s


# ── 存储层 ──────────────────────────────────────────────


def test_default_conversation_keeps_legacy_sid(store):
    c = store.ensure_default("s_abc", "_wxyz")
    assert c["sid"] == "s_abc_wxyz" and c["suffix"] == "" and c["title"] == DEFAULT_TITLE
    # 幂等
    assert store.ensure_default("s_abc", "_wxyz")["created"] == c["created"]


def test_create_and_scope_isolation(store):
    a = store.create("owner1", "_w1")
    store.create("owner1", "_w2")
    store.create("owner2", "_w1")
    assert a["sid"].startswith("owner1_w1_c") and a["suffix"].startswith("_c")
    assert [c["sid"] for c in store.list("owner1", "_w1")] == [a["sid"]]


def test_touch_sets_auto_title_and_counts_turns(store):
    c = store.create("o", "")
    store.touch(c["sid"], "帮我把 README 翻译成英文，并保留原有的排版格式和所有链接")
    got = store.get(c["sid"])
    assert got["turns"] == 1 and got["title"].startswith("帮我把 README")
    assert got["title"].endswith("…")
    # 之后不再改标题
    store.touch(c["sid"], "另一件事")
    assert store.get(c["sid"])["title"] == got["title"]


def test_touch_unknown_sid_is_noop(store):
    assert store.touch("nope", "x") is None


def test_context_prefix_first_turn_only(store):
    old = store.create("o", "_w")
    store.update(old["sid"], archived=True, summary="目标：重构登录模块；已完成：拆分 auth.py")
    seeded = store.create("o", "_w", seed="上次在修 CI 的缓存问题", seed_from="x")
    prefix = store.context_prefix(seeded["sid"])
    assert "【延续上下文】" in prefix and "CI 的缓存" in prefix
    assert "【项目记忆】" in prefix and "重构登录模块" in prefix
    store.touch(seeded["sid"], "继续")
    assert store.context_prefix(seeded["sid"]) == ""


def test_project_memory_isolated_by_workspace(store):
    other = store.create("o", "_other")
    store.update(other["sid"], archived=True, summary="别的项目的秘密")
    fresh = store.create("o", "_w")
    assert "别的项目" not in store.context_prefix(fresh["sid"])


def test_touch_unarchives(store):
    c = store.create("o", "")
    store.update(c["sid"], archived=True)
    store.touch(c["sid"], "又用上了")
    assert store.get(c["sid"])["archived"] is False


def test_extractive_summary_and_title_helpers():
    t = [{"role": "user", "content": "做一个爬虫"},
         {"role": "assistant", "content": "好的"},
         {"role": "user", "content": "加上重试"},
         {"role": "assistant", "content": "已完成，脚本在 crawl.py"}]
    s = extractive_summary(t)
    assert "做一个爬虫" in s and "加上重试" in s and "crawl.py" in s
    assert extractive_summary([]) == ""
    assert auto_title("【项目记忆】xxx\n---\n真正的问题") == "真正的问题"


# ── HTTP 端点 ───────────────────────────────────────────


class _FakeLTM:
    def __init__(self):
        self.docs = []

    async def add(self, documents, metadatas=None, ids=None):
        self.docs.extend(documents)


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    import automind.server as srv
    srv._store.config_file = tmp_path / "cfg.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._store.chats_dir = tmp_path / "chats"
    srv._store.session_histories.clear()
    srv._AUTH_TOKEN = ""
    ltm = _FakeLTM()
    fake_agent = SimpleNamespace(llm=None, memory=SimpleNamespace(long_term=ltm))
    monkeypatch.setattr(srv, "get_agent", lambda: fake_agent)
    yield TestClient(srv.app), srv, ltm
    srv._AUTH_TOKEN = ""


def test_list_creates_default(client):
    c, _, _ = client
    r = c.get("/api/conversations", params={"owner": "s_me", "workspace": ""})
    items = r.json()["conversations"]
    assert len(items) == 1 and items[0]["sid"] == "s_me" and items[0]["title"] == DEFAULT_TITLE


def test_archive_writes_summary_to_long_term_memory(client, store):
    c, _, ltm = client
    conv = c.post("/api/conversations", json={"owner": "s_me", "workspace": "_w"}).json()["conversation"]
    r = c.post(f"/api/conversations/{conv['sid']}/archive", json={
        "owner": "s_me",
        "transcript": [{"role": "user", "content": "部署到测试环境"},
                       {"role": "assistant", "content": "已部署，地址 http://t"}]})
    body = r.json()
    assert r.status_code == 200 and body["memorized"] is True
    assert body["summary_by"] == "extractive"          # 无 LLM → 抽取式兜底
    assert body["conversation"]["archived"] is True
    assert any("部署到测试环境" in d for d in ltm.docs)
    # 新会话首轮会带上"项目记忆"
    fresh = c.post("/api/conversations", json={"owner": "s_me", "workspace": "_w"}).json()["conversation"]
    assert "部署到测试环境" in store.context_prefix(fresh["sid"])


def test_continue_from_seeds_new_conversation(client, store):
    c, _, _ = client
    src = c.post("/api/conversations", json={"owner": "s_me", "workspace": ""}).json()["conversation"]
    r = c.post("/api/conversations", json={
        "owner": "s_me", "workspace": "", "continue_from": src["sid"],
        "transcript": [{"role": "user", "content": "分析日志里的 OOM"}]})
    new = r.json()["conversation"]
    assert new["seeded"] is True and new["title"].startswith("续：")
    assert "分析日志里的 OOM" in store.context_prefix(new["sid"])


def test_other_owner_cannot_touch_conversation(client):
    c, _, _ = client
    conv = c.post("/api/conversations", json={"owner": "s_me"}).json()["conversation"]
    assert c.patch(f"/api/conversations/{conv['sid']}",
                   json={"owner": "s_evil", "title": "x"}).status_code == 404
    assert c.delete(f"/api/conversations/{conv['sid']}",
                    params={"owner": "s_evil"}).status_code == 404
    assert c.delete(f"/api/conversations/{conv['sid']}",
                    params={"owner": "s_me"}).status_code == 200


def test_rename_and_pin(client):
    c, _, _ = client
    a = c.post("/api/conversations", json={"owner": "s_me"}).json()["conversation"]
    b = c.post("/api/conversations", json={"owner": "s_me"}).json()["conversation"]
    c.patch(f"/api/conversations/{a['sid']}", json={"owner": "s_me", "title": "重要", "pinned": True})
    items = c.get("/api/conversations", params={"owner": "s_me"}).json()["conversations"]
    assert items[0]["sid"] == a["sid"] and items[0]["title"] == "重要"
    assert b["sid"] in {i["sid"] for i in items}


def test_run_injection_helper(client, store):
    _, srv, _ = client
    conv = store.create("s_me", "", seed="上次的要点")
    out = srv._apply_conversation_context("新问题", conv["sid"])
    assert out.endswith("新问题") and "上次的要点" in out
    # 第二轮不再注入
    assert srv._apply_conversation_context("再问", conv["sid"]) == "再问"
    # 未登记的 sid 原样返回
    assert srv._apply_conversation_context("x", "unknown") == "x"
