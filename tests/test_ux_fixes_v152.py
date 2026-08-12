"""v1.5.2 开箱体验修复的回归测试。

每个用例都对应一处用户第一次跑就会撞上的真实故障，不是假想缺陷：

* 中文 Windows 控制台 GBK 编码 → 启动即 ``UnicodeEncodeError`` 崩溃；
* 默认 openai/gpt-4o 与"只配了 DeepSeek Key"不匹配 → 启动即模型连接失败；
* 社区版并发任务共用全局 Agent → 模式/上下文/token 计数互相污染；
* 切模型/切模式走整体重建 → Web 上点一下卡 2~3 秒。
"""

from __future__ import annotations

import io
import sys

import pytest

from automind.core import provider_resolver as pr
from automind.core.config import AgentConfig
from automind.core.console import enable_utf8_console

# ── 1. 控制台编码：中文/符号写出去不能把进程打崩 ──────────────

class TestConsoleEncoding:
    """`launch.bat` 里的 chcp 65001 只救了一个入口，自愈必须做在进程内。"""

    BANNER = "╔═══ AutoMind ✅ 启动 ═══╗"

    def test_gbk_stream_would_crash_without_fix(self):
        """先确认前提成立：GBK 流写制表符确实抛异常，否则用例是空的。"""
        gbk = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        with pytest.raises(UnicodeEncodeError):
            gbk.write(self.BANNER)
            gbk.flush()

    def test_reconfigured_stream_never_raises(self):
        """重配后同一段文本只会被替换，不会再抛异常。"""
        from automind.core.console import _reconfigure_stream

        buf = io.BytesIO()
        gbk = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
        _reconfigure_stream(gbk)
        gbk.write(self.BANNER)   # 不抛 = 修复生效
        gbk.flush()
        assert buf.getvalue(), "重配后应仍能写出内容"

    def test_reconfigure_tolerates_non_reconfigurable_stream(self):
        """StringIO（测试/管道场景）没有 reconfigure，必须静默跳过而非报错。"""
        from automind.core.console import _reconfigure_stream

        _reconfigure_stream(io.StringIO())

    def test_enable_is_idempotent(self):
        """入口点可能被多次调用（REPL → run_cli），重复调用不得有副作用。"""
        enable_utf8_console()
        enable_utf8_console()
        for stream in (sys.stdout, sys.stderr):
            enc = getattr(stream, "encoding", "") or ""
            if enc:                       # pytest 的捕获流可能没有 encoding
                assert enc.lower().replace("-", "") in ("utf8", "utf8mb4", "cp1252",
                                                        "ascii", "gbk", "cp936", "utf8")


# ── 2. 默认模型与实际 Key 对齐 ────────────────────────────────

class TestProviderResolver:
    """"只配了 DeepSeek 却默认 openai" —— 用户看到的是"模型连接失败"。"""

    def test_no_change_when_current_provider_has_key(self):
        p, m, note = pr.resolve("deepseek", "deepseek-chat", api_key="sk-x")
        assert (p, m, note) == ("deepseek", "deepseek-chat", "")

    def test_falls_back_to_the_provider_that_has_a_key(self):
        p, m, note = pr.resolve("openai", "gpt-4o", api_key="",
                                saved_keys={"deepseek": "sk-x"})
        assert p == "deepseek"
        assert m == "deepseek-chat"
        assert "DeepSeek" in note and "deepseek-chat" in note

    def test_note_is_actionable_when_nothing_is_configured(self):
        """一个 Key 都没有时不该谎称"已切换"，要指路去哪配。"""
        p, m, note = pr.resolve("openai", "gpt-4o", api_key="", saved_keys={})
        assert (p, m) == ("openai", "gpt-4o")
        assert "API Key" in note

    def test_env_var_counts_as_configured(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "sk-zhipu")
        p, m, note = pr.resolve("openai", "gpt-4o", api_key="", saved_keys={})
        assert p == "zhipu" and m == "glm-4-plus"

    def test_does_not_silently_pick_ollama(self, monkeypatch):
        """ollama 不要 Key 但要本地服务在跑，静默切过去只会换个更难懂的错。"""
        for var in set(pr.ENV_KEY_MAP.values()):
            monkeypatch.delenv(var, raising=False)
        p, _m, _note = pr.resolve("openai", "gpt-4o", api_key="", saved_keys={})
        assert p != "ollama"

    def test_env_key_map_is_the_single_source(self):
        """config.py 曾自带一份映射，漏了 moonshot/qwen 等别名。"""
        for alias in ("moonshot", "qwen", "glm", "gemini", "dashscope"):
            assert alias in pr.ENV_KEY_MAP, f"{alias} 缺少环境变量映射"

    def test_config_reads_key_via_shared_map(self, monkeypatch):
        monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
        cfg = AgentConfig()
        cfg.llm.provider = "moonshot"
        cfg.llm.api_key = ""
        cfg.model_post_init(None)
        assert cfg.llm.api_key == "sk-kimi"


