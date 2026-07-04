"""可观测性与资源清理测试 — logger 降级 / close() 链路 / CLI 版本。"""

import asyncio

from automind.core.logging import _StdlibStructAdapter, configure_logging, get_logger


class TestLoggerFallback:
    def test_get_logger_returns_structured_interface(self):
        log = get_logger("test.obs")
        # 两种实现都必须支持 structlog 风格调用签名
        log.info("event_name", key="value", n=1)
        log.warning("warn_event", reason="demo")
        log.error("err_event")

    def test_stdlib_adapter_formats_kwargs(self):
        import logging
        adapter = _StdlibStructAdapter(logging.getLogger("test.fmt"))
        assert adapter._fmt("evt", {"a": 1, "b": "x"}) == "evt a=1 b='x'"
        assert adapter._fmt("evt", {}) == "evt"

    def test_configure_logging_no_crash(self):
        configure_logging("INFO")
        configure_logging("DEBUG", debug=True)

    def test_bind_returns_self_compatible(self):
        log = get_logger("test.bind")
        bound = log.bind(session="s1")
        bound.info("bound_event")


class TestAgentClose:
    def test_close_idempotent(self):
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        agent = AutoMindAgent(AgentConfig(project_root="."))
        asyncio.run(agent.close())
        asyncio.run(agent.close())  # 幂等：重复调用不抛错

    def test_memory_close_releases_chroma(self, tmp_path):
        from automind.memory.long_term import LongTermMemory

        lt = LongTermMemory(persist_dir=str(tmp_path / "chroma"))
        lt.close()
        assert lt._collection is None
        assert lt._client is None
        assert lt._chroma_available is False
        lt.close()  # 幂等

    def test_memory_manager_close(self, tmp_path):
        from automind.memory.manager import MemoryManager

        m = MemoryManager(persist_dir=str(tmp_path / "chroma"))
        m.close()
        assert m.long_term._collection is None


class TestCliVersion:
    def test_version_from_package(self):
        import automind
        from automind.cli.app import create_parser

        parser = create_parser()
        # --version action 存在且绑定包版本
        version_actions = [a for a in parser._actions
                          if getattr(a, "version", None)]
        assert version_actions
        assert automind.__version__ in version_actions[0].version


class TestRichRepl:
    def test_repl_importable_and_degrades(self):
        from automind.cli.tui import run_rich_repl
        assert asyncio.iscoroutinefunction(run_rich_repl)


class TestExampleSkill:
    def test_word_count_example_runs(self):
        import importlib.util
        from pathlib import Path

        path = (Path(__file__).resolve().parents[2]
                / "examples" / "03-skill-development" / "word_count_skill.py")
        spec = importlib.util.spec_from_file_location("word_count_skill", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        r = asyncio.run(mod.WordCountSkill().execute({"text": "a b c"}))
        assert r.success and r.output["words"] == 3
