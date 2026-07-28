"""桌面版 WebView2 缓存自愈回归。

升级后"界面未能加载"的根因是浏览器缓存里留着旧版 HTML（它引用的哈希 JS 已
随新版本删除）。服务端改 no-store 只能防止**今后**再被污染，**已经污染**的
机器必须在升级后主动清一次缓存 —— 即本文件覆盖的逻辑。

清理是删目录操作，因此重点锁两件事：删对了（缓存目录）、没删错（用户态数据
Cookies / Local Storage 必须原样保留，否则用户升级即掉登录态与本地设置）。
"""

import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

main = pytest.importorskip("main", reason="desktop/main.py 不可导入")


def _fake_profile(data_dir: Path) -> Path:
    """造一个 WebView2 用户数据目录（缓存目录 + 必须保留的用户态数据）。"""
    profile = data_dir / "webview" / "EBWebView" / "Default"
    for name in ("Cache", "Code Cache", "GPUCache", "Service Worker"):
        d = profile / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "data_0").write_text("cached", encoding="utf-8")
    (profile / "Local Storage").mkdir(parents=True, exist_ok=True)
    (profile / "Local Storage" / "leveldb").mkdir(exist_ok=True)
    (profile / "Local Storage" / "leveldb" / "000003.log").write_text("x", encoding="utf-8")
    (profile / "Cookies").write_text("cookie-db", encoding="utf-8")
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    return profile


class TestPurgeWebviewCache:
    def test_removes_cache_dirs(self, tmp_path):
        profile = _fake_profile(tmp_path)
        removed = main._purge_webview_cache(str(tmp_path))
        assert removed == 4
        for name in ("Cache", "Code Cache", "GPUCache", "Service Worker"):
            assert not (profile / name).exists()

    def test_keeps_user_state(self, tmp_path):
        """只清缓存 —— 登录态与前端本地设置不能被清掉。"""
        profile = _fake_profile(tmp_path)
        main._purge_webview_cache(str(tmp_path))
        assert (profile / "Cookies").read_text(encoding="utf-8") == "cookie-db"
        assert (profile / "Preferences").exists()
        assert (profile / "Local Storage" / "leveldb" / "000003.log").exists()

    def test_missing_dir_is_noop(self, tmp_path):
        assert main._purge_webview_cache(str(tmp_path / "nope")) == 0


class TestPurgeOnVersionChange:
    def test_purges_once_per_version(self, tmp_path, monkeypatch):
        _fake_profile(tmp_path)
        monkeypatch.setattr(main, "_app_version", lambda: "1.3.1")

        main._purge_cache_on_version_change(str(tmp_path))
        assert not (tmp_path / "webview" / "EBWebView" / "Default" / "Cache").exists()
        assert (tmp_path / "webview_build.txt").read_text(encoding="utf-8") == "1.3.1"

        # 同版本再启动不应重复清理（否则每次启动都丢缓存、首屏变慢）
        _fake_profile(tmp_path)
        main._purge_cache_on_version_change(str(tmp_path))
        assert (tmp_path / "webview" / "EBWebView" / "Default" / "Cache").exists()

    def test_purges_again_after_upgrade(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_app_version", lambda: "1.3.0")
        _fake_profile(tmp_path)
        main._purge_cache_on_version_change(str(tmp_path))

        monkeypatch.setattr(main, "_app_version", lambda: "1.3.1")
        _fake_profile(tmp_path)
        main._purge_cache_on_version_change(str(tmp_path))
        assert not (tmp_path / "webview" / "EBWebView" / "Default" / "Cache").exists()


class TestWebView2Version:
    def test_unknown_version_is_not_treated_as_old(self, monkeypatch):
        """读不到版本号时不能判定过旧 —— 否则非 Windows/精简系统会被误伤。"""
        monkeypatch.setattr(main, "_webview2_version", lambda: "")
        assert main._webview2_too_old() is False

    def test_malformed_version_is_not_treated_as_old(self, monkeypatch):
        monkeypatch.setattr(main, "_webview2_version", lambda: "unknown")
        assert main._webview2_too_old() is False

    @pytest.mark.parametrize("ver,expected", [("83.0.478.37", True), ("120.0.2210.91", False)])
    def test_major_version_threshold(self, monkeypatch, ver, expected):
        monkeypatch.setattr(main, "_webview2_version", lambda: ver)
        assert main._webview2_too_old() is expected
