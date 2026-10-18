"""v1.8.0 工作流运行接口：异步运行 / 查询 / 取消 / 断点续跑 / 限额 / 出站事件。

对应 docs/WORKFLOW_ROADMAP.md 的 R-101 ~ R-105、R-110。全部离线：
工作流里只用一个测试专用的假工具（可调延迟、可按脚本失败、记录调用次数），
"续跑不重复执行已完成步骤"这类断言只能靠调用计数来证明。

注意 ``with TestClient(...)``：后台运行是挂在服务端事件循环上的任务，不用上下文
管理器的话 TestClient 每个请求都会新建再销毁一个循环，后台任务会被连带取消。
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool
from automind.workflow.runs import WorkflowRunStore


class ProbeTool(AbstractTool):
    name = "wf_probe"
    description = "测试用：按参数延迟、按脚本失败、记录调用"
    parameters = {"type": "object", "properties": {
        "value": {"type": "string"}, "delay": {"type": "number"}}}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures: dict[str, list[str]] = {}

    def count(self, value: str) -> int:
        return sum(1 for c in self.calls if c.get("value") == value)

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        delay = float(kwargs.get("delay") or 0)
        if delay:
            await asyncio.sleep(delay)
        queue = self.failures.get(str(kwargs.get("value")))
        if queue:
            return ToolResult(tool_name=self.name, success=False, error=queue.pop(0))
        return ToolResult(tool_name=self.name, success=True,
                          output={"value": kwargs.get("value")})


@pytest.fixture
def env(tmp_path, monkeypatch):
    srv._store.config_file = tmp_path / "cfg.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._store.chats_dir = tmp_path / "chats"
    srv._AUTH_TOKEN = ""
    srv._agent = None
    srv._wf_live.clear()
    monkeypatch.setattr(srv, "_wf_store_obj", WorkflowRunStore(tmp_path / "runs"))
    consumed: list[int] = []
    monkeypatch.setattr(srv._quota, "try_consume_task",
                        lambda: (consumed.append(1) or True, ""))
    monkeypatch.setattr(srv._quota, "refund_task", lambda: consumed.pop() if consumed else None)
    events: list[tuple[str, dict]] = []
    from automind.core import webhooks
    monkeypatch.setattr(webhooks, "emit", lambda e, p=None: events.append((str(e), p)) or 1)
    proj = tmp_path / "proj"
    (proj / "workflows").mkdir(parents=True)
    with TestClient(srv.app, client=("127.0.0.1", 51000)) as c:
        agent = srv.get_agent()
        agent.config.project_root = str(proj)
        tool = ProbeTool()
        agent.tool_registry.register(tool)
        yield {"c": c, "agent": agent, "tool": tool, "wf": proj / "workflows",
               "consumed": consumed, "events": events}
    srv._agent = None
    srv._wf_live.clear()


def write(env, name: str, text: str) -> None:
    (env["wf"] / name).write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def wait_done(c: TestClient, run_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/workflow/runs/{run_id}").json()
        if r.get("status") not in ("running", None) and not r.get("live"):
            return r
        time.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 在 {timeout}s 内没有结束")


THREE = """
version: 1
name: 三步
steps:
  - id: a
    type: tool
    tool: wf_probe
    args: {value: a}
  - id: b
    type: tool
    tool: wf_probe
    args: {value: b}
  - id: c
    type: tool
    tool: wf_probe
    args: {value: c}
