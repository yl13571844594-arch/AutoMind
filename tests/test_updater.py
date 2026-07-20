"""自动更新模块测试 — 版本比较 / 资产匹配 / 检查缓存 / 非桌面模式拒绝。"""

from __future__ import annotations

import io
import json

from automind import __version__
from automind.core import updater


class TestVersionCompare:
    def test_newer(self):
        assert updater.is_newer("99.0.0")
        assert updater.is_newer("1.2.1", "1.2.0")
        assert updater.is_newer("1.10.0", "1.9.9")

    def test_not_newer(self):
        assert not updater.is_newer("0.0.1")
        assert not updater.is_newer(__version__)
        assert not updater.is_newer("1.2.0", "1.2.0")
        assert not updater.is_newer("v1.0.0", "1.2.0")

    def test_malformed(self):
        assert not updater.is_newer("abc", "1.0.0")


class TestAssetPattern:
    def test_match(self):
        assert updater._ASSET_RE.match("AutoMind-Setup-1.2.0.exe")
        assert not updater._ASSET_RE.match("AutoMind-Setup-1.2.0.exe.sig")
        assert not updater._ASSET_RE.match("automind_agent-1.2.0.tar.gz")
        assert not updater._ASSET_RE.match("Evil-AutoMind-Setup-1.2.0.exe")


class TestCheck:
    def _mock_release(self, monkeypatch, tag: str):
        payload = json.dumps({
            "tag_name": tag,
            "body": "更新说明",
            "html_url": f"https://github.com/{updater.GITHUB_REPO}/releases/{tag}",
            "assets": [{
                "name": f"AutoMind-Setup-{tag.lstrip('v')}.exe",
                "size": 123,
                "browser_download_url":
                    f"https://github.com/{updater.GITHUB_REPO}/releases/download/"
                    f"{tag}/AutoMind-Setup-{tag.lstrip('v')}.exe",
            }],
        }).encode()

        class _Resp(io.BytesIO):
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(updater, "_open",
                            lambda *_a, **_k: _Resp(payload))

    def test_check_newer(self, tmp_path, monkeypatch):
        from automind.core import db as db_mod
        db_mod.reset_for_tests(tmp_path / "t.db")
        try:
            self._mock_release(monkeypatch, "v99.0.0")
            r = updater.check(force=True)
            assert r["available"] and r["latest"] == "99.0.0"
            assert r["asset_url"].startswith("https://github.com/")
            assert r["current"] == __version__
            # 二次调用命中缓存（无需再 mock 网络）
            monkeypatch.setattr(updater, "_open",
                                lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError))
            r2 = updater.check()
            assert r2["cached"] and r2["latest"] == "99.0.0"
        finally:
            db_mod.reset_for_tests(None)

    def test_check_up_to_date(self, tmp_path, monkeypatch):
        from automind.core import db as db_mod
        db_mod.reset_for_tests(tmp_path / "t.db")
        try:
            self._mock_release(monkeypatch, "v0.0.1")
            r = updater.check(force=True)
            assert not r["available"]
        finally:
            db_mod.reset_for_tests(None)

    def test_check_network_error(self, tmp_path, monkeypatch):
        from automind.core import db as db_mod
        db_mod.reset_for_tests(tmp_path / "t.db")
        try:
            monkeypatch.setattr(updater, "_open",
                                lambda *_a, **_k: (_ for _ in ()).throw(OSError("net down")))
            r = updater.check(force=True)
            assert not r["available"] and "检查失败" in r["error"]
        finally:
            db_mod.reset_for_tests(None)


class TestApply:
    def test_refuses_when_not_frozen(self):
        r = updater.apply_update()
        assert "pip install" in r["error"]

    def test_rejects_untrusted_host(self, tmp_path, monkeypatch):
        from automind.core import db as db_mod
        db_mod.reset_for_tests(tmp_path / "t.db")
        try:
            monkeypatch.setattr(updater, "_is_frozen", lambda: True)
            monkeypatch.setattr(updater, "check", lambda *_a, **_k: {
                "available": True, "latest": "99.0.0",
                "asset_url": "https://evil.example.com/AutoMind-Setup-99.0.0.exe",
            })
            r = updater.apply_update()
            assert "不受信任" in r["error"]
        finally:
            db_mod.reset_for_tests(None)


class TestServerRoutes:
    def test_update_routes_registered(self):
        import automind.server as server
        paths = {getattr(r, "path", "") for r in server.app.routes}
        assert {"/api/update/check", "/api/update/apply",
                "/api/update/state"} <= paths
