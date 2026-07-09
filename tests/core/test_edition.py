"""版本（Edition）机制测试 — 社区版门控与扩展协议。"""

import pytest

from automind.core import edition


@pytest.fixture(autouse=True)
def _isolated_edition(monkeypatch):
    """每个用例独立的版本状态；默认无许可证 → 社区版。"""
    monkeypatch.delenv("AUTOMIND_LICENSE", raising=False)
    edition.reset_for_tests()
    yield
    edition.reset_for_tests()


class TestCommunityDefaults:
    def test_default_edition_without_license(self):
        assert edition.get_edition() == edition.EDITION_COMMUNITY

    def test_all_commercial_features_off(self):
        flags = edition.feature_flags()
        assert set(flags) == set(edition.COMMERCIAL_FEATURES)
        assert not any(flags.values())

    def test_require_feature_raises_with_hint(self):
        with pytest.raises(edition.FeatureNotAvailable) as ei:
            edition.require_feature("multi_agent")
        assert "专业版" in str(ei.value)
        assert ei.value.feature == "multi_agent"

    def test_session_pool_hint_mentions_enterprise(self):
        assert "企业版" in edition.upgrade_hint("session_pool")

    def test_license_info_none(self):
        assert edition.license_info() is None


class TestExtensionAPI:
    def test_register_and_query(self):
        api = edition.ExtensionAPI()
        impl = object()
        api.register_feature("scheduler", impl)
        api.set_edition("pro", license={"tier": "pro"})
        edition._state["loaded"] = True  # 阻止 load_extensions 覆盖
        assert edition.get_feature("scheduler") is impl
        assert edition.has_feature("scheduler")
        assert edition.get_edition() == "pro"
        assert edition.license_info() == {"tier": "pro"}

    def test_set_edition_rejects_bogus_name(self):
        with pytest.raises(ValueError):
            edition.ExtensionAPI().set_edition("ultimate")

    def test_api_version_constant(self):
        assert edition.ExtensionAPI.api_version == edition.EXTENSION_API_VERSION == 1


class TestAgentGating:
    """社区版 Agent 调用商业模式应抛 FeatureNotAvailable（不触碰 LLM）。"""

    @pytest.mark.asyncio
    async def test_run_multi_blocked(self):
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        agent = AutoMindAgent(AgentConfig())
        with pytest.raises(edition.FeatureNotAvailable):
            await agent.run_multi("任务")

    @pytest.mark.asyncio
    async def test_run_loop_blocked(self):
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        agent = AutoMindAgent(AgentConfig())
        with pytest.raises(edition.FeatureNotAvailable):
            await agent.run_loop("任务")
