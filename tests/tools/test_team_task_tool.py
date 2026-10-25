"""``team_task`` —— 团队共享任务看板的 AI 工具。

## 这个文件盯住什么

1. **全链路可用**：create → list → claim → update(done) → comment → summary，
   每一步都返回"还能接着操作"的回执（id / 当前状态 / 下一步提示）；
2. **参数错误可执行**：非法 status/priority/due、空 title、空评论、未知 task_id
   都返回 ``success=False`` + 能照着改的文案（未知 id 还要把现有 id 列出来）；
3. **actor 一路兜底**：没有会话上下文 / 没有昵称时不能让工具失败；
4. **广播是旁路**：有 ``automind.server`` 时把变更推成 ``team_activity``，
   server 模块不存在或广播抛错都**不得**影响写操作。

存储全部注入内存 ``TeamStore``（loader/saver 用 list）：这些用例不碰 SQLite、
不联网、也不依赖 Web 端。
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from automind.core import session_ctx, team, team_presence
from automind.core.types import PermissionTier
from automind.tools.team_tasks import ACTIONS, TeamTaskTool

# ═══════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def memory_store() -> team.TeamStore:
    """内存 TeamStore：与生产同一套领域逻辑，只是读写落在 list 上。"""
    items: list[dict] = []

    def loader() -> list[dict]:
        return [dict(t) for t in items]

    def saver(tasks: list[dict]) -> None:
        items.clear()
        items.extend(dict(t) for t in tasks)

    return team.TeamStore(loader=loader, saver=saver)


@pytest.fixture(autouse=True)
def broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """默认注入一个假的 ``automind.server``，记录广播调用。

    autouse 的原因：本仓库其它用例可能已经 import 过真实的 ``automind.server``，
    若这里不隔离，``team_task`` 的广播就会真的走 WebSocket 实现 —— 单测随之
    依赖服务端内部结构，失败现场也不再是本用例的。需要"没有 server"的用例
    在用例体内用 monkeypatch 删掉即可（LIFO 覆盖本夹具）。
    """
    events: list[dict] = []
    module = ModuleType("automind.server")

    async def _broadcast_team_activity(kind: str, *, task: dict | None = None,
                                       sid: str = "", actor: str = "",
                                       detail: str = "", **extra: Any) -> None:
        events.append({"kind": kind, "task": task, "sid": sid,
                       "actor": actor, "detail": detail, **extra})

    module._broadcast_team_activity = _broadcast_team_activity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automind.server", module)
    return events


def _tool(store: team.TeamStore) -> TeamTaskTool:
    return TeamTaskTool(store=store)


def _first_task(store: team.TeamStore) -> dict:
    tasks = store.load()
    assert tasks, "夹具/被测代码没有写入任务"
    return tasks[0]


# ═══════════════════════════════════════════════════════════
# 1. 契约
# ═══════════════════════════════════════════════════════════


def test_tool_contract():
    assert TeamTaskTool.name == "team_task"
    assert TeamTaskTool.permission_tier is PermissionTier.SENSITIVE
    assert TeamTaskTool.risk_score == 40

    schema = TeamTaskTool.parameters
    props = schema["properties"]
    assert schema["required"] == ["action"]
    assert props["action"]["enum"] == list(ACTIONS)
    for key in ("task_id", "title", "desc", "assignee", "due", "text",
                "member", "q", "limit"):
        assert key in props, f"模型需要 {key} 参数才能完成对应动作"
    assert props["status"]["enum"] == list(team.STATUSES)
    assert props["priority"]["enum"] == list(team.PRIORITIES)
    assert "list" in TeamTaskTool.description
    assert "claim" in TeamTaskTool.description


def test_default_construction_does_not_touch_the_database():
    """注册工具时 Agent 不该因为数据目录/SQLite 问题而失败。"""
    tool = TeamTaskTool()
    assert tool.store is None


# ═══════════════════════════════════════════════════════════
# 2. 全链路
# ═══════════════════════════════════════════════════════════


async def test_create_list_claim_update_comment_summary_lifecycle(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)

    # ── create ──
    created = await tool.execute(
        action="create", title="补齐登录页错误提示",
        desc="空密码/错误密码都要有可读提示", priority="high",
        due="2026-10-31", tags="frontend,ui")
    assert created.success is True, created.error
    task = created.output["task"]
    task_id = task["id"]
    assert task_id
    assert task["status"] == "todo"
    assert task["priority"] == "high"
    assert task["due"] == "2026-10-31"
    assert task["tags"] == ["frontend", "ui"]
    assert task["comments"] == 0
    assert created.output["actor"] == "AutoMind", "没有会话上下文时兜底 AutoMind"
    assert "update" in created.output["note"] and "comment" in created.output["note"]
    assert broadcasts[-1]["kind"] == "task_created"
    assert broadcasts[-1]["task"]["id"] == task_id

    # ── list ──
    listed = await tool.execute(action="list")
    assert listed.success is True
    assert listed.output["count"] == 1
    assert listed.output["tasks"][0]["id"] == task_id
    assert listed.output["summary"]["total"] == 1
    assert listed.output["summary"]["by_status"]["todo"] == 1
    assert "claim" in listed.output["note"]

    # ── claim ──
    claimed = await tool.execute(action="claim", task_id=task_id, member="小明")
    assert claimed.success is True, claimed.error
    assert claimed.output["task"]["assignee"] == "小明"
    assert claimed.output["task"]["status"] == "doing"
    assert broadcasts[-1]["kind"] == "task_claimed"
    assert broadcasts[-1]["actor"] == "AutoMind", "广播署名人应是操作人，不是被认领人"

    # ── update → done ──
    done = await tool.execute(action="update", task_id=task_id, status="done")
    assert done.success is True, done.error
    assert done.output["task"]["status"] == "done"
    assert "comment" in done.output["note"], "完成时提示先留结果摘要"
    assert broadcasts[-1]["kind"] == "task_moved", "状态变化用 task_moved，前端看板才知道挪列"
    assert "doing" in broadcasts[-1]["detail"] and "done" in broadcasts[-1]["detail"]

    # ── comment ──
    commented = await tool.execute(
        action="comment", task_id=task_id, text="已修复并自测通过")
    assert commented.success is True, commented.error
    assert commented.output["task"]["comments"] == 1
    assert commented.output["comment"]["text"] == "已修复并自测通过"
    assert broadcasts[-1]["kind"] == "task_commented"
    stored = _first_task(memory_store)
    assert stored["comments"][0]["text"] == "已修复并自测通过"

    # ── summary ──
    summary = await tool.execute(action="summary")
    assert summary.success is True
    assert summary.output["total"] == 1
    assert summary.output["by_status"]["done"] == 1
    assert summary.output["completion_rate"] == 100.0
    assert summary.output["by_assignee"][0]["assignee"] == "小明"


async def test_list_filters_by_keyword_and_status(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    first = await tool.execute(action="create", title="修复登录页错误提示")
    await tool.execute(action="create", title="优化首屏性能", tags="perf")
    await tool.execute(action="update", task_id=first.output["task"]["id"],
                       status="doing")

    by_keyword = await tool.execute(action="list", q="登录")
    assert by_keyword.output["count"] == 1
    assert by_keyword.output["tasks"][0]["title"] == "修复登录页错误提示"

    by_status = await tool.execute(action="list", filter_status="doing")
    assert by_status.output["count"] == 1
    assert by_status.output["tasks"][0]["status"] == "doing"

    doing_with_keyword = await tool.execute(action="list", q="性能",
                                            filter_status="doing")
    assert doing_with_keyword.output["count"] == 0
    assert "没有匹配的任务" in doing_with_keyword.output["note"]


async def test_list_limit_truncates_with_an_explicit_note(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    for index in range(3):
        await tool.execute(action="create", title=f"任务 {index}")

    listed = await tool.execute(action="list", limit=2)

    assert listed.output["count"] == 3
    assert listed.output["returned"] == 2
    assert len(listed.output["tasks"]) == 2
    assert "仅显示前 2 条" in listed.output["note"]


async def test_update_can_clear_due_date_and_keep_other_fields(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    created = await tool.execute(action="create", title="有截止日期",
                                 due="2026-10-31", assignee="小红")
    task_id = created.output["task"]["id"]

    cleared = await tool.execute(action="update", task_id=task_id, due="")

    assert cleared.success is True
    assert cleared.output["task"]["due"] == ""
    assert cleared.output["task"]["assignee"] == "小红", "只改 due 不能顺手清掉负责人"


# ═══════════════════════════════════════════════════════════
# 3. actor 解析（昵称缺失是常态，不能失败）
# ═══════════════════════════════════════════════════════════


async def test_actor_falls_back_through_all_levels(
        memory_store: team.TeamStore, broadcasts: list[dict],
        monkeypatch: pytest.MonkeyPatch):
    tool = _tool(memory_store)

    # 1) 没有会话上下文 → AutoMind
    no_session = await tool.execute(action="create", title="无会话任务")
    assert no_session.output["actor"] == "AutoMind"

    # 2) 有会话但没有昵称 → 会话 <sid>
    with session_ctx.bind_session("sess-abc"):
        raw_session = await tool.execute(action="create", title="只有 sid")
    assert raw_session.output["actor"] == "会话 sess-abc"

    # 3) 持久化昵称优先（.automind/team_members.json）
    monkeypatch.setattr(team, "load_member_names",
                        lambda: {"sess-abc": "小明"})
    with session_ctx.bind_session("sess-abc"):
        persisted = await tool.execute(action="create", title="持久化昵称")
    assert persisted.output["actor"] == "小明"

    # 4) 没有持久化昵称时用在线昵称
    monkeypatch.setattr(team, "load_member_names", lambda: {})
    monkeypatch.setattr(team_presence, "list_members",
                        lambda: [{"sid": "sess-abc", "name": "在线甲"}])
    with session_ctx.bind_session("sess-abc"):
        presence = await tool.execute(action="create", title="在线昵称")
    assert presence.output["actor"] == "在线甲"


async def test_actor_is_used_as_creator_and_default_claim_member(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    with session_ctx.bind_session("sess-owner"):
        created = await tool.execute(action="create", title="我的任务")
        task_id = created.output["task"]["id"]
        claim_target = await tool.execute(action="create", title="待认领任务")
        claim_target_id = claim_target.output["task"]["id"]

        claimed = await tool.execute(action="claim", task_id=claim_target_id)

    assert _first_task(memory_store)["creator"] == "会话 sess-owner"
    assert claimed.output["task"]["assignee"] == "会话 sess-owner", \
        "claim 缺省 member 应该是当前 actor"
    assert claimed.output["task"]["id"] == claim_target_id
    assert task_id != claim_target_id


# ═══════════════════════════════════════════════════════════
# 4. 参数/任务校验：失败必须是可执行的
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("kwargs, marker", [
    ({"action": "bogus"}, "未知的 action"),
    ({"action": "create"}, "title 不能为空"),
    ({"action": "create", "title": "x", "status": "whatever"}, "status 必须是"),
    ({"action": "create", "title": "x", "priority": "p0"}, "priority 必须是"),
    ({"action": "create", "title": "x", "due": "10/31/2026"}, "due 无法识别"),
    ({"action": "list", "filter_status": "later"}, "filter_status 必须是"),
    ({"action": "list", "limit": 0}, "limit 必须大于 0"),
    ({"action": "list", "limit": "abc"}, "limit 必须是"),
    ({"action": "update", "status": "done"}, "task_id 不能为空"),
    ({"action": "update", "task_id": "t1"}, "没有可更新的字段"),
    ({"action": "claim"}, "task_id 不能为空"),
    ({"action": "comment", "task_id": "t1", "text": "   "}, "text 不能为空"),
    ({"action": "comment", "text": "有内容但没 id"}, "task_id 不能为空"),
])
async def test_invalid_params_are_actionable(
        memory_store: team.TeamStore, broadcasts: list[dict],
        kwargs: dict, marker: str):
    result = await _tool(memory_store).execute(**kwargs)

    assert result.success is False
    assert marker in (result.error or ""), result.error
    assert memory_store.load() == [], "参数校验失败不能写盘"


async def test_unknown_task_id_lists_existing_ids(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    created = await tool.execute(action="create", title="已存在的任务")
    task_id = created.output["task"]["id"]

    for kwargs in (
        {"action": "update", "task_id": "nope", "status": "done"},
        {"action": "claim", "task_id": "nope"},
        {"action": "comment", "task_id": "nope", "text": "评论"},
    ):
        result = await tool.execute(**kwargs)
        assert result.success is False
        message = result.error or ""
        assert "不存在" in message and "nope" in message
        assert task_id in message, "未知 id 的错误里要列出可用 id，模型才能一次改对"
        assert "action='list'" in message


async def test_invalid_status_on_existing_task_does_not_mutate_it(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    created = await tool.execute(action="create", title="别被非法状态改坏")
    task_id = created.output["task"]["id"]

    result = await tool.execute(action="update", task_id=task_id,
                                status="finished")

    assert result.success is False
    assert "status 必须是" in (result.error or "")
    assert _first_task(memory_store)["status"] == "todo"


# ═══════════════════════════════════════════════════════════
# 5. 广播：有则发、没有/失败都不影响工具
# ═══════════════════════════════════════════════════════════


async def test_broadcast_is_skipped_when_server_module_is_absent(
        memory_store: team.TeamStore, monkeypatch: pytest.MonkeyPatch):
    # 覆盖 autouse 夹具：模拟 CLI / 单测环境里没有 automind.server
    monkeypatch.delitem(sys.modules, "automind.server", raising=False)
    tool = _tool(memory_store)

    created = await tool.execute(action="create", title="CLI 环境建任务")

    assert created.success is True
    assert len(memory_store.load()) == 1


async def test_broadcast_failure_never_breaks_the_write(
        memory_store: team.TeamStore, monkeypatch: pytest.MonkeyPatch):
    broken = ModuleType("automind.server")

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("WebSocket 已断开")

    broken._broadcast_team_activity = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "automind.server", broken)
    tool = _tool(memory_store)

    created = await tool.execute(action="create", title="广播炸了也要成功")

    assert created.success is True
    assert len(memory_store.load()) == 1


async def test_broadcast_carries_actor_and_session(
        memory_store: team.TeamStore, broadcasts: list[dict]):
    tool = _tool(memory_store)
    with session_ctx.bind_session("sess-broadcast"):
        created = await tool.execute(action="create", title="带会话广播")

    event = broadcasts[-1]
    assert event["kind"] == "task_created"
    assert event["sid"] == "sess-broadcast"
    assert event["actor"] == "会话 sess-broadcast"
    assert event["task"]["id"] == created.output["task"]["id"]


# ═══════════════════════════════════════════════════════════
# 6. 默认存储：CLI / 测试环境下走 SQLite 也要真的能用
# ═══════════════════════════════════════════════════════════


async def test_default_sqlite_store_works_in_a_cli_like_environment(
        tmp_path, monkeypatch: pytest.MonkeyPatch):
    """没有 server 模块、数据目录是全新的 —— 默认 TeamStore() 必须能读写。

    这是 CLI / 首次启动的真实形态：没有 WebSocket 广播、数据目录刚建。
    如果这里失败，Agent 一调 team_task 就是"存储不可用"，"AI 参与团队协作"
    在非 Web 场景下等于不存在。用临时 SQLite 文件，不碰真实数据目录。
    """
    from automind.core import db as db_mod

    monkeypatch.delitem(sys.modules, "automind.server", raising=False)
    monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "data"))
    db_mod.reset_for_tests(tmp_path / "data" / "automind.db")
    try:
        tool = TeamTaskTool()          # store=None → 默认 SQLite
        assert tool.store is None

        created = await tool.execute(action="create", title="CLI 里的团队任务")
        assert created.success is True, created.error
        assert created.output["actor"] == "AutoMind"

        listed = await tool.execute(action="list")
        assert listed.success is True
        assert listed.output["count"] == 1
        assert listed.output["tasks"][0]["title"] == "CLI 里的团队任务"
    finally:
        db_mod.reset_for_tests(None)   # 关连接并清掉单例，避免污染其它用例


# ═══════════════════════════════════════════════════════════
# 7. 注册（Lead 在 agent.py 完成接线；这里验证接线确实生效）
# ═══════════════════════════════════════════════════════════


def test_agent_registers_the_team_task_tool(tmp_path):
    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig

    agent = AutoMindAgent(AgentConfig(project_root=str(tmp_path)))

    assert "team_task" in agent.tool_registry
    assert agent.tool_registry.get("team_task").name == "team_task"
