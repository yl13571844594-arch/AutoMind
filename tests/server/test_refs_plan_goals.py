"""v1.11.0：输入框新增的四类引用 —— MCP 连接器 / 技能 / 计划模式 / 长期目标。

每一类都盯两件事：**真的生效**（工具必下发、指令进了提示词、计划先等批准、
目标上下文注入并回写进度），以及**不生效时如实回执**（不存在、未连接、
模式不支持都要说清 —— "我引用了却不生效"是最糟的体验）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.goals import GoalStore, extract_progress
from automind.skills.markdown_skill import MarkdownSkill

# ═══════════════════════════════════════════════════════════
# 计划模式：overrides.plan_review
# ═══════════════════════════════════════════════════════════


def test_plan_review_forces_work_mode_for_this_message():
    inter, persist, rec = srv._resolve_overrides(
        {"interaction": "coding", "overrides": {"plan_review": True}})
    assert (inter, persist) == ("work", False), "计划模式要先出整份计划 → 走工作模式，且不改默认模式"
    assert rec["plan"]["applied"] and "编程" in rec["plan"]["note"]


def test_plan_review_not_applicable_to_multi_or_http(monkeypatch):
    monkeypatch.setattr(srv._edition, "has_feature", lambda _f: True)
    inter, _, rec = srv._resolve_overrides(
        {"interaction": "chat", "overrides": {"mode": "multi", "plan_review": True}})
    assert inter == "multi" and rec["plan"]["applied"] is False
    inter, _, rec = srv._resolve_overrides(
        {"interaction": "work", "overrides": {"plan_review": True}}, allow_plan=False)
    assert rec["plan"]["applied"] is False and "WebSocket" in rec["plan"]["reason"]


# ═══════════════════════════════════════════════════════════
# MCP / 技能引用：overrides.mcp / overrides.skills
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    md_dir = tmp_path / "s" / "review"
    md_dir.mkdir(parents=True)
    (md_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: 代码评审清单\n---\n先看边界条件，再看并发。", encoding="utf-8")
    md = MarkdownSkill(md_dir / "SKILL.md")
    py = SimpleNamespace(name="excel_report", description="生成 Excel 报表", input_schema=None)

    class _Reg:
        skills = {"review": md, "excel_report": py}

        def __contains__(self, n):
            return n in self.skills

        def get(self, n):
            return self.skills[n]

    tools = [SimpleNamespace(name=n) for n in
             ("mcp__github__list_issues", "mcp__github__create_pr", "mcp__fs__read")]
    agent = SimpleNamespace(skill_registry=_Reg(),
                            mcp_registry=SimpleNamespace(get_all_tools=lambda: tools))
    monkeypatch.setattr(srv, "get_agent", lambda: agent)
    monkeypatch.setattr(srv, "_read_config", lambda: {"mcp_servers": [
        {"name": "github"}, {"name": "fs"}, {"name": "slack"}]})
    return agent


def test_mcp_reference_expands_to_tools_and_reports_failures(fake_agent):
    rec: dict = {}
    task, prefer = srv._apply_references(
        "整理 issue", {"overrides": {"mcp": ["github", "slack", "nope"]}}, rec)
    assert prefer == ["mcp__github__list_issues", "mcp__github__create_pr"]
    assert "【引用 MCP 连接器】" in task and task.endswith("整理 issue")
    assert rec["mcp"]["applied"] == [{"name": "github", "tools": 2}]
    reasons = {d["name"]: d["reason"] for d in rec["mcp"]["dropped"]}
    assert "未连接" in reasons["slack"] and "不存在" in reasons["nope"]


def test_skill_reference_injects_markdown_and_exposes_python(fake_agent):
    rec: dict = {}
    task, prefer = srv._apply_references(
        "评审这个 PR", {"overrides": {"skills": ["review", "excel_report", "ghost"]}}, rec)
    assert "先看边界条件" in task, "Markdown 技能的指令正文必须真的进提示词"
    assert 'skill_invoke(name="excel_report"' in task
    assert prefer == ["skill_invoke"]
    kinds = {a["name"]: a["kind"] for a in rec["skills"]["applied"]}
    assert kinds == {"review": "markdown", "excel_report": "python"}
    assert rec["skills"]["dropped"][0]["name"] == "ghost"


def test_no_references_is_a_noop(fake_agent):
    assert srv._apply_references("x", {}, {}) == ("x", [])


def test_react_executor_always_sends_preferred_tools():
    from automind.planning.react_executor import ReActExecutor

    names = [f"t{i}" for i in range(40)] + ["mcp__github__create_pr"]
    reg = SimpleNamespace(list_names=lambda: names, list_all=lambda: [])
    ex = ReActExecutor(SimpleNamespace(), reg, tool_budget=5,
                       prefer_tools=["mcp__github__create_pr", "not_registered"])
    ex._select_tools("随便做点什么", "")
    assert "mcp__github__create_pr" in ex._active_tools
    assert "not_registered" not in ex._active_tools


# ═══════════════════════════════════════════════════════════
# 长期目标：存储 / 注入 / 回写
# ═══════════════════════════════════════════════════════════


def test_goal_store_lifecycle(tmp_path):
    st = GoalStore(tmp_path / "goals.json")
    g = st.create("me", "_w1", "覆盖率到 80%", "测试用例 + 报告", "pytest --cov ≥ 80%")
    assert st.list("me", "_w1")[0]["id"] == g["id"]
    assert st.list("other", "_w1") == [], "按 owner 隔离"
    assert st.list("me", "_w2") == [], "按工作区隔离"

    st.record_run(g["id"], task="补 core 的测试", success=True,
                  output="……做完了。\n目标进度：core 已到 72%，还差 planning 模块")
    st.record_run(g["id"], task="补 planning", success=True,
                  output="目标进度：总覆盖率 81%\n【目标已达成】")
    g2 = st.get(g["id"])
    assert g2["run_count"] == 2 and g2["progress"] == "总覆盖率 81%"
    assert g2["suggest_done"] is True and g2["status"] == "active", "模型自称达成只是建议，由人确认"
    st.update(g["id"], status="done")
    assert st.get(g["id"])["suggest_done"] is False
    with pytest.raises(ValueError):
        st.update(g["id"], status="bogus")
    with pytest.raises(ValueError):
        st.create("me", "", "  ")
    assert st.delete(g["id"]) and st.get(g["id"]) is None


def test_goal_context_block_gives_facts_and_asks_for_progress_line(tmp_path):
    st = GoalStore(tmp_path / "goals.json")
    g = st.create("me", "", "竞品分析", "一份 10 页报告")
    st.record_run(g["id"], task="收集资料", success=True, output="目标进度：资料已齐")
    block = GoalStore.context_block(st.get(g["id"]))
    assert "竞品分析" in block and "一份 10 页报告" in block and "资料已齐" in block
    assert "目标进度：" in block and "【目标已达成】" in block


def test_extract_progress_takes_last_line():
    assert extract_progress("目标进度：A\n...\n目标进度: B") == ("B", False)
    assert extract_progress("无") == ("", False)


def test_apply_goal_and_finish(tmp_path, monkeypatch):
    from automind import goals as goals_mod
    st = GoalStore(tmp_path / "goals.json")
    monkeypatch.setattr(goals_mod, "_store", st)
    g = st.create("me", "", "写手册")
    rec: dict = {}
    task, gid = srv._apply_goal("写第一章", {"overrides": {"goal": g["id"]}}, rec)
    assert gid == g["id"] and "【目标交付模式】" in task and rec["goal"]["applied"]

    rec = {}
    assert srv._apply_goal("x", {"overrides": {"goal": "g_missing"}}, rec) == ("x", "")
    assert rec["goal"]["applied"] is False

    st.update(g["id"], status="archived")
    rec = {}
    assert srv._apply_goal("x", {"overrides": {"goal": g["id"]}}, rec)[1] == ""
    assert "归档" in rec["goal"]["reason"]
    st.update(g["id"], status="active")

    monkeypatch.setattr(srv, "_task_history", [
        {"session_id": "run1", "success": True, "output": "截断版", "tokens": 1200,
         "interaction": "work", "duration_ms": 10}])
    out = srv._goal_finish({"id": g["id"], "output": "完整版\n目标进度：第一章完成"},
                           "run1", "写第一章")
    assert out["run_count"] == 1 and out["progress"] == "第一章完成" and out["tokens"] == 1200
    # 找不到本轮记录 = 被停止 / 出错
    out = srv._goal_finish({"id": g["id"]}, "run_gone", "写第二章")
    assert out["runs"][-1]["status"] == "interrupted"
    assert srv._goal_finish({}, "x", "y") is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    from automind import goals as goals_mod
    monkeypatch.setattr(goals_mod, "_store", GoalStore(tmp_path / "goals.json"))
    srv._AUTH_TOKEN = ""
    yield TestClient(srv.app, client=("127.0.0.1", 51000))


def test_goals_api_roundtrip_and_isolation(client):
    c = client
    r = c.post("/api/goals", json={"owner": "me", "workspace": "_w", "title": "上线 v2",
                                   "deliverable": "发布说明"}).json()
    gid = r["goal"]["id"]
    assert c.post("/api/goals", json={"owner": "me", "title": ""}).status_code == 400
    lst = c.get("/api/goals", params={"owner": "me", "workspace": "_w"}).json()["goals"]
    assert [g["id"] for g in lst] == [gid] and "runs" not in lst[0]
    assert c.get(f"/api/goals/{gid}", params={"owner": "someone"}).status_code == 404
    r = c.patch(f"/api/goals/{gid}", json={"owner": "me", "status": "paused", "notes": "别动 db"})
    assert r.json()["goal"]["status"] == "paused"
    assert c.patch(f"/api/goals/{gid}", json={"owner": "me", "status": "x"}).status_code == 400
    assert c.delete(f"/api/goals/{gid}", params={"owner": "other"}).status_code == 404
    assert c.delete(f"/api/goals/{gid}", params={"owner": "me"}).json()["status"] == "ok"


# ═══════════════════════════════════════════════════════════
# 计划审批闸门（agent 侧）
# ═══════════════════════════════════════════════════════════


def _plan_agent(answer):
    from automind.agent import AutoMindAgent
    from automind.core.types import Goal, HierarchicalPlan

    agent = object.__new__(AutoMindAgent)
    events: list = []

    async def _sink(ev):
        events.append(ev)
    agent.event_sink = _sink
    agent._mode = None
    agent._active_goal_id = None
    root = Goal(id="root", description="root", children=[
        Goal(id="g1", description="读配置"), Goal(id="g2", description="改代码")])
    plan = HierarchicalPlan(task_description="t", root_goal=root)
    replans: list = []

    async def _plan(task, ctx, tools):
        replans.append(task)
        return plan
    agent.hierarchical_planner = SimpleNamespace(plan=_plan)
    agent.tool_registry = SimpleNamespace(list_all=lambda: [])
    asked: list = []

    async def _cb(tool, args, tier, reason):
        asked.append((tool, args))
        return answer
    agent.plan_review_cb = _cb
    executed: list = []

    async def _exec(p):
        executed.append(p)
        return ["step"]
    agent._execute_plan = _exec
    return agent, events, asked, executed, replans


def test_plan_runs_only_after_approval():
    agent, events, asked, executed, _ = _plan_agent(True)
    _, steps = asyncio.run(agent._run_plan_execute("t", ""))
    assert asked[0][0] == "plan_review" and "1. 读配置" in asked[0][1]["plan"]
    assert executed and steps == ["step"] and agent._plan_rejected == ""
    assert [e["state"] for e in events if e["type"] == "plan_review"] == ["waiting", "approved"]


def test_rejected_plan_executes_nothing():
    agent, events, _, executed, _ = _plan_agent({"approved": False, "comment": "方向不对"})
    _, steps = asyncio.run(agent._run_plan_execute("t", ""))
    assert not executed and steps == [] and agent._plan_rejected == "方向不对"


def test_review_channel_failure_is_fail_closed():
    agent, _, _, executed, _ = _plan_agent(None)

    async def _boom(*_a):
        raise ConnectionError("ws closed")
    agent.plan_review_cb = _boom
    asyncio.run(agent._run_plan_execute("t", ""))
    assert not executed and "异常" in agent._plan_rejected


def test_edited_plan_is_replanned_then_executed():
    agent, events, _, executed, replans = _plan_agent(
        {"approved": True, "arguments": {"plan": "1. 只读配置"}})
    asyncio.run(agent._run_plan_execute("t", ""))
    assert len(replans) == 2 and "1. 只读配置" in replans[1]
    assert executed
    assert any(e.get("edited") for e in events if e["type"] == "plan_review")


def test_no_review_callback_means_old_behaviour():
    agent, _, asked, executed, _ = _plan_agent(True)
    agent.plan_review_cb = None
    asyncio.run(agent._run_plan_execute("t", ""))
    assert executed and not asked


# ═══════════════════════════════════════════════════════════
# skill_invoke：技能对模型可达
# ═══════════════════════════════════════════════════════════


def test_skill_invoke_tool_runs_and_reports_unknown(tmp_path):
    from automind.agent import _SkillInvokeTool
    from automind.skills.skill_registry import SkillRegistry

    reg = SkillRegistry()
    d = tmp_path / "s" / "tone"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: tone\n---\n语气要克制。", encoding="utf-8")
    reg.register(MarkdownSkill(d / "SKILL.md"))
    tool = _SkillInvokeTool(SimpleNamespace(skill_registry=reg))
    ok = asyncio.run(tool.execute(name="tone"))
    assert ok.success and "语气要克制" in ok.output
    bad = asyncio.run(tool.execute(name="ghost"))
    assert not bad.success and "tone" in bad.error
