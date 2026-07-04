"""v0.4 修复回归测试 — 真实 embedding / 检查点恢复 / 安全头 / 前端健壮性静态断言。"""

import asyncio
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

JS_DIR = Path(__file__).resolve().parents[2] / "automind" / "static" / "js"


# ── 真实 embedding 替换 SHA256 ──
class TestRealEmbedding:
    def test_similar_scores_higher_than_unrelated(self):
        from automind.memory.long_term import _SimpleEmbedder

        e = _SimpleEmbedder()

        def cos(a, b):
            return sum(x * y for x, y in zip(a, b))

        a = e.embed("how to fix a python import error")
        b = e.embed("fixing python import errors in code")
        c = e.embed("the weather is sunny in paris today")
        assert cos(a, b) > cos(a, c)
        assert abs(cos(a, a) - 1.0) < 1e-6  # L2 归一化 → 自相似为 1

    def test_deterministic(self):
        from automind.memory.long_term import _SimpleEmbedder

        e = _SimpleEmbedder()
        assert e.embed("hello world") == e.embed("hello world")

    def test_not_sha256_all_different(self):
        # 旧 SHA256 实现下，1 字符差异 → 完全不同向量、相似度≈0；新实现应保持高相似
        from automind.memory.long_term import _SimpleEmbedder

        e = _SimpleEmbedder()
        v1, v2 = e.embed("create a report"), e.embed("create a reports")

        def cos(a, b):
            return sum(x * y for x, y in zip(a, b))

        assert cos(v1, v2) > 0.5


# ── 检查点恢复（CLI --restore 不再是 TODO）──
class TestCheckpointRestore:
    def test_restore_roundtrip(self):
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig
        from automind.core.types import AgentState, Message, Role
        from automind.state.checkpoint import CheckpointManager

        async def go():
            d = tempfile.mkdtemp()
            cfg = AgentConfig(project_root=".")
            cfg.execution.checkpoint_dir = d
            mgr = CheckpointManager(d)
            st = AgentState(session_id="restore-rt")
            st.messages = [Message(role=Role.USER, content="hi"),
                           Message(role=Role.ASSISTANT, content="ok")]
            cid = await mgr.save(st)
            agent = await AutoMindAgent.from_checkpoint(cid, cfg)
            assert agent._agent_state.session_id == "restore-rt"
            assert len(agent._chat_history) == 2
            r = await agent.resume_from_checkpoint(cid)
            assert r.success  # 无计划 → 恢复上下文即成功
            await agent.close()

        asyncio.run(go())

    def test_agent_has_restore_methods(self):
        from automind.agent import AutoMindAgent
        assert hasattr(AutoMindAgent, "from_checkpoint")
        assert hasattr(AutoMindAgent, "resume_from_checkpoint")


# ── 安全响应头 ──
class TestSecurityHeaders:
    def test_headers_present(self):
        import automind.server as srv
        srv._AUTH_TOKEN = ""
        with TestClient(srv.app) as c:
            r = c.get("/")
            assert r.headers.get("X-Content-Type-Options") == "nosniff"
            assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
            assert r.headers.get("Referrer-Policy") == "no-referrer"
            assert "Content-Security-Policy" in r.headers
            csp = r.headers["Content-Security-Policy"]
            assert "frame-ancestors 'self'" in csp
            assert "default-src 'self'" in csp

    def test_api_has_nosniff(self):
        import automind.server as srv
        srv._AUTH_TOKEN = ""
        with TestClient(srv.app) as c:
            assert c.get("/api/health").headers.get("X-Content-Type-Options") == "nosniff"


# ── 前端健壮性静态断言 ──
class TestFrontendRobustness:
    def _js(self, name):
        return (JS_DIR / name).read_text(encoding="utf-8")

    def test_ws_exponential_backoff(self):
        ws = self._js("ws.js")
        assert "Math.pow(2" in ws, "WS 重连未用指数退避"
        assert "Math.random()" in ws, "WS 退避未加抖动"
        assert re.search(r"Math\.min\(\s*30000", ws), "WS 退避未封顶 30s"

    def test_block_arrays_cleared(self):
        chat = self._js("chat.js")
        assert "_htmlBlocks.length = 0" in chat and "_codeBlocks.length = 0" in chat, \
            "块数组未在渲染后清空（内存泄漏）"
        assert "data-hblk=" in chat, "html 块内容未内联到 data 属性"

    def test_stream_residual_removed(self):
        ws = self._js("ws.js")
        assert "_streamDiv.remove()" in ws, "错误/取消时未移除残留流式气泡"

    def test_localstorage_capped(self):
        core = self._js("core.js")
        assert "_TX_PER_MODE" in core and "_TX_TOTAL" in core, "localStorage 无大小上限"
        assert "_trimTranscripts" in core

    def test_append_dedup(self):
        core = self._js("core.js")
        chat = self._js("chat.js")
        assert "function buildMessageEl(" in core, "缺少统一消息构造器"
        assert "buildMessageEl(role, text, images)" in core
        assert "buildMessageEl(role, content, images)" in chat, "appendMessage 未复用构造器"

    def test_no_hardcoded_admin_path(self):
        panels = self._js("panels.js")
        assert "Administrator" not in panels, "仍硬编码 Administrator 桌面路径"
