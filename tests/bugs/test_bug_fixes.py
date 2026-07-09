"""BUGS_21_TO_FIX.md 回归测试 — 锁定已修复缺陷，防止再次退化。"""

import asyncio
from datetime import datetime

import pytest

from automind.core.types import Goal, Predicate


# ── B-02: 资源依赖边方向 ────────────────────────────────
class TestB02DependencyDirection:
    def test_producer_runs_before_consumer(self):
        from automind.planning.dependency_graph import TaskDependencyGraph

        producer = Goal(
            id="create_file", description="创建文件",
            expected_effects=[Predicate(name="file_exists", arguments=["a.txt"])],
        )
        consumer = Goal(
            id="read_file", description="读取文件",
            resource_deps=["file_exists"],
        )
        root = Goal(id="root", description="root", children=[consumer, producer])
        dg = TaskDependencyGraph()
        dg.build_from_goal_tree(root)
        order = dg.topological_order()
        assert order.index("create_file") < order.index("read_file")


# ── B-04: 检查点 datetime 往返 ──────────────────────────
class TestB04Checkpoint:
    def test_datetime_roundtrips_as_datetime(self, tmp_path):
        from automind.core.types import AgentState
        from automind.state.checkpoint import CheckpointManager

        mgr = CheckpointManager(tmp_path)
        state = AgentState(session_id="test")
        cid = asyncio.run(mgr.save(state))
        restored = asyncio.run(mgr.load(cid))
        assert isinstance(restored.last_updated, datetime)


# ── B-05 / B-15: 终端注入防护 ───────────────────────────
class TestB05Terminal:
    def test_injection_blocked(self):
        from automind.tools.terminal import TerminalTool
        r = asyncio.run(TerminalTool().execute(command="echo hi; rm -rf /"))
        assert not r.success

    def test_safe_command_ok(self):
        from automind.tools.terminal import TerminalTool
        r = asyncio.run(TerminalTool().execute(command="echo hi"))
        assert r.success

    def test_chained_safe_command_preserved(self):
        from automind.tools.terminal import TerminalTool
        r = asyncio.run(TerminalTool().execute(command="echo a && echo b"))
        assert r.success  # 合法的 && 链未被破坏


# ── B-06: 熔断器 HALF_OPEN 节流真正生效 ─────────────────
class TestB06CircuitBreaker:
    def test_half_open_counter_increments(self):
        from automind.reflection.retry_handler import (
            CircuitBreakerConfig,
            CircuitState,
            RetryConfig,
            RetryHandler,
        )

        h = RetryHandler(
            RetryConfig(max_retries=0, jitter=False),
            CircuitBreakerConfig(failure_threshold=10, half_open_max_requests=1),
        )
        h._circuit_state = CircuitState.HALF_OPEN

        async def boom():
            raise ValueError("fail")

        with pytest.raises(ValueError):
            asyncio.run(h.execute(boom))
        # 第二次请求应被 HALF_OPEN 节流器拦截（修复前计数器永不自增）
        with pytest.raises(RuntimeError, match="HALF_OPEN"):
            asyncio.run(h.execute(boom))


# ── B-08: 批量 metadata 独立 dict ───────────────────────
class TestB08LongTermMetadata:
    def test_metadatas_are_independent(self):
        from automind.memory.long_term import LongTermMemory

        mem = LongTermMemory(persist_dir=None)
        ids = asyncio.run(mem.add(["doc a", "doc b", "doc c"]))
        assert len(ids) == 3
        # 内存降级模式下每条 metadata 应互相独立
        stores = mem._in_memory_store
        stores[0]["metadata"]["k"] = "v0"
        assert "k" not in stores[1]["metadata"]


# ── B-09: 关系类型真实读回 ──────────────────────────────
class TestB09KnowledgeGraph:
    def test_relation_type_preserved(self):
        from automind.memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        kg.add_entity_simple("a") if hasattr(kg, "add_entity_simple") else None
        kg.add_relation("a", "b", "depends_on")
        rels = kg.get_relations("a", direction="out")
        assert rels and rels[0]["type"] == "depends_on"


