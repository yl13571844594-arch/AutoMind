"""v1.7.6 设置面板修复：执行治理参数可读、可校验、能同步到已存在的会话。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    import automind.server as srv
    srv._store.config_file = tmp_path / "cfg.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._store.chats_dir = tmp_path / "chats"
    srv._store.session_histories.clear()
    srv._AUTH_TOKEN = ""
    srv._agent = None
    srv._session_clones.clear()
    # 改配置属管理动作，仅本机回环放行
    yield TestClient(srv.app, client=("127.0.0.1", 51000)), srv
    srv._session_clones.clear()
    srv._AUTH_TOKEN = ""


def test_get_returns_numeric_knobs_and_policies(client):
    c, srv = client
    body = c.get("/api/config/autopilot").json()
    # 此前只返回布尔开关 → 面板上 11 个数值框全是空的
    for k in ("tool_timeout_seconds", "react_tool_timeout_seconds", "max_task_seconds",
              "react_no_progress_limit", "task_token_budget", "max_iterations"):
        assert isinstance(body[k], (int, float)), k
    assert body["approval_timeout_action"] in ("approve", "reject")
    assert body["write_conflict_policy"] in ("off", "warn", "block")
    assert isinstance(body["auto_review"], bool)


def test_post_returns_snapshot_and_rejects_bad_values(client):
    c, _ = client
    r = c.post("/api/config/autopilot", json={
        "tool_timeout_seconds": 900, "react_no_progress_limit": -3,
        "write_conflict_policy": "yolo", "max_iterations": 0}).json()
    assert r["tool_timeout_seconds"] == 900
    assert set(r["rejected"]) == {"react_no_progress_limit", "write_conflict_policy",
                                  "max_iterations"}
    # 读回来一致
    assert c.get("/api/config/autopilot").json()["tool_timeout_seconds"] == 900


def test_changes_propagate_to_existing_session_clones(client):
    c, srv = client
    base = srv.get_agent()
    clone = srv._acquire_run_agent(base, "s_live")
    assert clone is not base
    r = c.post("/api/config/autopilot", json={
        "react_tool_timeout_seconds": 777, "parallel_execution": False}).json()
    assert r["sessions_synced"] >= 1
    assert clone.config.execution.react_tool_timeout_seconds == 777
    assert clone.config.execution.parallel_execution is False
    assert clone.plan_executor.parallel is False


def test_int_knobs_persist_as_int(client):
    c, srv = client
    c.post("/api/config/autopilot", json={"react_tool_budget": 8})
    stored = srv._read_config()["autopilot"]["react_tool_budget"]
    assert stored == 8 and isinstance(stored, int)