# ── 3. 并发任务隔离 ───────────────────────────────────────────

@pytest.fixture
def base_agent(tmp_path, monkeypatch):
    """一个不连网的 Agent（无 Key → llm 为 None，不影响状态隔离的验证）。"""
    from automind.agent import AutoMindAgent

    for var in set(pr.ENV_KEY_MAP.values()):
        monkeypatch.delenv(var, raising=False)
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.memory.chroma_persist_dir = str(tmp_path / "chroma")
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpt")
    return AutoMindAgent(cfg)


class TestSessionClone:
    """并发任务此前共用全局 agent：模式互相覆盖、上下文串台、token 被清零。"""

    def test_clone_shares_the_expensive_registries(self, base_agent):
        """共享的必须是重且只读的部分，否则克隆就不"轻量"了。"""
        clone = base_agent.clone_for_session()
        assert clone.tool_registry is base_agent.tool_registry
        assert clone.skill_registry is base_agent.skill_registry
        assert clone.memory is base_agent.memory
        assert clone.project_indexer is base_agent.project_indexer

    def test_execution_state_is_not_shared(self, base_agent):
        from automind.core.types import ExecutionMode, InteractionMode, Message, Role

        a = base_agent.clone_for_session()
        b = base_agent.clone_for_session()

        a._interaction = InteractionMode.CHAT
        b._interaction = InteractionMode.LOOP
        a._mode = ExecutionMode.REACT
        b._mode = ExecutionMode.PLAN_AND_EXECUTE
        assert a._interaction is InteractionMode.CHAT, "模式被另一个会话覆盖了"
        assert a._mode is ExecutionMode.REACT

        a.context_mgr.add(Message(role=Role.USER, content="只属于 A 的话"))
        assert b.context_mgr is not a.context_mgr
        b_text = " ".join(m.content for m in b.context_mgr._messages)
        assert "只属于 A 的话" not in b_text, "两个会话的上下文串台了"

        a._current_plan = object()
        assert b._current_plan is None
        assert base_agent._current_plan is None

    def test_clone_does_not_disturb_the_base_agent(self, base_agent):
        from automind.core.types import InteractionMode

        base_agent._interaction = InteractionMode.WORK
        clone = base_agent.clone_for_session()
        clone._interaction = InteractionMode.CODING
        assert base_agent._interaction is InteractionMode.WORK

    @pytest.mark.asyncio
    async def test_closing_a_clone_keeps_shared_resources_alive(self, base_agent):
        """克隆去关共享的 MCP/记忆库，会把还在跑的其它会话一并弄挂。"""
        closed = {"memory": False, "mcp": False}
        base_agent.memory.close = lambda: closed.__setitem__("memory", True)

        async def _disconnect_all():
            closed["mcp"] = True
        base_agent.mcp_registry.disconnect_all = _disconnect_all

        clone = base_agent.clone_for_session()
        await clone.close()
        assert closed == {"memory": False, "mcp": False}


class TestAcquireRunAgent:
    """社区版（无企业版会话池）也必须给每个会话独立实例。"""

    def test_different_sessions_get_different_agents(self, base_agent, monkeypatch):
        import automind.server as srv

        monkeypatch.setattr(srv, "_pool_enabled", lambda: False)
        srv._session_clones.clear()
        try:
            a = srv._acquire_run_agent(base_agent, "sid-a")
            b = srv._acquire_run_agent(base_agent, "sid-b")
            assert a is not b
            assert a is not base_agent, "社区版仍在直接复用全局 agent"
            # 同一会话的多轮复用同一实例（保留该会话的规划/上下文）
            assert srv._acquire_run_agent(base_agent, "sid-a") is a
        finally:
            srv._session_clones.clear()

    def test_clones_are_capped(self, base_agent, monkeypatch):
        """会话数无上限地涨下去 = 内存泄漏。"""
        import automind.server as srv

        monkeypatch.setattr(srv, "_pool_enabled", lambda: False)
        monkeypatch.setattr(srv, "_SESSION_CLONE_MAX", 4)
        srv._session_clones.clear()
        try:
            for i in range(10):
                srv._acquire_run_agent(base_agent, f"sid-{i}")
            assert len(srv._session_clones) <= 4
        finally:
            srv._session_clones.clear()