"""


def test_run_returns_immediately_and_reports_via_query(env):
    """R-101：HTTP 立即返回 run_id；R-103：写入任务历史并计 1 次额度。"""
    write(env, "slow.yaml", """
        version: 1
        name: 慢流程
        steps:
          - id: wait
            type: tool
            tool: wf_probe
            args: {value: w, delay: 1.5}
        """)
    t0 = time.perf_counter()
    r = env["c"].post("/api/workflow/run", json={"file": "slow.yaml", "session_id": "s1"})
    assert time.perf_counter() - t0 < 1.0, "运行接口应当立即返回，而不是等工作流跑完"
    body = r.json()
    assert r.status_code == 200 and body["status"] == "running" and body["run_id"]
    live = env["c"].get(f"/api/workflow/runs/{body['run_id']}").json()
    assert live["live"] is True and live["status"] == "running"
    done = wait_done(env["c"], body["run_id"])
    assert done["status"] == "completed" and done["report"]["status"] == "ok"
    assert env["consumed"] == [1]
    hist = [h for h in srv._task_history if h.get("session_id") == body["run_id"]]
    assert hist and hist[-1]["interaction"] == "workflow"
    assert hist[-1]["workflow_run"]["summary"]["ok"] is True
    runs = env["c"].get("/api/workflow/runs").json()["runs"]
    assert runs[0]["run_id"] == body["run_id"]


def test_wait_keeps_synchronous_semantics_for_ci(env):
    write(env, "three.yaml", THREE)
    r = env["c"].post("/api/workflow/run", json={"file": "three.yaml", "wait": True})
    report = r.json()
    assert report["status"] == "ok" and report["summary"]["succeeded"] == 3


def test_dry_run_is_synchronous_and_free(env):
    write(env, "three.yaml", THREE)
    r = env["c"].post("/api/workflow/run", json={"file": "three.yaml", "dry_run": True})
    assert r.json()["status"] == "dry_run"
    assert env["tool"].calls == [] and env["consumed"] == []


def test_cancel_marks_step_and_run_cancelled(env):
    """R-102：运行中取消 → 当前步骤 cancelled、整轮 cancelled，记录可续跑。"""
    write(env, "slow.yaml", """
        version: 1
        name: 可取消
        steps:
          - id: long
            type: tool
            tool: wf_probe
            args: {value: long, delay: 20}
          - id: after
            type: tool
            tool: wf_probe
            args: {value: after}
        """)
    run_id = env["c"].post("/api/workflow/run", json={"file": "slow.yaml"}).json()["run_id"]
    time.sleep(0.3)
    assert env["c"].post(f"/api/workflow/runs/{run_id}/cancel").json()["ok"] is True
    done = wait_done(env["c"], run_id)
    assert done["status"] == "interrupted" and done["resumable"] is True
    steps = {s["id"]: s["status"] for s in done["report"]["steps"]}
    assert steps == {"long": "cancelled", "after": "skipped"}
    assert env["tool"].count("after") == 0
    # 已结束的运行再取消 → 409
    assert env["c"].post(f"/api/workflow/runs/{run_id}/cancel").status_code == 409


def test_resume_skips_completed_steps(env):
    """R-104：第 2 步失败中止 → 续跑只从第 2 步开始，第 1 步不重复执行（调用计数可证）。"""
    write(env, "three.yaml", THREE)
    env["tool"].failures["b"] = ["下游暂时不可用"]
    first = env["c"].post("/api/workflow/run", json={"file": "three.yaml"}).json()["run_id"]
    done = wait_done(env["c"], first)
    assert done["status"] == "partial" and done["report"]["status"] == "aborted"
    assert (env["tool"].count("a"), env["tool"].count("b"), env["tool"].count("c")) == (1, 1, 0)

    r = env["c"].post(f"/api/workflow/runs/{first}/resume", json={}).json()
    assert r["ok"] and r["reused_steps"] == ["a"] and r["resumed_from"] == first
    second = wait_done(env["c"], r["run_id"])
    assert second["status"] == "completed"
    assert (env["tool"].count("a"), env["tool"].count("b"), env["tool"].count("c")) == (1, 2, 1)
    report = second["report"]
    assert report["resumed_from"] == first and report["resumed_steps"] == ["a"]
    a = next(s for s in report["steps"] if s["id"] == "a")
    assert a["status"] == "ok" and a["detail"]["resumed"] is True
    # 源运行被标记为已续跑，不能再续一次
    again = env["c"].post(f"/api/workflow/runs/{first}/resume", json={})
    assert again.status_code == 409 and "续跑过" in again.json()["error"]
    assert env["consumed"] == [1, 1]


def test_resume_refused_when_file_changed(env):
    write(env, "three.yaml", THREE)
    env["tool"].failures["b"] = ["x"]
    first = env["c"].post("/api/workflow/run", json={"file": "three.yaml"}).json()["run_id"]
    wait_done(env["c"], first)
    write(env, "three.yaml", THREE.replace("name: 三步", "name: 三步（改）"))
    r = env["c"].post(f"/api/workflow/runs/{first}/resume", json={})
    assert r.status_code == 409 and r.json()["type"] == "digest_mismatch"


def test_dismiss_stops_resume_prompt(env):
    write(env, "three.yaml", THREE)
    env["tool"].failures["a"] = ["x"]
    first = env["c"].post("/api/workflow/run", json={"file": "three.yaml"}).json()["run_id"]
    wait_done(env["c"], first)
    assert env["c"].post(f"/api/workflow/runs/{first}/dismiss").json()["ok"]
    row = env["c"].get("/api/workflow/runs").json()["runs"][0]
    assert row["status"] == "dismissed" and row["resumable"] is False


def test_workflow_count_limit_blocks_run_not_listing(env, monkeypatch):
    """R-110：超出社区版上限的工作流可列出、可 dry-run，但不能运行。"""
    monkeypatch.setitem(srv._quota.QUOTA_RULES["community"], "workflows", 1)
    write(env, "a.yaml", THREE)
    write(env, "b.yaml", THREE.replace("name: 三步", "name: 第二个"))
    rows = {w["file"]: w for w in env["c"].get("/api/workflow/list").json()["workflows"]}
    assert rows["a.yaml"]["runnable"] is True and rows["b.yaml"]["runnable"] is False
    assert "上限（1 个工作流）" in rows["b.yaml"]["blocked_reason"]
    assert env["c"].post("/api/workflow/run",
                         json={"file": "b.yaml", "dry_run": True}).json()["status"] == "dry_run"
    r = env["c"].post("/api/workflow/run", json={"file": "b.yaml"})
    assert r.status_code == 403 and r.json()["type"] == "workflow_quota"
    assert env["consumed"] == []


def test_failed_run_emits_outbound_event(env):
    """R-110：失败 / 部分成功的运行触发 workflow.failed / workflow.partial。"""
    write(env, "three.yaml", THREE)
    env["tool"].failures["a"] = ["源系统 500"]
    run_id = env["c"].post("/api/workflow/run", json={"file": "three.yaml"}).json()["run_id"]
    wait_done(env["c"], run_id)
    names = [e for e, _ in env["events"]]
    assert "workflow.failed" in names
    payload = next(p for e, p in env["events"] if e == "workflow.failed")
    assert payload["event"] == "workflow.failed" and payload["status"] == "aborted"
    assert payload["extra"]["run_id"] == run_id and payload["extra"]["failed_step"] == "a"
    assert payload["extra"]["resumable"] is True
    assert "源系统 500" in payload["error"]


def test_partial_run_emits_partial_event(env):
    write(env, "cont.yaml", """
        version: 1
        name: 继续
        steps:
          - id: a
            type: tool
            tool: wf_probe
            args: {value: a}
            on_failure: continue
          - id: b
            type: tool
            tool: wf_probe
            args: {value: b}
        """)
    env["tool"].failures["a"] = ["x"]
    run_id = env["c"].post("/api/workflow/run", json={"file": "cont.yaml"}).json()["run_id"]
    assert wait_done(env["c"], run_id)["report"]["status"] == "partial"
    assert [e for e, _ in env["events"]].count("workflow.partial") == 1


def test_human_step_uses_the_shared_approval_chain(env):
    """修正：此前 REST 路径把 (tool, args, tier, reason) 形状的回调直接当作
    (step, prompt) 调用，human 步骤永远因为 TypeError 被判"审批通道异常"。
    现在走同一条审批链路：没有界面、没有外部通道时如实按拒绝收尾。"""
    write(env, "approve.yaml", """
        version: 1
        name: 审批
        steps:
          - id: ok
            type: human
            prompt: 请批准
        """)
    report = env["c"].post("/api/workflow/run",
                           json={"file": "approve.yaml", "wait": True}).json()
    step = report["steps"][0]
    assert step["status"] == "aborted"
    assert "审批通道异常" not in step["error"]
    assert "没有可送达的界面" in step["error"]


def test_save_source_delete_roundtrip(env):
    c = env["c"]
    r = c.post("/api/workflow/save", json={"file": "new", "yaml": THREE}).json()
    assert r["ok"] and r["file"] == "new.yaml" and r["valid"] is True
    assert c.post("/api/workflow/save", json={"file": "new.yaml", "yaml": THREE}).status_code == 409
    bad = c.post("/api/workflow/save",
                 json={"file": "new.yaml", "yaml": "version: 1\nname: x\n", "overwrite": True}).json()
    assert bad["ok"] and bad["valid"] is False and bad["errors"]
    assert c.get("/api/workflow/source", params={"file": "new.yaml"}).json()["yaml"].startswith("version")
    assert c.post("/api/workflow/save", json={"file": "../evil", "yaml": THREE}).json()["file"] == "evil.yaml"
    assert c.post("/api/workflow/delete", json={"file": "new.yaml"}).json()["ok"]
    assert not (Path(env["wf"]) / "new.yaml").exists()
    assert c.post("/api/workflow/delete", json={"file": "../x.yaml"}).status_code in (403, 404)


def test_run_endpoints_are_admin_only_remotely(env, monkeypatch):
    monkeypatch.delenv("AUTOMIND_ADMIN_TOKEN", raising=False)
    remote = TestClient(srv.app, client=("10.0.0.8", 51000))
    for path in ("/api/workflow/runs/x/cancel", "/api/workflow/runs/x/resume",
                 "/api/workflow/save", "/api/workflow/delete"):
        assert remote.post(path, json={}).status_code == 403, path
