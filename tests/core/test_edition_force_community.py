"""``AUTOMIND_FORCE_COMMUNITY`` 强制社区版模式（v1.11.19）。

为什么需要这个开关：本机若装了商业扩展包并持有有效许可证（``~/.automind/license``），
社区版门控类用例会大面积失败 —— 那不是代码回归，而是环境效应，却会让发布验收的
结论不可信。验收脚本（``scripts/acceptance.py``）用它把「社区版结论」跑成可重复的。

本文件守住四条底线：

1. 开关为真值时**跳过商业扩展加载** —— 连 ``import automind_pro`` 都不应发生；
2. 状态稳定在社区版：``edition=community``、``feature_flags()`` 全 False、无许可证摘要；
3. **只降不升**：即使此前已是专业版，打开开关后也会收敛回社区版；
4. 关掉开关后恢复原有语义（商业包照常加载、特性照常注册），不改变 ``AUTOMIND_LICENSE``
   等既有行为。全程用 monkeypatch，绝不写磁盘、不污染其它用例。
"""

from __future__ import annotations

import importlib
import types

import pytest

from automind.core import edition


@pytest.fixture(autouse=True)
def _isolated_edition(monkeypatch):
    """每个用例独立的版本状态；默认既无许可证、也不开强制社区版。"""
    monkeypatch.delenv("AUTOMIND_LICENSE", raising=False)
    monkeypatch.delenv(edition.FORCE_COMMUNITY_ENV, raising=False)
    edition.reset_for_tests()
    yield
    edition.reset_for_tests()


def _fake_extension_module(imported: list[str]) -> types.SimpleNamespace:
    """构造一个"能激活专业版"的假扩展包，用来证明加载路径真的被走到。"""

    def activate(extension_api) -> str:
        extension_api.register_feature("multi_agent", object())
        extension_api.set_edition("pro", {"tier": "pro", "customer": "unit-test"})
        return "pro"

    return types.SimpleNamespace(
        EXTENSION_API_VERSION=edition.EXTENSION_API_VERSION,
        activate=activate,
        imported=imported,
    )


def _patch_imports(monkeypatch, fake) -> None:
    """让 ``automind_pro`` 的 import 返回 fake；其它模块真实导入（不存在则 ImportError）。"""

    def _import_module(name, *args, **kwargs):
        fake.imported.append(name)
        if name == "automind_pro":
            return fake
        raise ImportError(f"测试中不访问 {name}（保持用例环境纯净）")

    monkeypatch.setattr(importlib, "import_module", _import_module)
    monkeypatch.setattr(edition, "_add_extensions_path", lambda: None)
    # _start_license_watchdog 会 try import automind_pro.licensing.guard；上面的
    # ImportError 分支本可兜住，这里显式替换以彻底避免真实包被加载。
    monkeypatch.setattr(edition, "_start_license_watchdog", lambda: False)


class TestForceCommunityEnabled:
    """真值解析：1/true/yes/on（大小写不敏感），其余一律不算。"""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "ON", "  1  "])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, value)
        assert edition.force_community_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "2", "enable"])
    def test_falsey_values(self, monkeypatch, value):
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, value)
        assert edition.force_community_enabled() is False

    def test_absent_by_default(self):
        assert edition.force_community_enabled() is False


class TestForcedCommunityBehaviour:
    def test_skips_commercial_import_even_if_pro_available(self, monkeypatch):
        imported: list[str] = []
        _patch_imports(monkeypatch, _fake_extension_module(imported))
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, "1")

        assert edition.load_extensions(force=True) == edition.EDITION_COMMUNITY
        assert "automind_pro" not in imported, (
            "AUTOMIND_FORCE_COMMUNITY=1 时不应 import automind_pro（商业扩展必须被跳过）")

    def test_state_stays_community(self, monkeypatch):
        _patch_imports(monkeypatch, _fake_extension_module([]))
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, "1")

        assert edition.get_edition() == edition.EDITION_COMMUNITY
        flags = edition.feature_flags()
        assert flags and not any(flags.values()), "强制社区版下全部商业特性开关必须为 False"
        assert edition.license_info() is None
        assert edition._state["loaded"] is True

    def test_only_downgrades_never_upgrades(self, monkeypatch):
        """此前已激活专业版：打开开关后必须收敛回社区版（只降不升）。"""
        _patch_imports(monkeypatch, _fake_extension_module([]))
        # 先按正常语义激活专业版
        assert edition.load_extensions(force=True) == edition.EDITION_PRO
        assert edition.has_feature("multi_agent")
        # 再打开开关并强制重载
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, "1")
        assert edition.load_extensions(force=True) == edition.EDITION_COMMUNITY
        assert not edition.has_feature("multi_agent")
        assert edition.license_info() is None
        assert not any(edition.feature_flags().values())

    def test_no_feature_can_be_gained_while_forced(self, monkeypatch):
        """开关本身不提供任何商业能力：feature_flags 全 False 且 require_feature 抛错。"""
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, "1")
        _patch_imports(monkeypatch, _fake_extension_module([]))
        edition.load_extensions(force=True)
        with pytest.raises(edition.FeatureNotAvailable):
            edition.require_feature("multi_agent")


class TestRecoveryAfterUnset:
    def test_original_semantics_restored_when_env_removed(self, monkeypatch):
        """关掉开关（并重置缓存）后，商业扩展照常加载 —— 不改变既有语义。"""
        _patch_imports(monkeypatch, _fake_extension_module([]))
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, "1")
        assert edition.get_edition() == edition.EDITION_COMMUNITY

        monkeypatch.delenv(edition.FORCE_COMMUNITY_ENV)
        edition.reset_for_tests()
        assert edition.get_edition() == edition.EDITION_PRO
        assert edition.has_feature("multi_agent")
        assert edition.license_info() == {"tier": "pro", "customer": "unit-test"}

    def test_reset_clears_loaded_cache(self, monkeypatch):
        """reset_for_tests 清掉 loaded 缓存，环境变化能被重新求值。"""
        monkeypatch.setenv(edition.FORCE_COMMUNITY_ENV, "1")
        _patch_imports(monkeypatch, _fake_extension_module([]))
        assert edition.load_extensions(force=True) == edition.EDITION_COMMUNITY
        edition.reset_for_tests()
        assert edition._state["loaded"] is False
        monkeypatch.delenv(edition.FORCE_COMMUNITY_ENV)
        assert edition.get_edition() == edition.EDITION_PRO