# ── 4. 切模型只换 LLM，不重建整个 Agent ───────────────────────

class TestSwitchLLM:
    """模式切换此前走 _rebuild_agent：重扫项目、重建 ChromaDB、重注册工具。"""

    def test_registries_survive_a_model_switch(self, base_agent):
        tools, skills = base_agent.tool_registry, base_agent.skill_registry
        memory, indexer = base_agent.memory, base_agent.project_indexer

        cfg = base_agent.config.llm.model_copy(deep=True)
        cfg.provider, cfg.model = "deepseek", "deepseek-chat"
        base_agent.switch_llm(cfg)

        assert base_agent.tool_registry is tools
        assert base_agent.skill_registry is skills
        assert base_agent.memory is memory
        assert base_agent.project_indexer is indexer
        assert base_agent.config.llm.model == "deepseek-chat"

    def test_dependent_modules_follow_the_new_llm(self, base_agent):
        """只换 agent.llm 而不同步规划器 = 界面显示 B、实际还在用 A。"""
        cfg = base_agent.config.llm.model_copy(deep=True)
        cfg.provider, cfg.model, cfg.api_key = "deepseek", "deepseek-chat", "sk-x"
        base_agent.switch_llm(cfg)

        assert base_agent.llm is not None, "带 Key 的切换应初始化成功"
        for holder in ("hierarchical_planner", "plan_executor",
                       "quality_assessor", "reflexion"):
            assert getattr(base_agent, holder).llm is base_agent.llm, (
                f"{holder} 仍指向旧的 LLM")
        assert base_agent.react_executor is None, "ReAct 执行器应被置空以取到新 llm"


class TestSharedTLSContext:
    """新建 LLM 客户端此前要 ~1.15 秒，97% 花在重复解析 CA 证书包上。

    这才是"切一次模型卡两三秒"的真正来源 —— 也是会话克隆能不能做到
    "轻量"的前提。
    """

    def test_context_is_reused(self):
        from automind.core.llm import shared_ssl_context

        first = shared_ssl_context()
        if first is None:
            pytest.skip("本机构建 SSLContext 失败，已按设计退回 httpx 默认行为")
        assert shared_ssl_context() is first, "每次都重建 SSLContext = 每次都慢 1 秒"

    def test_client_carries_the_shared_context(self):
        import ssl

        from automind.core.llm import shared_http_client, shared_ssl_context

        if shared_ssl_context() is None:
            pytest.skip("本机构建 SSLContext 失败")
        client = shared_http_client(timeout=30.0)
        assert client is not None
        # httpx 拿到现成的 SSLContext 就不会再 load_verify_locations
        assert isinstance(shared_ssl_context(), ssl.SSLContext)
        assert client.follow_redirects is True, "跟随跳转的默认值必须与 SDK 自带客户端一致"

    @pytest.mark.asyncio
    async def test_openai_backend_closes_its_client(self):
        """基类的 close() 是 no-op —— 每会话一个客户端时就是持续泄漏。"""
        from automind.core.config import LLMProviderConfig
        from automind.core.llm import LLMBackendFactory

        backend = LLMBackendFactory.create("deepseek", LLMProviderConfig(
            provider="deepseek", model="deepseek-chat", api_key="sk-test"))
        await backend.close()
        assert backend._client.is_closed(), "关不掉 = 连接与文件句柄一直攒着"


# ── 5. async 端点不得在事件循环上做同步 IO ────────────────────

class TestAsyncEndpointsOffloadIO:
    """并发调用时全部排队卡顿 —— 连正在流式输出的 WebSocket 一起卡。"""

    @pytest.mark.parametrize("endpoint", [
        "api_fs_list", "api_preview_file", "api_files_tree",
        "api_files_read", "api_files_write", "api_changes_diff",
    ])
    def test_endpoint_uses_to_thread(self, endpoint):
        import inspect

        import automind.server as srv

        src = inspect.getsource(getattr(srv, endpoint))
        assert "to_thread" in src, f"{endpoint} 仍在事件循环上直接读写磁盘"
        for blocking in ("read_text(", "write_text(", "iterdir("):
            assert blocking not in src, f"{endpoint} 里仍有裸的 {blocking}"
