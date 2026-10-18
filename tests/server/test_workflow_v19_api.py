"""v1.9.0 社区版一侧的服务端行为：专业版入口的降级、入站 webhook 的鉴权豁免、
触发运行的专用会话、运行记录的保留口径。专业版的真实行为见 pro/tests/test_workflow_pro.py。
"""

# 夹具 env 从 v1.8 的接口测试里复用：测试函数的同名参数会被 ruff 当成重定义（F811）
# ruff: noqa: F811

from __future__ import annotations

import pytest

import automind.server as srv
from tests.server.test_workflow_runs_api import env, wait_done, write  # noqa: F401


@pytest.mark.parametrize(("method", "path", "feature"), [
    ("get", "/api/workflow/triggers", "workflow_triggers"),
    ("post", "/api/workflow/triggers/tgx/toggle", "workflow_triggers"),
    ("post", "/api/workflow/triggers/tgx/token", "workflow_triggers"),
    ("post", "/api/workflow/triggers/tgx/run", "workflow_triggers"),
    ("post", "/api/workflow/hooks/tgx", "workflow_triggers"),
    ("get", "/api/workflow/dashboard", "workflow_history"),
])
def test_pro_entrypoints_are_locked_with_upgrade_hint(env, method, path, feature) -> None:
    r = getattr(env["c"], method)(path)
    assert r.status_code == 403
    body = r.json()
    assert body["feature"] == feature and "专业版" in body["error"]


def test_hook_path_does_not_need_platform_token(env, monkeypatch) -> None:
    """入站 webhook 凭自己的令牌：开了访问令牌时，调用方不必持有整个平台的令牌。"""
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "platform-secret")
    monkeypatch.setattr(srv, "_auth_token", lambda: "platform-secret")
    # 社区版：越过了平台令牌这一关，落到降级路由（403 升级提示），而不是 401
    assert env["c"].post("/api/workflow/hooks/tgx", json={}).status_code == 403
    assert env["c"].get("/api/workflow/triggers").status_code == 401


def test_trigger_management_is_an_admin_action() -> None:
    assert srv._is_admin_path("/api/workflow/triggers/tgx/toggle")
    assert not srv._is_admin_path("/api/workflow/hooks/tgx")


def test_list_exposes_trigger_declarations_but_community_cannot_load_them(env) -> None:
    write(env, "t.yaml", """
        version: 1
        name: 定时
        triggers:
          - cron: "0 9 * * *"
        steps:
          - {id: s, type: tool, tool: wf_probe}
        """)
    row = env["c"].get("/api/workflow/list").json()["workflows"][0]
    assert row["valid"] is False and row["triggers"] == []
    assert any("需要专业版" in e["message"] for e in row["errors"])


def test_dedicated_session_names() -> None:
    assert srv._wf_session_for("daily_report.yaml") == "wf_daily_report"
    zh = srv._wf_session_for("日报.yaml")
    assert zh.startswith("wf_") and len(zh) == 13 and zh == srv._wf_session_for("日报.yaml")


def test_community_retention_is_by_count() -> None:
    assert srv._wf_retention() == (50, None)


def test_launch_helper_shares_every_check(env) -> None:
    """触发器发起运行走的内部入口与 REST 同一套判定（校验 / 入参 / 限额）。"""
    write(env, "need.yaml", """
        version: 1
        name: 要参数
        inputs:
          who: {type: string, required: true}
        steps:
          - {id: s, type: tool, tool: wf_probe, args: {value: "{{ inputs.who }}"}}
        """)
    portal = env["c"].portal
    bad = portal.call(lambda: srv._launch_workflow(file_name="need.yaml", inputs={}))
    assert bad["ok"] is False and bad["status"] == 422 and "who" in bad["error"]
    missing = portal.call(lambda: srv._launch_workflow(file_name="nope.yaml"))
    assert missing["ok"] is False and missing["status"] == 404
    good = portal.call(lambda: srv._launch_workflow(
        file_name="need.yaml", inputs={"who": "x"}, trigger="cron", trigger_key="tgk",
        retry_left=2))
    assert good["ok"] and good["session_id"] == "wf_need"
    done = wait_done(env["c"], good["run_id"])
    assert done["trigger"] == "cron" and done["trigger_key"] == "tgk"
    rec = srv._wf_store().load(good["run_id"])
    assert rec["retry_left"] == 2 and rec["attempt"] == 1
    assert env["c"].get("/api/workflow/runs?trigger_key=tgk").json()["runs"][0]["run_id"] \
        == good["run_id"]
