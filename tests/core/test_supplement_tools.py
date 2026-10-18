"""v1.11.11 补充工具：待办 / 向用户提问 / 交付声明 / 原生 pwsh。

这些工具的共同点是**只增加能力、不改变既有执行逻辑**，因此测试重点放在：
1. 输入校验与失败话术（不能让模型"看起来成功"）；
2. 会话隔离（工具注册表跨会话克隆共享，状态绝不能串）；
3. 事件契约（右栏/聊天卡片依赖它，字段名错一个界面就静默空白）；
4. 没有通道/没有权限时 fail-closed（提问不是批准、交付不是伪造）。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from automind.core import session_ctx, todo_store, user_questions
from automind.core.types import PermissionTier
from automind.tools.ask_user import AskUserTool
from automind.tools.present import PresentTool
from automind.tools.todo import TodoWriteTool


class _FakeDB:
    """最小 kv 存储替身：不建真实 SQLite，测试之间互不污染。"""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def kv_get(self, key: str, default=None):
        return self.data.get(key, default)

    def kv_set(self, key: str, value) -> None:
        self.data[key] = value


@pytest.fixture()
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(todo_store, "_db", lambda: db)
    return db


def _events():
    bucket: list[dict] = []

    async def sink(ev: dict) -> None:
        bucket.append(ev)

    return bucket, sink


# ═══════════════════════════════════════════════════════════
# 待办
# ═══════════════════════════════════════════════════════════


class TestTodoStore:
    def test_normalize_accepts_common_aliases(self):
        out = todo_store.normalize([
            {"content": "读配置", "status": "doing"},
            {"content": "改代码", "status": "done"},
            {"content": "跑测试", "status": "pending"},
        ])
        assert [x["status"] for x in out] == [
            "in_progress", "completed", "pending"]

    def test_normalize_allows_empty_list_for_clear(self):
        assert todo_store.normalize([]) == []

    def test_normalize_rejects_bad_status(self):
        with pytest.raises(ValueError, match="status"):
            todo_store.normalize([{"content": "x", "status": "whatever"}])
        with pytest.raises(ValueError):
            todo_store.normalize([{"status": "pending"}])

    def test_replace_load_counts_are_session_scoped(self, fake_db):
        todo_store.replace("s1", [{"content": "a", "status": "pending"}])
        todo_store.replace("s2", [{"content": "b", "status": "completed"}])
        assert [t["content"] for t in todo_store.load("s1")] == ["a"]
        assert [t["content"] for t in todo_store.load("s2")] == ["b"]
        assert todo_store.counts(todo_store.load("s1")) == {
            "pending": 1, "in_progress": 0, "completed": 0}
        todo_store.clear("s1")
        assert todo_store.load("s1") == []


class TestTodoTool:
    async def test_emits_event_and_returns_counts(self, fake_db):
        bucket, sink = _events()
        with session_ctx.bind_session("conv-1", sink=sink):
            result = await TodoWriteTool().execute(todos=[
                {"content": "第一步", "status": "in_progress"}])
        assert result.success
        assert result.output["counts"]["in_progress"] == 1
        assert bucket and bucket[0]["type"] == "todo_updated"
        assert bucket[0]["session_id"] == "conv-1"
        assert bucket[0]["todos"][0]["content"] == "第一步"

    async def test_invalid_input_fails_without_writing(self, fake_db):
        with session_ctx.bind_session("conv-1"):
            result = await TodoWriteTool().execute(todos=[{"content": "x",
                                                           "status": "bad"}])
        assert not result.success and "status" in (result.error or "")
        assert todo_store.load("conv-1") == []

    async def test_session_isolation(self, fake_db):
        with session_ctx.bind_session("A"):
            await TodoWriteTool().execute(todos=[{"content": "A 的任务",
                                                  "status": "pending"}])
        with session_ctx.bind_session("B"):
            await TodoWriteTool().execute(todos=[{"content": "B 的任务",
                                                  "status": "pending"}])
        assert [t["content"] for t in todo_store.load("A")] == ["A 的任务"]
        assert [t["content"] for t in todo_store.load("B")] == ["B 的任务"]


# ═══════════════════════════════════════════════════════════
# 向用户提问
# ═══════════════════════════════════════════════════════════


class TestUserQuestionsRegistry:
    async def test_resolve_returns_normalized_answers(self):
        qid, fut = user_questions.create("s1", [
            {"id": "q1", "question": "选哪个？",
             "options": [{"label": "A"}, {"label": "B"}]}])
        rec = user_questions.resolve(qid, [{"id": "q1", "selected": ["A"]}])
        assert rec is not None and rec["answers"][0]["selected"] == ["A"]
        assert (await fut)["answers"][0]["selected"] == ["A"]
        assert user_questions.resolve(qid, [{"id": "q1", "selected": ["A"]}]) is None

    async def test_invalid_answers_keep_question_pending(self):
        qid, fut = user_questions.create("s1", [{"id": "q1", "question": "x"}])
        assert user_questions.resolve(qid, [{"id": "nope", "selected": []}]) is None
        assert not fut.done(), "结构非法应让用户重新回答，而不是结束问题"
        rec = user_questions.resolve(qid, [{"id": "q1", "selected": []}])
        assert rec is not None and fut.done()

    async def test_cancel_wakes_waiter(self):
        qid, fut = user_questions.create("s1", [{"id": "q1", "question": "x"}])
        assert user_questions.cancel(qid, "timeout") is not None
        assert (await fut)["cancelled"] is True

    async def test_cancel_session_only_touches_that_session(self):
        q1, f1 = user_questions.create("s1", [{"id": "q1", "question": "x"}])
        q2, f2 = user_questions.create("s2", [{"id": "q1", "question": "x"}])
        assert user_questions.cancel_session("s1", "closed") == 1
        assert (await f1)["cancelled"] is True
        assert not f2.done()
        user_questions.cancel(q2, "done")


class TestAskUserTool:
    async def test_answers_roundtrip_through_event_channel(self):
        bucket, sink = _events()
        tool = AskUserTool()
        with session_ctx.bind_session("s1", sink=sink):
            task = asyncio.create_task(tool.execute(questions=[
                {"id": "fmt", "question": "输出什么格式？",
                 "options": [{"label": "Markdown（推荐）"}, {"label": "PDF"}]}],
                timeout=30))
            await asyncio.sleep(0)                 # 让工具先登记并发事件
            for _ in range(50):
                if bucket:
                    break
                await asyncio.sleep(0.01)
            qid = bucket[0]["question_id"]
            assert user_questions.resolve(qid, [
                {"id": "fmt", "selected": ["Markdown（推荐）"]}]) is not None
            result = await task
        assert result.success
        assert result.output["answers"][0]["selected"] == ["Markdown（推荐）"]

    async def test_no_event_channel_fails_fast(self):
        tool = AskUserTool()
        with session_ctx.bind_session("s1"):        # 没有 sink
            result = await tool.execute(questions=[{"id": "q", "question": "x"}],
                                        timeout=5)
        assert not result.success and "通道" in (result.error or "")

    async def test_subagent_cannot_ask_user(self):
        tool = AskUserTool()
        with session_ctx.bind_session("s1", sink=_events()[1]), \
                session_ctx.bind_agent(object(), depth=1):
            result = await tool.execute(questions=[{"id": "q", "question": "x"}])
        assert not result.success and "子智能体" in (result.error or "")

    async def test_timeout_is_failure_not_approval(self):
        tool = AskUserTool()
        _, sink = _events()
        with session_ctx.bind_session("s1", sink=sink):
            result = await tool.execute(
                questions=[{"id": "q", "question": "x"}], timeout=0.05)
        assert not result.success
        assert result.output.get("timed_out") is True
        assert "超时" in (result.error or "") or "没有回答" in (result.error or "")


# ═══════════════════════════════════════════════════════════
# 交付声明
# ═══════════════════════════════════════════════════════════


class TestPresentTool:
    async def test_presents_existing_file_and_emits_event(self, tmp_path):
        f = tmp_path / "报告.md"
        f.write_text("hi", encoding="utf-8")
        tool = PresentTool(project_root=tmp_path)
        bucket, sink = _events()
        with session_ctx.bind_session("s1", sink=sink):
            result = await tool.execute(files=[{"path": "报告.md",
                                                "description": "最终报告"}])
        assert result.success and result.output["count"] == 1
        assert bucket and bucket[0]["type"] == "presented_files"
        assert bucket[0]["files"][0]["path"] == str(f.resolve())
        assert bucket[0]["files"][0]["description"] == "最终报告"

    async def test_missing_and_directory_rejected(self, tmp_path):
        (tmp_path / "sub").mkdir()
        tool = PresentTool(project_root=tmp_path)
        with session_ctx.bind_session("s1"):
            missing = await tool.execute(files=[{"path": "nope.txt"}])
            directory = await tool.execute(files=[{"path": "sub"}])
        assert not missing.success and not directory.success

    async def test_outside_project_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        tool = PresentTool(project_root=tmp_path)
        with session_ctx.bind_session("s1"):
            result = await tool.execute(files=[{"path": str(outside)}])
        assert not result.success and "范围" in (result.error or "")

    async def test_too_many_files_rejected(self, tmp_path):
        tool = PresentTool(project_root=tmp_path)
        with session_ctx.bind_session("s1"):
            result = await tool.execute(
                files=[{"path": f"f{i}.txt"} for i in range(9)])
        assert not result.success


# ═══════════════════════════════════════════════════════════
# 原生 pwsh（仅 Windows）
# ═══════════════════════════════════════════════════════════


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows")
class TestPwshTool:
    async def test_runs_native_powershell(self):
        from automind.tools.pwsh import PwshTool
        if not PwshTool.available():
            pytest.skip("本机没有 pwsh/powershell")
        tool = PwshTool(workdir=".", timeout=60)
        assert tool.permission_tier is PermissionTier.SENSITIVE
        result = await tool.execute(command='Write-Output "hello-from-pwsh"')
        assert result.success, result.error
        assert "hello-from-pwsh" in str(result.output.get("stdout", ""))

    async def test_missing_command_is_reported(self):
        from automind.tools.pwsh import PwshTool
        if not PwshTool.available():
            pytest.skip("本机没有 pwsh/powershell")
        result = await PwshTool(workdir=".", timeout=60).execute(command="")
        assert not result.success


class TestToolRegistration:
    """注册是"只增不改"的入口：单个加载器失败不能连累其余工具。"""

    @staticmethod
    def _agent(tmp_path):
        from automind.agent import AutoMindAgent
        from automind.tools.base import ToolRegistry

        agent = object.__new__(AutoMindAgent)
        agent.tool_registry = ToolRegistry()
        agent.config = SimpleNamespace(
            project_root=str(tmp_path),
            execution=SimpleNamespace(tool_timeout_seconds=300.0,
                                      tool_timeout_max_seconds=1800.0),
        )
        agent._record_tool_group_failure = lambda *_a, **_k: None
        return agent

    def test_community_tools_are_registered(self, tmp_path):
        agent = self._agent(tmp_path)
        agent._register_supplement_tools()
        names = set(agent.tool_registry.list_names())
        assert {"todo_write", "ask_user_question", "present"} <= names

    def test_subagent_tools_absent_without_pro_license(self, tmp_path, monkeypatch):
        from automind.core import edition

        monkeypatch.setattr(edition, "has_feature", lambda _name: False)
        agent = self._agent(tmp_path)
        agent._register_supplement_tools()
        assert "subagent" not in set(agent.tool_registry.list_names())

    def test_one_failing_loader_does_not_block_others(self, tmp_path):
        agent = self._agent(tmp_path)

        def boom() -> None:
            raise RuntimeError("插件坏了")

        agent._register_todo_tool = boom        # 第一个失败
        agent._register_supplement_tools()
        names = set(agent.tool_registry.list_names())
        assert "ask_user_question" in names and "present" in names