# ── B-10: ToT 路径完整回溯 ──────────────────────────────
class TestB10ReasoningPath:
    def test_path_traces_to_root(self):
        from automind.planning.reasoning import ReasoningEngine, ThoughtNode

        eng = ReasoningEngine()
        root = ThoughtNode(id="root", content="R")
        mid = ThoughtNode(id="m", content="M", parent_id="root")
        leaf = ThoughtNode(id="l", content="L", parent_id="m")
        eng._node_registry = {"root": root, "m": mid, "l": leaf}
        assert eng._get_path(leaf) == "R → M → L"


# ── B-11: 令牌桶并发不为负 ──────────────────────────────
class TestB11TokenBucket:
    def test_no_negative_under_concurrency(self):
        from automind.state.resource_manager import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=1000.0, burst=5.0)

        async def run():
            await asyncio.gather(*[limiter.acquire(1) for _ in range(5)])
        asyncio.run(run())
        assert limiter._tokens >= 0


# ── B-16: 权限白名单生效 ────────────────────────────────
class TestB16Permissions:
    def test_allowed_paths_whitelist(self, tmp_path):
        from automind.tools.permissions import PermissionEngine

        eng = PermissionEngine()
        allowed_dir = tmp_path / "workspace"
        allowed_dir.mkdir()
        eng.policy.allowed_paths = [str(allowed_dir)]
        assert eng.check_path(str(allowed_dir / "ok.txt")) is True
        assert eng.check_path(str(tmp_path / "outside.txt")) is False


# ── B-17: 类型注解检测真实统计 ──────────────────────────
class TestB17TypeAnnotations:
    def test_ratio_based_detection(self, tmp_path):
        from automind.context.code_analyzer import CodeAnalyzer

        annotated = tmp_path / "typed.py"
        annotated.write_text(
            "def a(x: int) -> int:\n    return x\n"
            "def b(y: str) -> str:\n    return y\n",
            encoding="utf-8",
        )
        plain = tmp_path / "plain.py"
        plain.write_text("def a(x):\n    return x\ndef b(y):\n    return y\n", encoding="utf-8")

        az = CodeAnalyzer(project_root=tmp_path)
        assert az.analyze([str(annotated)]).style.type_annotations_used is True
        assert az.analyze([str(plain)]).style.type_annotations_used is False


# ── B-20: 同 ID 实体合并而非覆盖 ────────────────────────
class TestB20EntityMerge:
    def test_duplicate_id_merges(self):
        from automind.memory.entity_memory import EntityMemory

        em = EntityMemory()
        em._entities.clear()

        async def go():
            class _LLM:
                async def generate(self, msgs):
                    class R:
                        text = (
                            '[{"id":"e1","type":"concept","name":"X","properties":{"a":1}},'
                            '{"id":"e1","type":"concept","name":"X","properties":{"b":2}}]'
                        )
                    return R()
            await em._llm_extract("txt", _LLM())
        asyncio.run(go())
        assert em._entities["e1"].properties == {"a": 1, "b": 2}


# ── B-21: 压缩后 token 估算重算 ─────────────────────────
class TestB21ContextTokens:
    def test_recalculate_after_compress(self):
        from automind.context.context_manager import ContextManager
        from automind.core.types import Message, Role

        cm = ContextManager(max_tokens=100000)
        for i in range(12):
            cm.add(Message(role=Role.USER, content=f"message number {i} " * 5))
        asyncio.run(cm.compress())
        # 重算后估算值应等于"现存消息 + 摘要"的实际计数
        expected = sum(cm._token_counter.count(m.content) for m in cm._messages)
        if cm._summary:
            expected += cm._token_counter.count(cm._summary)
        assert cm._estimated_tokens == expected
