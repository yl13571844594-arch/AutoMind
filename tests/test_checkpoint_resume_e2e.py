"""v1.11.16 断点续跑真正可用 —— 端到端（不联网、全部临时目录）。

覆盖四条续跑路径与发现/API 层：

1. **plan**：3 步计划跑完 2 步后中断 → 新 Agent 实例（模拟进程重启）续跑 →
   只执行剩下 1 步、已完成目标不被重置、源检查点标 resumed。
2. **react（工作/编程）**：检查点里记着 2 个真实完成的动作 → 续跑提示列出
   这些动作并明确"不要重做"；**预算窗口/已用量从 0 起算**（上一轮烧光预算
   才停下的场景），否则续跑第一次调用就会再次失败。
3. **multi / loop**：按与 budget 成员约定的 metadata（run_mode/stop_reason/
   budget_exhausted/progress）构造检查点 → 续跑把旧进度交给编排器/循环引擎，
   multi 只补剩余 role、loop 从下一迭代继续；源检查点 resumed、新检查点 completed。
4. **发现与 API**：resumable_for_session / list_resumable 对四种 kind 的发现、
   排除 completed/dismissed/resumed、每会话去重、旧检查点兼容；
   `/api/checkpoints/resumable`、dismiss，以及 WS/REST 在 multi/loop 带
   resume_checkpoint 时**走 resume 而不是 run_multi/run_loop** 的路由验证。

所有模型/特性均为替身：不联网、不调用真实 API；检查点目录指向 tmp_path。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from automind.agent import AutoMindAgent
from automind.core.config import AgentConfig
from automind.core.types import (
    AgentResult,
    AgentState,
    ExecutionMode,
    Goal,
    GoalStatus,
    HierarchicalPlan,
    InteractionMode,
    PlanStatus,
    TokenUsage,
)
from automind.planning.plan_executor import StepResult
from automind.state.checkpoint import CheckpointManager, session_of

# ═══════════════════════════════════════════════════════════════
# 替身与夹具
# ═══════════════════════════════════════════════════════════════


class _FakeLLM:
    """只提供预算相关表面的 LLM 替身（不会被真正调用）。"""

    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.reset_calls = 0
        self.call_timeout = 300.0

    def reset(self) -> None:
        self.reset_calls += 1
        self.usage = TokenUsage()

    def token_count(self, text: str) -> int:
        return len(text or "")


@pytest.fixture()
def cfg(tmp_path):
    """隔离的 Agent 配置：检查点/项目根都落在 tmp_path。"""
    c = AgentConfig()
    c.execution.checkpoint_dir = str(tmp_path / "ckpt")
    c.execution.checkpoint_enabled = True
    c.execution.isolate_workspace = False
    c.project_root = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir(exist_ok=True)
    return c


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """避免轨迹/记忆等旁路写到仓库 .automind。"""
    monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "data"))


def _new_agent(cfg) -> AutoMindAgent:
    """模拟"进程重启后的新 Agent 实例"：配置相同、内存态全空。"""
    agent = AutoMindAgent(cfg)
    agent.llm = _FakeLLM()
    agent.session_id = "s_resume"
    return agent


def _save(mgr: CheckpointManager, state: AgentState) -> str:
    return asyncio.run(mgr.save(state))


def test_checkpoint_save_ids_never_collide_within_one_tick(tmp_path, monkeypatch):
    """Windows 上 time_ns() 同一 clock tick 可能返回相同值 —— id 不能重复。

    旧实现末段取 ``int(time.time_ns() % 1_000_000)``；"预算耗尽写检查点 →
    立刻续跑又写一份"会在同一 tick 内算出相同 id 并**静默覆盖**前一份。
    v1.11.16 改为"6 位微秒 + 4 位进程内自增序号"，这里把时钟钉死在同一个
    纳秒值上，连续保存 20 次（每次都会是同一 tick）验证：
      · 20 个不同 id、20 个文件、无一覆盖；
      · 末段仍是纯数字且 ``session_of()`` 还能正确取回 session（旧格式兼容）。
    """
    from automind.state import checkpoint as checkpoint_mod

    fixed_ns = 1_790_000_000_123_456_789
    monkeypatch.setattr(checkpoint_mod.time, "time_ns", lambda: fixed_ns)

    mgr = CheckpointManager(tmp_path / "tick")
    state = AgentState(session_id="s_tick",
                       metadata={"task": "t", "status": "partial"})
    ids = [_save(mgr, state) for _ in range(20)]

    assert len(ids) == 20 and len(set(ids)) == 20, \
        "同一 clock tick 内的检查点 id 重复（会静默覆盖）"
    files = sorted(mgr.checkpoint_dir.glob("ckpt_*.json"))
    assert len(files) == 20, f"应有 20 个检查点文件，实际 {len(files)} 个（发生覆盖）"
    for cid in ids:
        assert (mgr.checkpoint_dir / f"{cid}.json").is_file(), cid
        tail = cid.rsplit("_", 1)[-1]
        assert tail.isdigit() and len(tail) == 10, f"id 末段应为 6 位微秒+4 位序号：{cid}"
        assert session_of(cid) == "s_tick", cid   # id 格式仍可解析会话归属


def _plan_state() -> AgentState:
    root = Goal(id="root", description="三步任务")
    root.children = [
        Goal(id="g1", description="第一步", status=GoalStatus.COMPLETED),
        Goal(id="g2", description="第二步", status=GoalStatus.COMPLETED),
        Goal(id="g3", description="第三步"),
    ]
    plan = HierarchicalPlan(task_description="三步任务", root_goal=root,
                            status=PlanStatus.EXECUTING,
                            execution_order=["g1", "g2", "g3"])
    return AgentState(session_id="s_plan", plan=plan, metadata={
        "task": "三步任务", "interaction": "work", "status": "running"})


def _react_metadata() -> dict:
    return {
        "task": "重构 auth 模块", "interaction": "coding",
        "status": "partial", "stop_reason": "budget_exhausted",
        "budget_exhausted": True,
        "react": {
            "completed_actions": 2, "failed_actions": 0,
            "actions": [
                {"tool": "file_write", "ok": True, "artifact": "auth.py",
                 "args": {"path": "auth.py"}},
                {"tool": "terminal", "ok": True, "args": {"command": "pytest -q"}},
            ],
            "artifacts": ["auth.py"], "open_failures": {},
            "interrupted_reason": "BudgetExhaustedError: 预算已用尽",
            "stop_reason": "task_budget", "last_thought": "接下来补测试",
        },
    }


def _multi_metadata() -> dict:
    progress = {
        "plan": [{"role": "planner", "subtask": "拆需求"},
                 {"role": "coder", "subtask": "写页面"},
                 {"role": "reviewer", "subtask": "复核"}],
        "steps": [
            {"role": "planner", "ok": True, "output": "需求已拆"},
            {"role": "coder", "ok": True, "output": "页面已写"},
        ],
        "scratch": "已确认需求：登录页 + 注册页",
        "last_output": "前两步完成",
        "steps_done": 2, "steps_total": 3,
    }
    return {"task": "做一个登录网站", "interaction": "multi", "run_mode": "multi",
            "status": "partial", "stop_reason": "budget_exhausted",
            "budget_exhausted": True, "progress": progress}


def _loop_metadata() -> dict:
    progress = {"iterations": 2, "last_output": "第二轮修正完成",
                "feedback": "按钮对齐仍差一点", "max_iterations": 8}
    return {"task": "把按钮调成圆角", "interaction": "loop", "run_mode": "loop",
            "status": "partial", "stop_reason": "budget_exhausted",
            "budget_exhausted": True, "progress": progress}


# ═══════════════════════════════════════════════════════════════
# 1. plan：重启后续跑只执行剩余目标
# ═══════════════════════════════════════════════════════════════


def test_plan_resume_after_restart_only_remaining_steps(cfg):
    mgr = CheckpointManager(cfg.execution.checkpoint_dir)
    cid = _save(mgr, _plan_state())

    info = mgr.resumable_for_session("s_plan")
    assert info is not None and info["kind"] == "plan"
    assert info["progress"] == {"total": 3, "done": 2, "remaining": 1}
    assert (info["done"], info["total"], info["remaining"]) == (2, 3, 1)

    agent = _new_agent(cfg)          # ← 新实例 = 模拟进程重启
    seen: dict = {}

    async def fake_execute_plan(plan):
        seen["statuses"] = {g.id: g.status.value for g in plan.root_goal.children}
        steps = []
        for goal in plan.root_goal.children:
            if goal.status == GoalStatus.COMPLETED:
                continue             # 计划执行器本身就跳过已完成目标
            goal.status = GoalStatus.COMPLETED
            steps.append(StepResult(goal_id=goal.id,
                                    goal_description=goal.description,
                                    success=True))
        plan.status = PlanStatus.COMPLETED
        seen["executed"] = [s.goal_id for s in steps]
        return steps

    agent._execute_plan = fake_execute_plan
    result = asyncio.run(agent.resume_from_checkpoint(cid))

    assert result.success is True
    assert result.steps_executed == 1
    assert seen["executed"] == ["g3"], "续跑重做了已完成的步骤"
    # 已完成目标在交给执行器时仍然保持 COMPLETED（没有被重置成 pending）
    assert seen["statuses"] == {"g1": "completed", "g2": "completed", "g3": "pending"}
    assert "已从断点续跑" in result.output and "2/3" in result.output
    # 源检查点标记 resumed；续跑产生的新检查点已完成 → 会话不再提示续跑
    assert mgr.load_sync(cid).metadata["status"] == "resumed"
    assert mgr.resumable_for_session("s_plan") is None
    latest = mgr.load_sync(mgr.list_checkpoints()[0]["id"])
    assert latest.metadata["status"] == "completed"


# ═══════════════════════════════════════════════════════════════
# 2. react：续跑提示带真实进度 + 预算窗口重置
# ═══════════════════════════════════════════════════════════════


def test_react_resume_prompt_lists_progress_and_resets_budget(cfg):
    mgr = CheckpointManager(cfg.execution.checkpoint_dir)
    cid = _save(mgr, AgentState(session_id="s_react", metadata=_react_metadata()))

    info = mgr.resumable_for_session("s_react")
    assert info is not None and info["kind"] == "react"
    assert info["progress"]["completed_actions"] == 2
    assert info["artifacts"] == ["auth.py"]

    agent = _new_agent(cfg)
    # 模拟"上一轮把预算烧光"：窗口用了 3 个、已花 1 亿 token
    agent._budget_windows_used = 3
    agent._budget_spent_prior = 99_999_999
    agent.resources.tokens.track_usage(3_000_000, 3_000_000)
    seen: dict = {}

    async def fake_run(prompt: str) -> AgentResult:
        seen["prompt"] = prompt
        seen["windows"] = agent._budget_windows_used
        seen["prior"] = agent._budget_spent_prior
        seen["used"] = agent.resources.tokens.tokens_used.total
        return AgentResult(success=True, output="续跑完成", steps_executed=1)

    agent.run = fake_run
    result = asyncio.run(agent.resume_from_checkpoint(cid))

    assert result.success is True
    prompt = seen["prompt"]
    assert "重构 auth 模块" in prompt
    # 提示里必须列出已完成动作与产物，并明确"不要重做"
    assert "不要重做" in prompt
    assert "file_write(path=auth.py)" in prompt and "pytest -q" in prompt
    assert "auth.py" in prompt
    # 预算窗口 / 已用量在真正执行前已归零（否则第一次调用就再撞预算）
    assert seen["windows"] == 0 and seen["prior"] == 0 and seen["used"] == 0
    assert mgr.load_sync(cid).metadata["status"] == "resumed"


def test_react_checkpoint_without_actions_is_still_resumable(tmp_path):
    """需求：react 即使 actions 为空、只要 metadata 有 task/partial 也可续跑。"""
    mgr = CheckpointManager(tmp_path)
    cid = _save(mgr, AgentState(session_id="s_empty", metadata={
        "task": "只做了一半的任务", "interaction": "work",
        "status": "interrupted", "react": {"actions": []}}))
    info = mgr.resumable_for_session("s_empty")
    assert info is not None and info["id"] == cid and info["kind"] == "react"
    assert info["progress"]["completed_actions"] == 0


# ═══════════════════════════════════════════════════════════════
# 3. multi / loop：按契约检查点续跑
# ═══════════════════════════════════════════════════════════════


@pytest.fixture()
def pro_features(monkeypatch):
    """把假特性直接塞进 edition 状态（与 pro/tests 的做法一致）。"""
    from automind.core import edition

    monkeypatch.setitem(edition._state, "edition", "pro")
    monkeypatch.setitem(edition._state, "loaded", True)
    return edition


def _install_multi(monkeypatch, edition, captured: list, agent) -> dict:
    state: dict = {}

    class _Orch:
        async def run(self, task, context="", on_event=None, resume=None):
            # 记录续跑真正开始执行那一刻的预算状态：必须是"新任务"的 0 起算，
            # 否则上一轮烧光的额度会让第一次模型调用就再次 BudgetExhausted。
            state["windows_at_run"] = agent._budget_windows_used
            state["tokens_at_run"] = agent.resources.tokens.tokens_used.total
            progress = dict(resume or {})
            done_roles = {str(s.get("role"))
                          for s in progress.get("steps") or [] if s.get("ok")}
            plan = [{"role": "planner", "subtask": "拆需求"},
                    {"role": "coder", "subtask": "写页面"},
                    {"role": "reviewer", "subtask": "复核"}]
            remaining = [p["role"] for p in plan if p["role"] not in done_roles]
            state["executed_roles"] = remaining
            if on_event is not None:
                await on_event({"type": "ma_plan", "plan": plan, "resumed": True})
                for i, role in enumerate(remaining):
                    await on_event({"type": "ma_step_end", "index": i,
                                    "role": role, "success": True})
            steps = [dict(s) for s in progress.get("steps") or []]
            steps += [{"role": r, "ok": True, "output": f"{r} 完成"} for r in remaining]
            return {"output": "协同续跑完成", "plan": plan, "steps": steps,
                    "scratch": progress.get("scratch", ""),
                    "last_output": "全部完成"}

    class _Feature:
        def __init__(self) -> None:
            self.orch = _Orch()

        def create(self, llm):
            return self.orch

    feature = _Feature()
    monkeypatch.setitem(edition._state["features"], "multi_agent", feature)
    captured.append(feature)
    return state


def _install_loop(monkeypatch, edition, captured: list, agent) -> dict:
    state: dict = {}

    class _Engine:
        async def run(self, agent_, task, on_event=None, max_iterations=None,
                      resume=None):
            state["windows_at_run"] = agent_._budget_windows_used
            state["tokens_at_run"] = agent_.resources.tokens.tokens_used.total
            progress = dict(resume or {})
            state["resume"] = progress
            state["max_iterations"] = max_iterations
            it = int(progress.get("iterations") or 0) + 1
            state["executed_iteration"] = it
            if on_event is not None:
                await on_event({"type": "loop_iter_start", "iter": it,
                                "max": max_iterations or 8, "resumed": True})
            return {"output": f"第 {it} 轮完成", "iterations": it,
                    "feedback": "", "success": True,
                    "max_iterations": max_iterations or 8}

    engine = _Engine()
    monkeypatch.setitem(edition._state["features"], "loop_engine", engine)
    captured.append(engine)
    return state


def _event_sink(events: list):
    async def sink(ev):
        events.append(dict(ev))
    return sink


def test_multi_resume_continues_remaining_roles(cfg, pro_features, monkeypatch):
    mgr = CheckpointManager(cfg.execution.checkpoint_dir)
    cid = _save(mgr, AgentState(session_id="s_multi", metadata=_multi_metadata()))

    info = mgr.resumable_for_session("s_multi")
    assert info is not None and info["kind"] == "multi"
    assert info["progress"]["steps_done"] == 2
    assert info["progress"]["steps_total"] == 3
    assert info["stop_reason"] == "budget_exhausted"

    captured: list = []
    agent = _new_agent(cfg)
    agent._interaction = InteractionMode.MULTI
    agent._mode = ExecutionMode.PLAN_AND_EXECUTE
    agent._budget_windows_used = 4              # 上一轮用掉的窗口
    agent.resources.tokens.track_usage(500_000, 500_000)
    state = _install_multi(monkeypatch, pro_features, captured, agent)
    events: list = []
    agent.event_sink = _event_sink(events)

    result = asyncio.run(agent.resume_from_checkpoint(cid))

    assert result.success is True
    # --- 关键：续跑把旧进度交给了编排器，且只补剩余 role ---
    orch = captured[0].orch
    assert state["executed_roles"] == ["reviewer"]
    assert result.steps_executed >= 1
    # 续跑开始时预算窗口/用量已从 0 起算（新任务一份预算）
    assert state["windows_at_run"] == 0
    assert state["tokens_at_run"] == 0
    # 源检查点 resumed，新检查点 completed，不再提示续跑
    assert mgr.load_sync(cid).metadata["status"] == "resumed"
    latest = mgr.load_sync(mgr.list_checkpoints()[0]["id"])
    assert latest.metadata["status"] == "completed"
    assert latest.metadata["run_mode"] == "multi"
    assert mgr.resumable_for_session("s_multi") is None
    # 事件：task_resumed(kind=multi) + 编排器的 ma_* 进度事件应送达前端
    assert any(e.get("type") == "task_resumed" and e.get("kind") == "multi"
               for e in events), events
    assert any(e.get("type") == "ma_plan" and e.get("resumed") for e in events), events
    assert any(e.get("type") == "ma_step_end" for e in events), events
    assert orch is not None


def test_loop_resume_continues_from_next_iteration(cfg, pro_features, monkeypatch):
    mgr = CheckpointManager(cfg.execution.checkpoint_dir)
    cid = _save(mgr, AgentState(session_id="s_loop", metadata=_loop_metadata()))

    info = mgr.resumable_for_session("s_loop")
    assert info is not None and info["kind"] == "loop"
    assert info["progress"]["iterations"] == 2

    captured: list = []
    agent = _new_agent(cfg)
    agent._interaction = InteractionMode.LOOP
    agent._mode = ExecutionMode.REACT
    agent._budget_windows_used = 2
    agent.resources.tokens.track_usage(600_000, 600_000)
    state = _install_loop(monkeypatch, pro_features, captured, agent)
    events: list = []
    agent.event_sink = _event_sink(events)

    result = asyncio.run(agent.resume_from_checkpoint(cid))

    assert result.success is True
    assert state["resume"]["iterations"] == 2, "续跑没有拿到已完成的迭代轮数"
    assert state["executed_iteration"] == 3, "没有从下一迭代继续"
    assert state["max_iterations"] == 8
    # 续跑开始时预算窗口/用量已从 0 起算
    assert state["windows_at_run"] == 0
    assert state["tokens_at_run"] == 0
    assert mgr.load_sync(cid).metadata["status"] == "resumed"
    latest = mgr.load_sync(mgr.list_checkpoints()[0]["id"])
    assert latest.metadata["status"] == "completed"
    assert latest.metadata["run_mode"] == "loop"
    assert any(e.get("type") == "task_resumed" and e.get("kind") == "loop"
               for e in events), events
    assert any(e.get("type") == "loop_iter_start" and e.get("resumed")
               for e in events), events


# ═══════════════════════════════════════════════════════════════
# 4. 发现层：四种 kind / 过滤 / 去重 / 旧检查点兼容
# ═══════════════════════════════════════════════════════════════


def test_resumable_for_session_supports_all_four_kinds(tmp_path):
    mgr = CheckpointManager(tmp_path)
    _save(mgr, _plan_state())
    _save(mgr, AgentState(session_id="s_react", metadata=_react_metadata()))
    _save(mgr, AgentState(session_id="s_multi", metadata=_multi_metadata()))
    _save(mgr, AgentState(session_id="s_loop", metadata=_loop_metadata()))

    kinds = {}
    for sid in ("s_plan", "s_react", "s_multi", "s_loop"):
        info = mgr.resumable_for_session(sid)
        assert info is not None, f"{sid} 没有被发现"
        kinds[sid] = info["kind"]
    assert kinds == {"s_plan": "plan", "s_react": "react",
                     "s_multi": "multi", "s_loop": "loop"}


def test_list_resumable_covers_four_kinds_newest_first_and_dedupes(tmp_path):
    mgr = CheckpointManager(tmp_path)
    ids = {}
    for sid, state in (
            ("s_plan", _plan_state()),
            ("s_react", AgentState(session_id="s_react", metadata=_react_metadata())),
            ("s_multi", AgentState(session_id="s_multi", metadata=_multi_metadata())),
            ("s_loop", AgentState(session_id="s_loop", metadata=_loop_metadata()))):
        ids[sid] = _save(mgr, state)
        time.sleep(0.01)

    # 同一会话更早的一份 partial：list_resumable 只能出现最新的那条
    older = _save(mgr, AgentState(session_id="s_multi", metadata={
        "task": "旧的多智能体任务", "interaction": "multi", "run_mode": "multi",
        "status": "partial", "progress": {"steps": [], "steps_done": 0,
                                          "steps_total": 0}}))
    rows = mgr.list_resumable()
    sids = [r["session_id"] for r in rows]
    assert set(sids) == {"s_plan", "s_react", "s_multi", "s_loop"}
    assert sids[0] == "s_multi", f"最新检查点应排最前：{sids}"
    assert [r["id"] for r in rows if r["session_id"] == "s_multi"] == [older]
    assert len({r["session_id"] for r in rows}) == len(rows)   # 每会话一条

    # 终态检查点不再出现（completed / dismissed / resumed）
    mgr.mark(ids["s_loop"], "dismissed")
    assert mgr.resumable_for_session("s_loop") is None
    mgr.mark(ids["s_react"], "completed")
    assert mgr.resumable_for_session("s_react") is None
    mgr.mark(ids["s_plan"], "resumed")
    assert mgr.resumable_for_session("s_plan") is None
    remaining = {r["session_id"] for r in mgr.list_resumable()}
    assert remaining == {"s_multi"}


def test_legacy_checkpoints_are_compatible(tmp_path):
    """旧版检查点（无 run_mode/progress）仍能发现；未知形态不误报。"""
    mgr = CheckpointManager(tmp_path)
    # 旧 plan：metadata 甚至没有 interaction
    cid_plan = _save(mgr, _plan_state())
    info = mgr.resumable_for_session("s_plan")
    assert info is not None and info["id"] == cid_plan and info["kind"] == "plan"

    # 旧 react：只有 react 动作
    cid_react = _save(mgr, AgentState(session_id="s_react", metadata={
        "task": "写个爬虫", "interaction": "coding", "status": "interrupted",
        "react": {"completed_actions": 1,
                  "actions": [{"tool": "file_write", "ok": True}],
                  "artifacts": ["crawl.py"]}}))
    info = mgr.resumable_for_session("s_react")
    assert info is not None and info["id"] == cid_react and info["kind"] == "react"
    assert info["done"] == 1 and info["artifacts"] == ["crawl.py"]   # 旧扁平字段仍在

    # 未知形态（无 react / 非 ReAct 交互 / 无计划）：宁可不出提示，也不要给错断点
    _save(mgr, AgentState(session_id="s_unknown", metadata={
        "task": "不知道是什么", "interaction": "chat", "status": "running"}))
    assert mgr.resumable_for_session("s_unknown") is None

    # 最老的检查点（无 interaction / 无 react）：task + partial 仍按 react 续跑
    cid_ancient = _save(mgr, AgentState(session_id="s_ancient", metadata={
        "task": "很老的任务", "status": "partial"}))
    info = mgr.resumable_for_session("s_ancient")
    assert info is not None and info["id"] == cid_ancient and info["kind"] == "react"

    # 老版本"最新一条是已完成任务"仍应吞掉更早的未完成检查点
    _save(mgr, AgentState(session_id="s_react", metadata={
        "task": "写个爬虫", "status": "completed"}))
    assert mgr.resumable_for_session("s_react") is None


# ═══════════════════════════════════════════════════════════════
# 5. HTTP API：resumable / dismiss
# ═══════════════════════════════════════════════════════════════


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """假 Agent（只挂 checkpoint_mgr）→ TestClient，隔离仓库数据。"""
    from fastapi.testclient import TestClient

    import automind.server as srv

    mgr = CheckpointManager(tmp_path / "api_ckpt")
    fake_agent = SimpleNamespace(checkpoint_mgr=mgr)
    monkeypatch.setattr(srv, "get_agent", lambda: fake_agent)
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    client = TestClient(srv.app, client=("127.0.0.1", 51000))
    yield client, mgr, fake_agent
    client.close()


def test_resumable_api_returns_kind_and_progress(api):
    client, mgr, _ = api
    cid = _save(mgr, AgentState(session_id="s_multi", metadata=_multi_metadata()))

    body = client.get("/api/checkpoints/resumable",
                      params={"session_id": "s_multi"}).json()
    info = body["resumable"]
    assert info["id"] == cid and info["kind"] == "multi"
    assert info["run_mode"] == "multi"
    assert info["stop_reason"] == "budget_exhausted"
    assert info["progress"] == {"steps_done": 2, "steps_total": 3,
                                "last_output": "前两步完成"}
    # 其它会话看自己的：没有断点 → None
    assert client.get("/api/checkpoints/resumable",
                      params={"session_id": "s_other"}).json()["resumable"] is None


def test_dismiss_api_clears_banner(api):
    client, mgr, _ = api
    cid = _save(mgr, AgentState(session_id="s_multi", metadata=_multi_metadata()))
    assert client.post(f"/api/checkpoints/{cid}/dismiss",
                       json={"session_id": "s_other"}).status_code == 404
    assert client.post(f"/api/checkpoints/{cid}/dismiss",
                       json={"session_id": "s_multi"}).status_code == 200
    assert client.get("/api/checkpoints/resumable",
                      params={"session_id": "s_multi"}).json()["resumable"] is None


def test_resume_target_validates_multi_loop_and_reports_reasons(api):
    import automind.server as srv

    _, mgr, _ = api
    cid = _save(mgr, AgentState(session_id="s_multi", metadata=_multi_metadata()))
    info, err = srv._resume_target({"resume_checkpoint": cid}, "s_multi")
    assert err == "" and info is not None and info["kind"] == "multi"
    info, err = srv._resume_target({"resume_checkpoint": cid}, "s_other")
    assert info is None and "不属于当前会话" in err

    mgr.mark(cid, "resumed")
    info, err = srv._resume_target({"resume_checkpoint": cid}, "s_multi")
    assert info is None and "已失效" in err


# ═══════════════════════════════════════════════════════════════
# 6. WS / REST 路由：multi/loop 带 resume_checkpoint 时走 resume
# ═══════════════════════════════════════════════════════════════


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(dict(data))


class _FakeRunAgent:
    """只实现 server 续跑路径需要的表面；run/run_multi/run_loop 一旦被调用即失败。"""

    def __init__(self, mgr: CheckpointManager, tmp_path: Path) -> None:
        self.checkpoint_mgr = mgr
        self.llm = SimpleNamespace(usage=TokenUsage())
        self.config = SimpleNamespace(
            llm=SimpleNamespace(provider="openai", model="fake", api_key="",
                                api_base="", max_tokens=1000, temperature=0.2),
            execution=SimpleNamespace(checkpoint_dir=str(mgr.checkpoint_dir),
                                      checkpoint_enabled=True,
                                      max_task_seconds=0, isolate_workspace=False),
            project_root=str(tmp_path / "proj"))
        (tmp_path / "proj").mkdir(exist_ok=True)
        self._interaction = InteractionMode.WORK
        self._mode = ExecutionMode.REACT
        self.session_id = ""
        self._run_session_id = ""
        self.approval_callback = None
        self.plan_review_cb = None
        self.prefer_tools = []
        self.event_sink = None
        self.calls: list[str] = []
        self.resume_ids: list[str] = []

    # 预算重置顺序记账（server 的 _resume_budget_reset 必须先于 resume）
    def _begin_task_budget(self) -> None:
        self.calls.append("budget")

    async def resume_from_checkpoint(self, cid: str) -> AgentResult:
        self.calls.append("resume")
        self.resume_ids.append(cid)
        return AgentResult(success=True, output="已续跑", steps_executed=1,
                           duration_ms=1.0, token_usage=TokenUsage())

    async def run(self, task):                       # pragma: no cover - 不应被调用
        self.calls.append("run")
        raise AssertionError("续跑路径不应该从头 run")

    async def run_multi(self, task, on_event=None, **kw):   # pragma: no cover
        self.calls.append("run_multi")
        raise AssertionError("续跑路径不应该重跑 multi")

    async def run_loop(self, task, on_event=None, **kw):    # pragma: no cover
        self.calls.append("run_loop")
        raise AssertionError("续跑路径不应该重跑 loop")

    def interjection_report(self) -> dict:
        return {}

    def drain_interjections(self) -> list:
        return []


@pytest.fixture()
def fake_server(monkeypatch, tmp_path):
    """把 server 的外部依赖替换成假实现，只保留续跑路由本身。"""
    import automind.server as srv

    mgr = CheckpointManager(tmp_path / "route_ckpt")
    agent = _FakeRunAgent(mgr, tmp_path)
    monkeypatch.setattr(srv, "get_agent", lambda: agent)
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_acquire_run_agent", lambda base, _sid: base)
    monkeypatch.setattr(srv, "_apply_mode_model", lambda a, _interaction: a)
    monkeypatch.setattr(srv, "_resolve_model_override",
                        lambda a, _data, _raw, _images, _receipt: (a, True))
    monkeypatch.setattr(srv, "_apply_router",
                        lambda a, _raw, _images=None: a)
    monkeypatch.setattr(srv._quota, "try_consume_task", lambda: (True, ""))
    monkeypatch.setattr(srv, "_record_and_notify", lambda _sid, _ev: None)
    monkeypatch.setattr(srv, "_push_history", lambda _record: None)
    monkeypatch.setattr(srv, "_accumulate_tokens", lambda _record: None)
    monkeypatch.setattr(srv, "_goal_finish", lambda _ctx, _sid, _task: None)
    events: list = []

    async def _broadcast(data):
        events.append(dict(data))

    monkeypatch.setattr(srv, "_broadcast", _broadcast)
    monkeypatch.setattr(srv, "_running_tasks", {"count": 0})
    monkeypatch.setattr(srv, "_live_runs", {})
    monkeypatch.setattr(srv, "_active_sessions", {})
    monkeypatch.setattr(srv, "_interject_refs", {})
    return agent, mgr, events


def _ws_resume_data(cid: str, run_mode: str) -> dict:
    return {"task": "原任务", "session_id": "s_me", "interaction": run_mode,
            "resume_checkpoint": cid}


@pytest.mark.parametrize("run_mode", ["multi", "loop"])
def test_ws_run_resume_dispatches_to_resume_not_run_mode(
        fake_server, run_mode):
    import automind.server as srv

    agent, mgr, _ = fake_server
    meta = _multi_metadata() if run_mode == "multi" else _loop_metadata()
    cid = _save(mgr, AgentState(session_id="s_me", metadata=meta))
    ws = _FakeWS()

    asyncio.run(srv._ws_run(ws, "cid", _ws_resume_data(cid, run_mode)))

    assert agent.resume_ids == [cid], "带 resume_checkpoint 却没有走 resume"
    assert "run" not in agent.calls and "run_multi" not in agent.calls \
        and "run_loop" not in agent.calls
    # 预算重置发生在 resume 之前（先重置再续跑）
    assert agent.calls.index("budget") < agent.calls.index("resume")
    # task_start 带上 resume 摘要；task_complete 带 resumed_from
    start = next(m for m in ws.sent if m.get("type") == "task_start")
    assert start["resume"]["kind"] == run_mode
    assert start["resume"]["id"] == cid
    assert start["resume"]["progress"]
    done = next(m for m in ws.sent if m.get("type") == "task_complete")
    assert done["resumed_from"] == cid and done["resume_kind"] == run_mode


@pytest.mark.parametrize("run_mode", ["multi", "loop"])
def test_rest_run_resume_dispatches_to_resume_not_run_mode(
        fake_server, run_mode):
    import automind.server as srv

    agent, mgr, _ = fake_server
    meta = _multi_metadata() if run_mode == "multi" else _loop_metadata()
    cid = _save(mgr, AgentState(session_id="s_me", metadata=meta))

    record = asyncio.run(srv.api_run(_ws_resume_data(cid, run_mode)))

    assert agent.resume_ids == [cid]
    assert "run" not in agent.calls and "run_multi" not in agent.calls \
        and "run_loop" not in agent.calls
    assert agent.calls.index("budget") < agent.calls.index("resume")
    assert record["resumed_from"] == cid
    assert record["resume_kind"] == run_mode
    assert record["interaction"] == run_mode
    assert record["success"] is True


def test_rest_run_rejects_invalid_resume_checkpoint(fake_server):
    import automind.server as srv

    agent, mgr, _ = fake_server
    cid = _save(mgr, AgentState(session_id="s_other", metadata=_multi_metadata()))
    resp = asyncio.run(srv.api_run({"task": "x", "session_id": "s_me",
                                    "resume_checkpoint": cid}))
    assert resp.status_code == 409
    assert agent.resume_ids == []


# ═══════════════════════════════════════════════════════════════
# 7. 预算重置兜底 + 前后端接线契约（源码级，防字段漂移）
# ═══════════════════════════════════════════════════════════════


def test_resume_budget_reset_falls_back_for_old_agent():
    """老版本 Agent 没有 _begin_task_budget 时，服务端回退到旧三步。"""
    import automind.server as srv

    class _Counter:
        def __init__(self) -> None:
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

    class _OldAgent:
        def __init__(self) -> None:
            self.resources = SimpleNamespace(tokens=_Counter())
            self.window_resets = 0
            self.usage_resets = 0

        def _reset_budget_windows(self) -> None:
            self.window_resets += 1

        def _reset_usage_total(self) -> None:
            self.usage_resets += 1

    agent = _OldAgent()
    srv._resume_budget_reset(agent)
    assert agent.window_resets == 1
    assert agent.usage_resets == 1
    assert agent.resources.tokens.reset_calls == 1


def test_frontend_and_server_wiring_contract():
    """把"四种 kind / 续跑入口 / 预算耗尽提示"的接线钉在源码上。

    这几处分散在 web 的 3 个文件与 server 的 2 个处理器里，漏掉任一处的表现
    都是"功能看起来做了，用户实际点不到/看不懂"—— 用源码断言防漂移
    （与 tests/server/test_approval_flow.py 的前后端契约测试同思路）。
    """
    root = Path(__file__).resolve().parent.parent
    web = root / "web" / "src"
    overrides = (web / "lib" / "overrides.ts").read_text(encoding="utf-8")
    # 四种 kind 的进度字段与文案都在（旧版只有 plan/react）
    for token in ("'plan' | 'react' | 'multi' | 'loop'", "steps_done",
                  "steps_total", "iterations", "RESUMABLE_ICON", "budget_exhausted"):
        assert token in overrides, f"overrides.ts 缺少 {token}"
    assert "case 'multi'" in overrides and "case 'loop'" in overrides

    panel = (web / "components" / "chat" / "ChatPanel.tsx").read_text(encoding="utf-8")
    assert "RESUMABLE_ICON" in panel and "describeResumable(resumable)" in panel
    assert "refreshTick" in panel, "任务结束后没有对账 resumable（预算耗尽后横幅可能不出现）"
    assert "从断点续跑" in panel

    ws = (web / "ws.ts").read_text(encoding="utf-8")
    assert "case 'run_interrupted'" in ws, "multi/loop 的「已保存断点」事件没人处理"
    assert "case 'budget_exhausted'" in ws, "multi/loop 预算耗尽的收尾事件没人处理"
    assert "resumed_from" in ws and "已保存断点" in ws
    assert "从断点续跑" in ws  # budget_exceeded 文案指向真正的续跑入口

    srv_src = (root / "automind" / "server.py").read_text(encoding="utf-8")
    assert "_resume_budget_reset" in srv_src
    assert "resume_info is None" in srv_src      # multi/loop 直跑分支在续跑时跳过
    assert "resumed_from" in srv_src and "resume_kind" in srv_src
    cp_src = (root / "automind" / "state" / "checkpoint.py").read_text(encoding="utf-8")
    for kind in ('"plan"', '"react"', '"multi"', '"loop"'):
        assert kind in cp_src

