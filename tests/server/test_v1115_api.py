"""v1.11.5 接口：内置工作流样板（列表 / 一键安装 / 安装后可运行）与对话要点摘要。"""

# ruff: noqa: F811

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import automind.server as srv
from tests.server.test_workflow_runs_api import env  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def test_samples_listed(env) -> None:
    body = env["c"].get("/api/workflow/samples").json()
    cs = next(s for s in body["samples"] if s["id"] == "customer_service")
    assert cs["name"] == "在线客服" and cs["installed"] is False
    assert "customer-service-kb.md" in cs["extras"] and "contains_any" in cs["yaml"]


def test_install_writes_yaml_and_kb_then_runs(env) -> None:
    r = env["c"].post("/api/workflow/samples/customer_service/install", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["valid"] and body["file"] == "customer_service.yaml"
    assert (env["wf"] / "customer_service.yaml").is_file()
    assert (env["wf"] / "customer-service-kb.md").is_file()
    listed = env["c"].get("/api/workflow/samples").json()["samples"]
    assert next(s for s in listed if s["id"] == "customer_service")["installed"] is True

    # 装完即可运行：走固定话术的分支不需要模型。产物写到工作区内的临时目录
    # （文件工具的根在 Agent 创建时就定了，测试里改 project_root 管不到它）
    out = ".workflow_test_tmp/cs-api"
    try:
        run = env["c"].post("/api/workflow/run", json={
            "file": "customer_service.yaml", "wait": True,
            "inputs": {"message": "我想退货", "out_dir": out}}).json()
        assert run["status"] == "ok", run
        status = {s["id"]: s["status"] for s in run["steps"]}
        assert status["refund_reply"] == "ok" and status["shipping_reply"] == "skipped"
        assert "无理由退货" in (ROOT / out / "cs-reply.md").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(ROOT / out, ignore_errors=True)


def test_install_does_not_overwrite(env) -> None:
    (env["wf"] / "customer-service-kb.md").write_text("我的知识库", encoding="utf-8")
    (env["wf"] / "customer_service.yaml").write_text("version: 1\n", encoding="utf-8")
    r = env["c"].post("/api/workflow/samples/customer_service/install", json={})
    assert r.status_code == 409
    assert (env["wf"] / "customer-service-kb.md").read_text(encoding="utf-8") == "我的知识库"


def test_unknown_sample_404(env) -> None:
    assert env["c"].post("/api/workflow/samples/nope/install", json={}).status_code == 404


def test_conversation_summary_falls_back_without_llm(env, monkeypatch) -> None:
    monkeypatch.setattr(srv, "get_agent", lambda: SimpleNamespace(llm=None))
    r = env["c"].post("/api/conversation-summary", json={"transcript": [
        {"role": "user", "content": "帮我把 report.md 改成表格"},
        {"role": "assistant", "content": "已改好 report.md，共 3 列"},
        {"role": "user", "content": "再加一列合计"},
    ]})
    body = r.json()
    assert r.status_code == 200 and body["by"] == "extractive"
    assert body["summary"] and body["turns"] == 2


def test_conversation_summary_validates(env) -> None:
    assert env["c"].post("/api/conversation-summary", json={"transcript": "x"}).status_code == 400
    assert env["c"].post("/api/conversation-summary", json={"transcript": []}).json()["by"] == "none"
