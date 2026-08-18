"""浏览器二进制缺失时必须自动回退，而不是把英文报错甩给用户。

Playwright 的 Python 包不含浏览器本体，要另跑 `playwright install` 下载
约 150MB 的 Chromium。没下载时用户拿到的是一大段英文框：

    BrowserType.launch: Executable doesn't exist at ...
    ║     playwright install                                     ║

桌面版尤其难办 —— 冻结包里没有可用的 `playwright` 命令行，照着做也做不了。
现在改为回退到系统已装的 Edge / Chrome（Windows 上 Edge 是系统组件），
零下载即可用；三者都没有时才报错，且给中文说明。
"""

from __future__ import annotations

import pytest

from automind.tools import browser as browser_mod

pytest.importorskip("playwright")


class TestMissingExecutableDetection:
    @pytest.mark.parametrize("msg", [
        "BrowserType.launch: Executable doesn't exist at /x/chrome-headless-shell",
        "Please run the following command to download new browsers: playwright install",
        "Chromium distribution 'msedge' is not found at /opt/microsoft/msedge",
    ])
    def test_recognises_missing_binary(self, msg):
        assert browser_mod._is_missing_executable(Exception(msg))

    @pytest.mark.parametrize("msg", [
        "Timeout 30000ms exceeded",
        "net::ERR_CONNECTION_REFUSED",
        "Permission denied",
    ])
    def test_other_failures_are_not_treated_as_missing(self, msg):
        """启动本身出错（权限/沙箱/网络）不该被当成"换个浏览器再试"。"""
        assert not browser_mod._is_missing_executable(Exception(msg))


class TestLaunchCandidates:
    def test_bundled_chromium_is_tried_first(self):
        cands = browser_mod._launch_candidates()
        assert cands[0] == ("bundled", {}), "应优先用 playwright install 下载的 Chromium"

    def test_system_channels_follow_as_fallback(self):
        cands = browser_mod._launch_candidates()
        sources = [s for s, _ in cands]
        assert "msedge" in sources, "Windows 上 Edge 是系统自带，必须作为回退项"
        for source, kwargs in cands[1:]:
            assert kwargs == {"channel": source}


class TestFallbackBehaviour:
    async def test_falls_back_to_system_browser(self, tmp_path, monkeypatch):
        """把 Playwright 下载目录指向空目录 → 仍应能靠系统浏览器跑起来。"""
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        tool = browser_mod.BrowserTool()
        try:
            r = await tool.execute(action="navigate", url="https://example.com")
        finally:
            await tool._cleanup()
        if not r.success and "系统已安装的 Edge / Chrome" in (r.error or ""):
            pytest.skip("本机没有任何可用浏览器，无从验证回退")
        assert r.success, r.error
        assert tool.browser_source != "bundled", "本应回退到系统浏览器"

    async def test_message_is_chinese_and_actionable_when_nothing_available(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        monkeypatch.setattr(browser_mod, "SYSTEM_CHANNELS", ())   # 假装系统里也没有
        tool = browser_mod.BrowserTool()
        try:
            r = await tool.execute(action="navigate", url="https://example.com")
        finally:
            await tool._cleanup()
        assert not r.success
        err = r.error or ""
        assert "浏览器自动化不可用" in err, "错误信息必须是中文的"
        assert "playwright install chromium" in err, "要给出可照抄的安装命令"

    async def test_status_probe_reports_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        st = await browser_mod.browser_status()
        assert st["sdk"] is True
        if st["ready"]:
            assert st["source"], "就绪时必须说明用的是哪个浏览器"
        else:
            assert "playwright install" in st["detail"]
