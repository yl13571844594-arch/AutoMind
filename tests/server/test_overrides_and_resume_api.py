"""v1.7.7：输入框 overrides 协议（模式/专家/附件 + 回执）与断点续跑 / 副本管理接口。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.core.types import AgentState


def test_mode_override_is_per_message_and_gated(monkeypatch):
    monkeypatch.setattr(srv._edition, "has_feature", lambda _f: False)
    inter, persist, rec = srv._resolve_overrides(
        {"interaction": "chat", "overrides": {"mode": "coding"}})
    assert inter == "coding" and persist is False and rec["mode"]["applied"]
    inter, persist, rec = srv._resolve_overrides(
        {"interaction": "chat", "overrides": {"mode": "multi"}})
    assert inter == "chat" and persist is True
    assert rec["mode"]["applied"] is False and "专业版" in rec["mode"]["reason"]
    inter, persist, rec = srv._resolve_overrides({"interaction": "work"})
    assert (inter, persist, rec) == ("work", True, {})


def test_expert_override(monkeypatch):
    monkeypatch.setattr(srv._experts, "get",
                        lambda eid: {"name": "律师", "icon": "⚖", "prompt": "你是律师"}
                        if eid == "law" else None)
    monkeypatch.setattr(srv._experts, "bump_usage", lambda _eid: None)
    rec: dict = {}
    out = srv._apply_expert_override("审合同", {"overrides": {"expert": "law"}}, rec)
    assert "你是律师" in out and rec["expert"]["applied"]
    rec = {}
    out = srv._apply_expert_override("审合同", {"overrides": {"expert": "gone"}}, rec)
    assert out == "审合同" and rec["expert"]["applied"] is False
    rec = {}
    out = srv._apply_expert_override("审合同", {"overrides": {"expert": ""}}, rec)
    assert out == "审合同" and rec["expert"]["name"] == "不使用专家"


def test_attachments_inlined_with_receipt_and_cache_key():
    rec: dict = {}
    task, key = srv._apply_attachments("总结一下", {"overrides": {"attachments": [
        {"name": "notes.md", "text": "第一行\n第二行"},
        {"name": "img.png", "text": ""},
    ]}}, rec)
    assert "notes.md" in task and "第二行" in task and key.startswith("\n[attachments:")
    assert rec["attachments"]["applied"] == ["notes.md"]
    assert rec["attachments"]["dropped"][0]["name"] == "img.png"
    assert srv._apply_attachments("x", {}, {}) == ("x", "")


@pytest.fixture
def client(tmp_path, monkeypatch):
    srv._store.config_file = tmp_path / "cfg.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._store.chats_dir = tmp_path / "chats"
    srv._AUTH_TOKEN = ""
    srv._agent = None
    agent = srv.get_agent()
    from automind.state.checkpoint import CheckpointManager
    agent.checkpoint_mgr = CheckpointManager(tmp_path / "ck")
    agent.config.project_root = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    yield TestClient(srv.app, client=("127.0.0.1", 51000)), agent
    srv._agent = None


def test_resumable_endpoint_and_dismiss(client):
    c, agent = client
    cid = asyncio.run(agent.checkpoint_mgr.save(AgentState(session_id="s_me_c1", metadata={
        "task": "迁移数据库", "status": "interrupted", "interaction": "work",
        "react": {"completed_actions": 2, "actions": [{}]}})))
    r = c.get("/api/checkpoints/resumable", params={"session_id": "s_me_c1"}).json()
    assert r["resumable"]["id"] == cid and r["resumable"]["done"] == 2
    assert c.get("/api/checkpoints/resumable",
                 params={"session_id": "s_me"}).json()["resumable"] is None
    assert c.post(f"/api/checkpoints/{cid}/dismiss",
                  json={"session_id": "s_other"}).status_code == 404
    assert c.post(f"/api/checkpoints/{cid}/dismiss", json={"session_id": "s_me_c1"}).status_code == 200
    assert c.get("/api/checkpoints/resumable",
                 params={"session_id": "s_me_c1"}).json()["resumable"] is None


def test_resume_target_validation(client):
    _, agent = client
    cid = asyncio.run(agent.checkpoint_mgr.save(AgentState(session_id="s_me", metadata={
        "task": "t", "status": "running", "react": {"actions": [{}]}})))
    info, err = srv._resume_target({"resume_checkpoint": cid}, "s_me")
    assert info["id"] == cid and not err
    info, err = srv._resume_target({"resume_checkpoint": cid}, "s_evil")
    assert info is None and "不属于" in err


def test_session_workspace_endpoints(client):
    c, agent = client
    from pathlib import Path

    from automind.core import workspace

    class Ex:
        isolate_workspace = True
    proj = Path(agent.config.project_root)
    (proj / "a.txt").write_text("1", encoding="utf-8")
    wd = Path(workspace.prepare("s_me_c9", proj, Ex()).path)
    (wd / "out.txt").write_text("result", encoding="utf-8")
    lst = c.get("/api/session-workspaces", params={"owner": "s_me"}).json()
    assert lst["copies"][0]["session"] == "s_me_c9" and lst["copies"][0]["changed"] == 1
    assert c.get("/api/session-workspaces", params={"owner": "s_x"}).json()["copies"] == []
    ch = c.get("/api/session-workspaces/s_me_c9/changes", params={"owner": "s_me"}).json()
    assert ch["added"] == ["out.txt"]
    assert c.post("/api/session-workspaces/s_me_c9/apply",
                  json={"owner": "s_evil"}).status_code == 404
    res = c.post("/api/session-workspaces/s_me_c9/apply", json={"owner": "s_me"}).json()
    assert res["applied"] == ["out.txt"] and (proj / "out.txt").read_text() == "result"
    assert c.delete("/api/session-workspaces/s_me_c9", params={"owner": "s_me"}).status_code == 200
    assert not wd.exists()
