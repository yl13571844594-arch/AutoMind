"""v1.10.0 社区版一侧的服务端行为：治理 / 轨迹 / 模板入口的降级、请求身份、
审批人校验（外部回执）。企业版的真实行为见 pro/tests/test_workflow_governance.py。
"""

# 夹具 env 从 v1.8 的接口测试里复用：测试函数的同名参数会被 ruff 当成重定义（F811）
# ruff: noqa: F811

from __future__ import annotations

import asyncio

import pytest

import automind.server as srv
from tests.server.test_workflow_runs_api import env  # noqa: F401


@pytest.mark.parametrize(("method", "path", "feature"), [
    ("get", "/api/workflow/governance", "workflow_governance"),
    ("post", "/api/workflow/governance/a.yaml/submit", "workflow_governance"),
    ("post", "/api/workflow/governance/a.yaml/approve", "workflow_governance"),
    ("get", "/api/workflow/secrets", "workflow_governance"),
    ("get", "/api/workflow/concurrency", "workflow_governance"),
    ("get", "/api/workflow/runs/abc/audit", "workflow_governance"),
    ("get", "/api/workflow/from-trace/candidates", "workflow_from_trace"),
    ("post", "/api/workflow/from-trace", "workflow_from_trace"),
    ("get", "/api/workflow/templates", "workflow_templates"),
    ("post", "/api/workflow/templates/x/use", "workflow_templates"),
])
def test_v110_entrypoints_are_locked(env, method, path, feature) -> None:
    r = getattr(env["c"], method)(path)
    assert r.status_code == 403
    body = r.json()
    assert body["feature"] == feature
    assert ("企业版" if feature == "workflow_governance" else "专业版") in body["error"]


def test_community_identity_is_local_admin(env) -> None:
    body = env["c"].get("/api/workflow/list").json()
    assert body["identity"] == {"user": "local", "role": "admin", "team": "default",
                                "source": "local"}


def test_list_reports_secrets_and_permissions(env) -> None:
    (env["wf"] / "a.yaml").write_text(
        "version: 1\nname: a\nsteps:\n  - {id: s, type: tool, tool: wf_probe}\n",
        encoding="utf-8")
    row = env["c"].get("/api/workflow/list").json()["workflows"][0]
    assert row["secrets"] == [] and row["can"] == {
        "edit": True, "run": True, "trigger": True, "approve": True}
    assert "publish" not in row                         # 社区版没有发布审批


@pytest.mark.parametrize(("required", "who", "ok"), [
    ([], {"user": "x", "role": "viewer"}, True),
    (["role:finance"], {"user": "x", "role": "finance"}, True),
    (["role:finance"], {"user": "x", "role": "admin"}, False),
    (["user:alice", "role:manager"], {"user": "alice", "role": "viewer"}, True),
    (["user:alice"], None, False),
    # 没有登录身份的平台管理员（本机 / 平台令牌）可以代任何角色审批；SSO 登录的 admin 角色不行
    (["role:finance"], {"user": "local", "role": "admin", "source": "local"}, True),
    (["role:finance"], {"user": "root", "role": "admin", "source": "sso"}, False),
])
def test_approver_matching(required, who, ok) -> None:
    assert srv._approver_matches(required, who)[0] is ok


def test_receipt_enforces_required_approver(env, monkeypatch) -> None:
    """外部回执：这一级指定了审批人时，回执方身份不符 → 403，审批继续等。"""
    from automind.core import webhooks

    monkeypatch.setattr(webhooks, "approval_receipt_state",
                        lambda aid, pending: {"resolvable": aid in pending})

    async def _make():
        return asyncio.get_running_loop().create_future()

    fut = env["c"].portal.call(_make)
    srv._pending_approvals["ap1"] = {"future": fut, "required": ["role:finance"],
                                     "client_id": "http", "session_id": "s", "tool": "t"}
    try:
        bad = env["c"].post("/api/approvals/ap1", json={
            "approved": True, "approver": {"user": "dev", "role": "developer"}})
        assert bad.status_code == 403 and bad.json()["type"] == "approver_mismatch"
        assert not fut.done()
        good = env["c"].post("/api/approvals/ap1", json={
            "approved": True, "approver": {"user": "fin", "role": "finance"}})
        assert good.status_code == 200
        assert fut.result()["approver"]["user"] == "fin"
    finally:
        srv._pending_approvals.pop("ap1", None)
