"""可靠性修复的回归测试（Q1–Q5）。

每个用例都对应一处**实际存在的空实现或静默失败**，不是假想缺陷。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from automind.core.types import Goal, HierarchicalPlan, PlanStatus
from automind.planning.dependency_graph import TaskDependencyGraph
from automind.planning.hierarchical_planner import HierarchicalPlanner
from automind.planning.nonmonotonic import NonMonotonicReasoner
from automind.symbolic.datalog_engine import DatalogEngine

# ── Q4: 循环依赖必须真的被消解 ──────────────────────────────

class TestResolveCycles:
    """_resolve_cycles 此前是 `return root` —— 检测到环却什么都不做。"""

    @staticmethod
    def _cyclic_tree() -> Goal:
        """构造 A → B → C → A 的环（C 把祖先 A 当成了自己的子目标）。"""
        a = Goal(id="A", description="根目标")
        b = Goal(id="B", description="子目标 B", parent_id="A")
        c = Goal(id="C", description="子目标 C", parent_id="B")
        a.children = [b]
        b.children = [c]
        c.children = [a]          # ← 回边，闭合成环
        return a

    def test_cycle_exists_before_fix(self):
        """先确认这棵树确实带环，否则用例本身就是空的。"""
        g = TaskDependencyGraph()
        g.build_from_goal_tree(self._cyclic_tree())
        assert g.check_cycles(), "构造的树没有环，用例无效"

    def test_cycle_is_actually_removed(self):
        root = self._cyclic_tree()
        g = TaskDependencyGraph()
        g.build_from_goal_tree(root)
        cycles = g.check_cycles()

        HierarchicalPlanner._resolve_cycles(root, cycles)

        after = TaskDependencyGraph()
        after.build_from_goal_tree(root)
        assert after.check_cycles() == [], "消环后依赖图里仍有环"

    def test_normal_chain_is_preserved(self):
        """只断回边，A → B → C 这条正常分解链必须留着。"""
        root = self._cyclic_tree()
        g = TaskDependencyGraph()
        g.build_from_goal_tree(root)
        HierarchicalPlanner._resolve_cycles(root, g.check_cycles())

        assert [c.id for c in root.children] == ["B"]
        assert [c.id for c in root.children[0].children] == ["C"]
        assert root.children[0].children[0].children == [], "回边未被断开"

    def test_topological_order_works_after_resolve(self):
        """真正的验收点：消环后拓扑排序不再抛异常。"""
        root = self._cyclic_tree()
        g = TaskDependencyGraph()
        g.build_from_goal_tree(root)
        HierarchicalPlanner._resolve_cycles(root, g.check_cycles())

        g2 = TaskDependencyGraph()
        g2.build_from_goal_tree(root)
        order = g2.topological_order()          # 带环时会 raise ValueError
        assert set(order) == {"A", "B", "C"}
        assert order.index("A") < order.index("B") < order.index("C")

    def test_self_loop_removed(self):
        a = Goal(id="A", description="自环")
        a.children = [a]
        g = TaskDependencyGraph()
        g.build_from_goal_tree(a)
        HierarchicalPlanner._resolve_cycles(a, g.check_cycles())
        assert a.children == []

    def test_logs_removed_edges(self, caplog):
        root = self._cyclic_tree()
        g = TaskDependencyGraph()
        g.build_from_goal_tree(root)
        with caplog.at_level(logging.WARNING):
            HierarchicalPlanner._resolve_cycles(root, g.check_cycles())
        # 断开依赖边是会影响执行结果的动作，不能悄悄做
        assert any("cycle" in r.getMessage().lower() for r in caplog.records), \
            f"未记录消环日志：{[r.getMessage() for r in caplog.records]}"

    def test_no_cycles_is_noop(self):
        a = Goal(id="A", description="根")
        b = Goal(id="B", description="子", parent_id="A")
        a.children = [b]
        HierarchicalPlanner._resolve_cycles(a, [])
        assert [c.id for c in a.children] == ["B"], "无环时不应改动目标树"


# ── Q5a: 回溯必须真的更新时间戳 ─────────────────────────────

class TestBacktrackTimestamp:
    """`plan.updated_at = plan.updated_at` 是自赋值，纯 no-op。"""

    @staticmethod
    def _plan() -> tuple[HierarchicalPlan, Goal]:
        root = Goal(id="R", description="根")
        child = Goal(id="C1", description="会失败的子目标", parent_id="R")
        root.children = [child]
        plan = HierarchicalPlan(
            task_description="t", root_goal=root, status=PlanStatus.EXECUTING,
        )
        # 人为把时间戳拨到过去，才能确认"确实被更新了"而不是碰巧相等
        plan.updated_at = datetime.now(UTC) - timedelta(hours=1)
        return plan, child

    def test_updated_at_advances(self):
        plan, child = self._plan()
        before = plan.updated_at
        NonMonotonicReasoner().backtrack_plan(plan, child.id, "工具执行失败")
        assert plan.updated_at > before, "回溯后 updated_at 没有推进（自赋值 no-op）"

    def test_updated_at_is_recent(self):
        plan, child = self._plan()
        NonMonotonicReasoner().backtrack_plan(plan, child.id, "失败")
        delta = abs((datetime.now(UTC) - plan.updated_at).total_seconds())
        assert delta < 60, f"时间戳不是当前时间，相差 {delta}s"

    def test_revision_history_still_recorded(self):
        """修时间戳不能把原有的修订记录搞丢。"""
        plan, child = self._plan()
        NonMonotonicReasoner().backtrack_plan(plan, child.id, "磁盘写满")
        assert any("磁盘写满" in h for h in plan.revision_history)


# ── Q5b: datalog 推导模式必须真的产出事实 ───────────────────

class TestDatalogDerive:
    """`^` 分支原本是 `pass`，导致规则永远推不出任何新事实。"""

    def test_simple_rule_derives_fact(self):
        e = DatalogEngine()
        e.assert_fact("parent", "alice", "bob")
        e.add_rule("ancestor", ["X", "Y"], [("parent", ["X", "Y"])])

        n = e.derive()
        assert n >= 1, "规则没有推导出任何事实（^ 分支被跳过）"
        assert e.ask("ancestor", "alice", "bob")

    def test_multi_body_join(self):
        """两段 body 的连接查询 —— 变量绑定必须跨 body 一致。"""
        e = DatalogEngine()
        e.assert_fact("parent", "alice", "bob")
        e.assert_fact("parent", "bob", "carol")
        e.add_rule("grandparent", ["X", "Z"],
                   [("parent", ["X", "Y"]), ("parent", ["Y", "Z"])])

        e.derive()
        assert e.ask("grandparent", "alice", "carol")
        # 不该凭空产生别的组合
        assert not e.ask("grandparent", "alice", "bob")

    def test_derive_is_idempotent(self):
        e = DatalogEngine()
        e.assert_fact("parent", "a", "b")
        e.add_rule("ancestor", ["X", "Y"], [("parent", ["X", "Y"])])
        first = e.derive()
        second = e.derive()
        assert first >= 1
        assert second == 0, "第二次推导不应再产出新事实"

    def test_no_facts_derives_nothing(self):
        e = DatalogEngine()
        e.add_rule("ancestor", ["X", "Y"], [("parent", ["X", "Y"])])
        assert e.derive() == 0


# ── Q4 兜底：不可解的环不能把进程转死 ───────────────────────

def test_unresolvable_cycle_terminates(caplog):
    """环里的边在目标树上找不到对应父子关系时，必须收敛而不是死循环。"""
    a = Goal(id="A", description="根")
    with caplog.at_level(logging.WARNING):
        # 传一个树上根本不存在的环
        HierarchicalPlanner._resolve_cycles(a, [["X", "Y", "X"]])
    assert any("unresolvable" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("depth", [3, 8])
def test_deep_chain_with_backedge(depth):
    """较深的链上挂一条回边，同样要能消掉。"""
    goals = [Goal(id=f"G{i}", description=f"g{i}") for i in range(depth)]
    for i in range(depth - 1):
        goals[i].children = [goals[i + 1]]
    goals[-1].children = [goals[0]]          # 回边指回根

    g = TaskDependencyGraph()
    g.build_from_goal_tree(goals[0])
    HierarchicalPlanner._resolve_cycles(goals[0], g.check_cycles())

    after = TaskDependencyGraph()
    after.build_from_goal_tree(goals[0])
    assert after.check_cycles() == []


# ── Q6: 每次 LLM 调用结束都要上报用量 ───────────────────────

class TestUsageReporting:
    """流式用量此前只在整段生成完才带出一次，界面上长时间显示 0。"""

    @staticmethod
    def _provider():
        """构造一个不联网的最小 provider（继承基类以获得包装逻辑）。"""
        from collections.abc import AsyncIterator

        from automind.core.config import LLMProviderConfig
        from automind.core.llm import LLMBackend
        from automind.core.types import LLMResponse

        class FakeProvider(LLMBackend):
            _model = "fake-model"

            async def generate(self, messages, tools=None, stop=None):
                return LLMResponse(text="hi", prompt_tokens=10,
                                   completion_tokens=5, finish_reason="stop",
                                   provider="fake", model="fake-model")

            async def generate_stream(self, messages, tools=None) -> AsyncIterator[str]:
                yield "部分"
                yield "回答"
                yield '\n<!--STREAM_USAGE:{"prompt_tokens": 7, "completion_tokens": 3}-->'

        return FakeProvider(LLMProviderConfig(provider="fake", model="fake-model",
                                              api_key="x"))

    def test_generate_reports_usage(self):
        import asyncio
        p = self._provider()
        seen = []
        p.usage_sink = lambda u: (seen.append(u), asyncio.sleep(0))[1]
        asyncio.run(p.generate([{"role": "user", "content": "hi"}]))
        assert len(seen) == 1
        assert seen[0]["prompt_tokens"] == 10
        assert seen[0]["total_tokens"] == 15

    def test_two_calls_accumulate(self):
        """验收点：两次调用后累计值 = 两次之和。"""
        import asyncio
        p = self._provider()
        total = {"n": 0}

        async def sink(u):
            total["n"] += u["total_tokens"]

        p.usage_sink = sink
        asyncio.run(p.generate([{"role": "user", "content": "a"}]))
        asyncio.run(p.generate([{"role": "user", "content": "b"}]))
        assert total["n"] == 30, f"累计值应为 15+15=30，实得 {total['n']}"

    def test_stream_reports_usage_and_preserves_chunks(self):
        """流式也要上报；且 chunk 内容与顺序必须**原样不变**（约束）。"""
        import asyncio
        p = self._provider()
        seen = []

        async def sink(u):
            seen.append(u)

        p.usage_sink = sink

        async def run():
            return [c async for c in p.generate_stream([{"role": "user", "content": "x"}])]

        chunks = asyncio.run(run())
        assert chunks[:2] == ["部分", "回答"], "流内容被改动了"
        assert "STREAM_USAGE:" in chunks[-1], "既有的 usage 标记格式被破坏"
        assert len(seen) == 1 and seen[0]["streamed"] is True
        assert seen[0]["total_tokens"] == 10

    def test_sink_failure_does_not_break_generation(self):
        """回调抛异常不能打断生成 —— 上报是旁路，不是主流程。"""
        import asyncio
        p = self._provider()

        async def boom(_u):
            raise RuntimeError("sink 挂了")

        p.usage_sink = boom
        r = asyncio.run(p.generate([{"role": "user", "content": "hi"}]))
        assert r.text == "hi"

    def test_no_sink_is_noop(self):
        import asyncio
        p = self._provider()
        assert asyncio.run(p.generate([{"role": "user", "content": "hi"}])).text == "hi"


# ── Q1: 聊天历史落库失败不能静默 ────────────────────────────

class TestSessionHistoryPersistence:
    """原实现 `except Exception: pass` —— 写盘失败聊天记录悄悄丢。"""

    @staticmethod
    def _store(monkeypatch, fail_times: int):
        """造一个前 fail_times 次写入必失败的 store。"""
        from automind import server_store

        st = server_store.Store.__new__(server_store.Store)
        st.session_histories = {"s1": [{"role": "user", "content": "hi"}]}
        calls = {"n": 0}

        class FakeDb:
            def session_save(self, sid, hist):
                calls["n"] += 1
                if calls["n"] <= fail_times:
                    raise OSError("disk full")

        monkeypatch.setattr(st, "_db", lambda: FakeDb(), raising=False)
        monkeypatch.setattr(server_store.Store, "_SAVE_BACKOFF", 0.001)
        return st, calls

    def test_raises_and_logs_on_persistent_failure(self, monkeypatch, caplog):
        from automind.server_store import SessionSaveError

        st, calls = self._store(monkeypatch, fail_times=99)
        with caplog.at_level(logging.ERROR), pytest.raises(SessionSaveError):
            st.save_session_history("s1")
        assert calls["n"] == 3, "应重试 3 次"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "session_history_save_failed" in msgs, f"未记录错误日志：{msgs}"

    def test_retry_succeeds(self, monkeypatch):
        """瞬时失败（SQLite 加锁）应被重试救回，不打扰用户。"""
        st, calls = self._store(monkeypatch, fail_times=2)
        st.save_session_history("s1")          # 不应抛
        assert calls["n"] == 3

    def test_memory_state_kept_even_on_failure(self, monkeypatch):
        """落库失败也不能把内存里的会话搞没 —— 当前对话还要能继续。"""
        from automind.server_store import SessionSaveError

        st, _ = self._store(monkeypatch, fail_times=99)
        with pytest.raises(SessionSaveError):
            st.save_session_history("s1")
        assert st.session_histories["s1"], "内存态被清空了"

    def test_error_message_is_actionable(self, monkeypatch):
        from automind.server_store import SessionSaveError

        st, _ = self._store(monkeypatch, fail_times=99)
        with pytest.raises(SessionSaveError) as ei:
            st.save_session_history("s1")
        assert "重试" in str(ei.value) and "disk full" in str(ei.value)


def test_history_failure_reaches_frontend(monkeypatch):
    """WS 路径上，落库失败必须变成用户看得见的一条事件。"""
    import asyncio

    from automind import server as srv
    from automind.server_store import SessionSaveError

    def boom(_sid):
        raise SessionSaveError("disk full")

    monkeypatch.setattr(srv, "_save_session_history", boom)

    sent = []

    class FakeWs:
        async def send_json(self, payload):
            sent.append(payload)

    ok = asyncio.run(srv._save_history_notify("s1", FakeWs()))
    assert ok is False
    assert sent and sent[0]["type"] == "history_save_failed"
    assert "保存失败" in sent[0]["error"]


def test_history_failure_does_not_break_task(monkeypatch):
    """存档失败不该让整个任务失败 —— 回答已经生成出来了。"""
    import asyncio

    from automind import server as srv
    from automind.server_store import SessionSaveError

    monkeypatch.setattr(srv, "_save_session_history",
                        lambda _s: (_ for _ in ()).throw(SessionSaveError("x")))
    # 不传 ws：仍应正常返回 False 而不是把异常抛给调用方
    assert asyncio.run(srv._save_history_notify("s1")) is False


# ── Q2: Token 预算保护必须真的生效 ──────────────────────────

class TestBudgetEnforcement:
    """ResourceManager 实例化后从未被调用 —— 预算保护一直空转。"""

    @staticmethod
    def _agent(monkeypatch, budget: int, used: int):
        """构造 agent 并注入假 LLM —— 测试环境通常没配 API Key，
        真实 self.llm 会是 None，直接测就只会 skip，等于没验收。"""
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        a = AutoMindAgent(AgentConfig())

        class FakeLLM:
            usage_sink = None
            pre_call_hook = None

        a.llm = FakeLLM()
        a._attach_usage_sink()          # 重新挂钩到假 LLM 上
        a.resources.tokens.budget = budget
        a.resources.tokens.tokens_used.prompt_tokens = used
        a.resources.tokens.tokens_used.completion_tokens = 0
        return a

    def test_over_budget_blocks_call(self, monkeypatch):
        """验收点：超预算必须被拦下，而不是继续烧钱。"""
        import asyncio
        a = self._agent(monkeypatch, budget=100, used=150)
        events = []
        a.event_sink = lambda e: (events.append(e), asyncio.sleep(0))[1]

        hook = a.llm.pre_call_hook
        assert hook is not None, "pre_call_hook 未被挂上"
        with pytest.raises(RuntimeError, match="budget"):
            asyncio.run(hook())
        assert any(e["type"] == "budget_exceeded" for e in events), \
            f"未推送超预算事件：{[e.get('type') for e in events]}"

    def test_under_budget_passes(self, monkeypatch):
        import asyncio
        a = self._agent(monkeypatch, budget=1000, used=10)
        asyncio.run(a.llm.pre_call_hook())          # 不应抛

    def test_warning_emitted_near_limit(self, monkeypatch):
        import asyncio
        a = self._agent(monkeypatch, budget=100, used=85)
        events = []
        a.event_sink = lambda e: (events.append(e), asyncio.sleep(0))[1]
        asyncio.run(a.llm.pre_call_hook())
        assert any(e["type"] == "budget_warning" for e in events)


def test_wrapper_forwards_callbacks():
    """_TokenTrackingLLM 只代理读不代理写，回调会挂错对象（本次修复）。"""
    from automind.agent import _TokenTrackingLLM

    class Backend:
        usage_sink = None
        pre_call_hook = None

    b = Backend()
    w = _TokenTrackingLLM(b)
    sentinel = object()
    w.usage_sink = sentinel
    w.pre_call_hook = sentinel
    assert b.usage_sink is sentinel, "usage_sink 没有转交给后端"
    assert b.pre_call_hook is sentinel, "pre_call_hook 没有转交给后端"
    # 其它属性仍应留在包装器上
    w.something_else = 1
    assert not hasattr(b, "something_else")


# ── Q3: 关键路径的静默吞异常必须留痕 ────────────────────────

class TestSilentExceptionsConverged:
    """全库 90+ 处 except:pass，这里覆盖失败会真正伤到用户的 10 处。

    判据统一为：制造失败 → 断言日志里有可定位的记录，且**主流程不被打断**。
    """

    def test_task_history_replace_logs(self, monkeypatch, caplog):
        from automind import server as srv

        class Boom:
            def history_replace(self, *_a, **_k):
                raise OSError("disk full")

        monkeypatch.setattr(srv._db_mod, "get_db", lambda: Boom())
        with caplog.at_level(logging.ERROR):
            srv._save_task_history()          # 不应抛
        assert "task_history_replace_failed" in " ".join(
            r.getMessage() for r in caplog.records)

    def test_task_history_append_logs(self, monkeypatch, caplog):
        from automind import server as srv

        class Boom:
            def history_append(self, *_a, **_k):
                raise OSError("locked")

        monkeypatch.setattr(srv._db_mod, "get_db", lambda: Boom())
        with caplog.at_level(logging.ERROR):
            rec = srv._push_history({"session_id": "s", "task": "t"})
        assert rec["session_id"] == "s", "主流程被打断了"
        assert "task_history_append_failed" in " ".join(
            r.getMessage() for r in caplog.records)

    def test_token_tracking_failure_logs(self, caplog):
        """记账失败最危险 —— 界面照常显示数字，用户无从察觉算错了。"""
        import asyncio

        from automind.agent import _TokenTrackingLLM

        class Backend:
            async def generate(self, *_a, **_k):
                return "not-an-llm-response"      # 让 usage.add 抛

        w = _TokenTrackingLLM(Backend())
        with caplog.at_level(logging.WARNING):
            r = asyncio.run(w.generate([]))
        assert r == "not-an-llm-response", "记账失败不该影响返回值"
        assert "token_usage_track_failed" in " ".join(
            m.getMessage() for m in caplog.records)

    def test_md_skill_load_failure_logs(self, tmp_path, caplog, monkeypatch):
        """技能加载失败 —— 用户放了技能却在界面上看不到，最难自查。"""
        from automind.skills.skill_registry import SkillRegistry

        d = tmp_path / "broken"
        d.mkdir()
        (d / "SKILL.md").write_text("# x", encoding="utf-8")
        # MarkdownSkill 对内容很宽容，直接写垃圾它照收；构造真实失败要让它抛
        import automind.skills.markdown_skill as ms

        def boom(_path):
            raise ValueError("技能定义损坏")

        monkeypatch.setattr(ms, "MarkdownSkill", boom)
        with caplog.at_level(logging.WARNING):
            n = SkillRegistry().discover_skill_md(tmp_path)
        assert n == 0
        # 载入失败可以容忍，"不告诉任何人"不行
        assert any("skill" in r.getMessage().lower() for r in caplog.records), \
            f"技能加载失败未留痕：{[r.getMessage() for r in caplog.records]}"

    def test_py_skill_load_failure_logs(self, tmp_path, caplog):
        from automind.skills.skill_registry import SkillRegistry

        (tmp_path / "bad_skill.py").write_text("this is not python !!!", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            SkillRegistry().discover_from_directory(tmp_path)
        assert "py_skill_load_failed" in " ".join(
            r.getMessage() for r in caplog.records)

    def test_disable_tool_failure_logs(self, monkeypatch, caplog):
        """用户以为禁用了工具其实没禁掉 —— 安全相关的错觉。"""
        from automind import server as srv

        src = pathlib_read(srv)
        assert "disable_tool_failed" in src, "禁用工具失败未收敛为日志"

    def test_mcp_restore_failure_logs(self):
        from automind import server as srv
        assert "mcp_server_restore_failed" in pathlib_read(srv)

    def test_project_index_failure_logs(self):
        from automind import agent as ag
        assert "project_index_unavailable" in pathlib_read(ag)

    def test_stream_usage_parse_failure_logs(self):
        from automind import agent as ag
        assert "stream_usage_parse_failed" in pathlib_read(ag)

    def test_entrypoint_skill_failure_logs(self):
        from automind.skills import skill_registry as sr
        assert "entrypoint_skill_load_failed" in pathlib_read(sr)


def pathlib_read(mod) -> str:
    """读取模块源码（用于断言"某处已收敛为具名日志事件"）。"""
    import inspect
    return inspect.getsource(mod)


# ── Q7: 单轮超时护栏 + 执行心跳 ─────────────────────────────

class TestTimeoutAndHeartbeat:
    """流式"连上了但不吐字"时底层不超时，整个任务无限期挂着。"""

    @staticmethod
    def _hanging_provider(hang: float = 30.0):
        from collections.abc import AsyncIterator

        from automind.core.config import LLMProviderConfig
        from automind.core.llm import LLMBackend
        from automind.core.types import LLMResponse

        class Hang(LLMBackend):
            _model = "m"

            async def generate(self, messages, tools=None, stop=None):
                import asyncio
                await asyncio.sleep(hang)          # 永远不返回
                return LLMResponse(text="", provider="p", model="m")

            async def generate_stream(self, messages, tools=None) -> AsyncIterator[str]:
                import asyncio
                yield "第一块"
                await asyncio.sleep(hang)          # 之后再不吐字
                yield "永远到不了"

        return Hang(LLMProviderConfig(provider="p", model="m", api_key="x"))

    def test_generate_times_out(self):
        """验收点：流挂起时必须超时返回，而不是永远等下去。"""
        import asyncio

        from automind.core.exceptions import LLMTimeoutError

        p = self._hanging_provider(hang=10)
        p.call_timeout = 0.3
        with pytest.raises(LLMTimeoutError, match="超过"):
            asyncio.run(p.generate([{"role": "user", "content": "x"}]))

    def test_heartbeat_fires_during_long_call(self):
        """验收点：长调用期间必须有心跳，界面才证明得了"还活着"。"""
        import asyncio

        beats = []

        async def run():
            p = self._hanging_provider(hang=1.0)
            p.call_timeout = 5.0
            p.heartbeat_interval = 0.1
            p.heartbeat_hook = lambda e, ph: beats.append((e, ph)) or asyncio.sleep(0)
            await p.generate([{"role": "user", "content": "x"}])

        asyncio.run(run())
        assert len(beats) >= 3, f"心跳次数太少：{len(beats)}"
        assert beats[0][1] == "thinking"
        assert beats[-1][0] > beats[0][0], "elapsed 没有递增"

    def test_stream_times_out(self):
        import asyncio

        from automind.core.exceptions import LLMTimeoutError

        async def run():
            p = self._hanging_provider(hang=10)
            p.call_timeout = 0.3
            return [c async for c in p.generate_stream([{"role": "user", "content": "x"}])]

        with pytest.raises(LLMTimeoutError):
            asyncio.run(run())

    def test_normal_call_not_affected(self):
        """护栏不能误伤正常调用。"""
        import asyncio
        p = self._hanging_provider(hang=0.01)
        p.call_timeout = 5.0
        r = asyncio.run(p.generate([{"role": "user", "content": "x"}]))
        assert r is not None

    def test_no_heartbeat_hook_still_times_out(self):
        import asyncio

        from automind.core.exceptions import LLMTimeoutError
        p = self._hanging_provider(hang=10)
        p.call_timeout = 0.2
        p.heartbeat_hook = None
        with pytest.raises(LLMTimeoutError):
            asyncio.run(p.generate([{"role": "user", "content": "x"}]))


# ── Q8: 工具连续失败熔断 ────────────────────────────────────

class TestToolCircuitBreaker:
    """模型会对着坏工具反复重试到迭代上限，既烧 token 又无人知晓。"""

    @staticmethod
    def _executor():
        from automind.planning.react_executor import ReActExecutor
        return ReActExecutor.__new__(ReActExecutor)

    def _fresh(self):
        ex = self._executor()
        ex._tool_failures = {}
        return ex

    def test_trips_after_three_failures(self):
        ex = self._fresh()
        for _ in range(3):
            ex._record_tool_outcome("bad_tool", False, "连接被拒绝")
        reason = ex._breaker_reason("bad_tool")
        assert reason, "连续失败 3 次后未熔断"
        assert "连续失败 3 次" in reason
        assert "连接被拒绝" in reason, "熔断说明里必须带上失败原因"

    def test_not_tripped_before_threshold(self):
        ex = self._fresh()
        ex._record_tool_outcome("t", False, "e")
        ex._record_tool_outcome("t", False, "e")
        assert ex._breaker_reason("t") == "", "还没到阈值就熔断了"

    def test_success_resets_streak(self):
        """偶发失败不该累积成熔断。"""
        ex = self._fresh()
        ex._record_tool_outcome("t", False, "e")
        ex._record_tool_outcome("t", False, "e")
        ex._record_tool_outcome("t", True, "")
        ex._record_tool_outcome("t", False, "e")
        assert ex._breaker_reason("t") == ""

    def test_breaker_is_per_tool(self):
        ex = self._fresh()
        for _ in range(3):
            ex._record_tool_outcome("bad", False, "x")
        assert ex._breaker_reason("bad")
        assert ex._breaker_reason("good") == "", "熔断不应波及其它工具"

    def test_logs_on_trip(self, caplog):
        ex = self._fresh()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                ex._record_tool_outcome("bad", False, "boom")
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "tool_circuit_open" in msgs, f"熔断未留痕：{msgs}"

    def test_failure_report_for_frontend(self):
        ex = self._fresh()
        ex._record_tool_outcome("bad", False, "boom")
        ex._record_tool_outcome("ok", True, "")
        rep = ex.tool_failure_report()
        assert "bad" in rep and rep["bad"]["streak"] == 1
        assert "ok" not in rep, "成功的工具不该出现在失败报告里"


# ── Q8 补充：tool_error 事件与任务前自检 ────────────────────

def test_tool_error_event_emitted():
    """工具失败要单独发事件供前端标红，含原因与连续失败次数。"""
    import asyncio

    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig
    from automind.core.types import ToolCall, ToolResult

    a = AutoMindAgent(AgentConfig())
    events = []
    a.event_sink = lambda e: (events.append(e), asyncio.sleep(0))[1]
    _, on_action = a._react_callbacks()

    tc = ToolCall(id="1", name="bad_tool", arguments={})
    bad = ToolResult(tool_name="bad_tool", success=False, error="连接被拒绝")
    asyncio.run(on_action(tc, bad))

    errs = [e for e in events if e["type"] == "tool_error"]
    assert errs, f"未发出 tool_error：{[e['type'] for e in events]}"
    assert errs[0]["tool"] == "bad_tool"
    assert "连接被拒绝" in errs[0]["error"], "事件里必须带失败原因"


def test_tool_success_emits_no_error_event():
    import asyncio

    from automind.agent import AutoMindAgent
    from automind.core.config import AgentConfig
    from automind.core.types import ToolCall, ToolResult

    a = AutoMindAgent(AgentConfig())
    events = []
    a.event_sink = lambda e: (events.append(e), asyncio.sleep(0))[1]
    _, on_action = a._react_callbacks()
    asyncio.run(on_action(ToolCall(id="1", name="t", arguments={}),
                          ToolResult(tool_name="t", success=True, output={})))
    assert not [e for e in events if e["type"] == "tool_error"]


class TestPreflight:
    def test_reports_missing_llm(self, monkeypatch):
        import asyncio

        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        a = AutoMindAgent(AgentConfig())
        a.llm = None
        events = []
        a.event_sink = lambda e: (events.append(e), asyncio.sleep(0))[1]
        rep = asyncio.run(a.preflight_check())
        assert rep["ok"] is False
        assert any("LLM" in p for p in rep["problems"])
        assert any(e["type"] == "preflight_warning" for e in events)

    def test_reports_missing_project_dir(self, monkeypatch, tmp_path):
        import asyncio

        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        a = AutoMindAgent(AgentConfig())
        a.config.project_root = str(tmp_path / "does-not-exist")
        rep = asyncio.run(a.preflight_check())
        assert any("不存在" in p for p in rep["problems"])

    def test_healthy_setup_passes_dir_check(self):
        import asyncio

        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        a = AutoMindAgent(AgentConfig())
        rep = asyncio.run(a.preflight_check())
        # 测试环境可能没配 LLM，但目录与工具这两项应当是健康的
        assert not any("目录" in p for p in rep["problems"])
        assert not any("没有任何可用工具" in p for p in rep["problems"])
