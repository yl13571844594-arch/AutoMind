"""v1.7.7 P0-1：断点续跑接到 Web —— 检查点记录真实进度、按会话精确匹配、真正接着跑。"""

from __future__ import annotations

import pytest

from automind.agent import build_react_resume_prompt
from automind.core.types import AgentState
from automind.state.checkpoint import CheckpointManager, session_of


def _save(mgr: CheckpointManager, sid: str, **metadata) -> str:
    import asyncio
    st = AgentState(session_id=sid, metadata=metadata)
    return asyncio.run(mgr.save(st))


def test_session_of_is_exact():
    assert session_of("ckpt_s_abc_1790000000_000123") == "s_abc"
    assert session_of("ckpt_s_abc_c1234_1790000000_000123") == "s_abc_c1234"
    assert session_of("garbage") == ""


def test_latest_for_session_does_not_leak_across_conversations(tmp_path):
    mgr = CheckpointManager(tmp_path)
    _save(mgr, "s_abc_c1234", task="别的会话", status="running")
    assert mgr.latest_for_session("s_abc") is None
    assert mgr.resumable_for_session("s_abc") is None


def test_react_checkpoint_is_resumable_until_completed(tmp_path):
    mgr = CheckpointManager(tmp_path)
    cid = _save(mgr, "s1", task="写个爬虫", interaction="coding", status="interrupted",
                react={"completed_actions": 3, "actions": [{"tool": "file_write", "ok": True}],
                       "artifacts": ["crawl.py"]})
    info = mgr.resumable_for_session("s1")
    assert info["id"] == cid and info["kind"] == "react" and info["done"] == 3
    assert info["task"] == "写个爬虫" and info["interaction"] == "coding"
    # 用户忽略 → 不再提示
    assert mgr.mark(cid, "dismissed")
    assert mgr.resumable_for_session("s1") is None


def test_newer_completed_task_supersedes_old_interrupted(tmp_path):
    import time
    mgr = CheckpointManager(tmp_path)
    _save(mgr, "s1", task="旧任务", status="interrupted", react={"actions": [{}]})
    time.sleep(1.1)
    _save(mgr, "s1", task="新任务", status="completed")
    assert mgr.resumable_for_session("s1") is None


def test_prune_keeps_latest_per_session(tmp_path):
    import asyncio
    import time
    mgr = CheckpointManager(tmp_path)
    keep = _save(mgr, "quiet", task="慢任务", status="running", react={"actions": [{}]})
    time.sleep(0.02)
    for _ in range(25):
        _save(mgr, "busy", task="x", status="running")
    asyncio.run(mgr.prune(max_checkpoints=5))
    assert (tmp_path / f"{keep}.json").exists()


def test_resume_prompt_lists_real_progress():
    prompt = build_react_resume_prompt("实现登录", {
        "actions": [{"tool": "file_write", "ok": True, "args": {"path": "auth.py"},
                     "artifact": "auth.py"},
                    {"tool": "terminal", "ok": False, "args": {"command": "pytest"}}],
        "artifacts": ["auth.py"], "open_failures": {"terminal": "2 failed"},
        "interrupted_reason": "LLMConnectionError", "last_thought": "接下来修测试"})
    assert "实现登录" in prompt and "file_write(path=auth.py)" in prompt
    assert "auth.py" in prompt and "2 failed" in prompt and "不要重做" in prompt
    assert prompt.count("【断点续跑】") == 1


# ── Agent 集成：ReAct 被中断 → 检查点带进度 → resume 带着进度重跑 ─────────


@pytest.mark.asyncio
async def test_agent_resume_react_uses_recorded_progress(tmp_path, monkeypatch):
    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig
    from automind.core.types import AgentResult, ExecutionMode

    cfg = AgentConfig()
    cfg.execution.checkpoint_dir = str(tmp_path / "ck")
    agent = AutoMindAgent(cfg)
    agent._mode = ExecutionMode.REACT
    agent.session_id = "s_web"
    cid = await agent.checkpoint_mgr.save(AgentState(session_id="s_web", metadata=dict(
        task="重构 utils", status="interrupted", interaction="coding",
        react={"completed_actions": 1, "artifacts": ["utils.py"],
               "actions": [{"tool": "file_edit", "ok": True, "args": {"path": "utils.py"}}]})))
    seen = {}

    async def fake_run(prompt):
        seen["prompt"] = prompt
        seen["origin"] = dict(agent._resume_origin or {})
        return AgentResult(success=True, output="ok")

    monkeypatch.setattr(agent, "run", fake_run)
    await agent.resume_from_checkpoint(cid)
    assert "重构 utils" in seen["prompt"] and "file_edit(path=utils.py)" in seen["prompt"]
    # 续跑的任务原文仍是原任务（不会越套越长）
    assert seen["origin"] == {"task": "重构 utils", "checkpoint": cid}
    # 源断点标记为 resumed，不会被再次续跑
    assert agent.checkpoint_mgr.resumable_for_session("s_web") is None


def test_begin_record_uses_original_task_when_resuming(tmp_path):
    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig

    cfg = AgentConfig()
    cfg.execution.checkpoint_dir = str(tmp_path / "ck")
    agent = AutoMindAgent(cfg)
    agent._current_plan = object()                  # 上一轮计划任务的残留
    agent._resume_origin = {"task": "原任务", "checkpoint": "ckpt_x_1_2"}
    agent._begin_checkpoint_record("【断点续跑】……很长的提示")
    md = agent._agent_state.metadata
    assert md["task"] == "原任务" and md["resumed_from"] == "ckpt_x_1_2"
    assert md["status"] == "running" and agent._current_plan is None
