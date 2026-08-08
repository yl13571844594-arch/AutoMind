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
            # 三平台资产全给上（真实 Release 就是这样）：check() 按当前平台挑，
            # 只放 .exe 会让这个用例在 Linux CI 上挑不到资产而失败。
            "assets": [
                {"name": name, "size": 123,
                 "browser_download_url":
                     f"https://github.com/{updater.GITHUB_REPO}/releases/download/{tag}/{name}"}
                for name in (f"AutoMind-Setup-{tag.lstrip('v')}.exe",
                             f"AutoMind-{tag.lstrip('v')}.dmg",
                             f"automind_{tag.lstrip('v')}_amd64.deb")
            ],
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
            # 必须挑到**当前平台**的那个包（Windows→.exe / macOS→.dmg / Linux→.deb）
            import sys as _sys
            expected = {"win32": ".exe", "darwin": ".dmg"}.get(_sys.platform, ".deb")
            assert r["asset_name"].endswith(expected), \
                f"{_sys.platform} 上应挑 {expected}，实得 {r['asset_name']}"
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
            # 一键升级仅 Windows 支持；此处要测的是域名校验，故直接放行平台判断
            monkeypatch.setattr(updater, "can_auto_install", lambda: True)
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


class TestInstallScript:
    """升级批处理的回归护栏 —— 这些点错一个，用户就会"点了升级应用再没回来"。"""

    def _script(self, tmp_path):
        from pathlib import Path
        bat = updater._install_script(
            tmp_path, Path(r"C:\tmp\AutoMind-Setup-9.9.9.exe"),
            r"C:\Program Files\AutoMind\AutoMind.exe", "4242")
        return bat, bat.read_bytes()

    def test_no_stray_carriage_returns(self, tmp_path):
        """必须是干净的 CRLF：write_text 的换行转换会写成 \r\r\n。"""
        _, raw = self._script(tmp_path)
        assert b"\r\r\n" not in raw
        assert raw.count(b"\r\n") == raw.count(b"\n")

    def test_always_relaunches_the_app(self, tmp_path):
        """无论安装成没成，最后都要把应用拉起来 —— 这是"不让用户没得用"的底线。"""
        _, raw = self._script(tmp_path)
        text = raw.decode(errors="replace")
        start = text.index("start ")
        # 拉起应用必须在安装命令之后，且不能被包在任何条件分支里（除了 exist 判断）
        assert start > text.index("/VERYSILENT")
        assert "if exist" in text[text.rindex("\n", 0, start):start]

    def test_silent_failure_retries_visibly(self, tmp_path):
        """装在 Program Files 需要 UAC，/VERYSILENT 下提权界面出不来必然失败。"""
        _, raw = self._script(tmp_path)
        text = raw.decode(errors="replace")
        assert "/VERYSILENT" in text and "/SILENT " in text.replace("/VERYSILENT", "")

    def test_wait_loop_is_bounded_and_console_free(self, tmp_path):
        """等待要有上限；且不能用 timeout（无控制台时它每次都报错、空转烧 CPU）。"""
        _, raw = self._script(tmp_path)
        text = raw.decode(errors="replace")
        assert "GEQ" in text                      # 有次数上限
        assert "ping " in text                    # 用 ping 延时
        assert "timeout /t" not in text

    def test_retry_exit_code_captured_outside_block(self, tmp_path):
        """`(...)` 块整体解析，块内的 %ERRORLEVEL% 展开的是"重试之前"的旧值。

        实测：静默失败 rc=5、可见重试成功 rc=0 时，块内写法记成 5（装成了却
        报失败），块外 RC2 回写才记成 0。
        """
        _, raw = self._script(tmp_path)
        text = raw.decode(errors="replace")
        retry = text.index("/SILENT ") if "/SILENT " in text else text.index(".retry")
        close = text.index(")", retry)          # 重试所在块的右括号
        # 块内不许再出现 set RC=%ERRORLEVEL%（那是失效写法）
        assert "set RC=%ERRORLEVEL%" not in text[retry:close]
        # 块外必须先取 RC2 再有条件回写
        assert "set RC2=%ERRORLEVEL%" in text[close:]
        assert "if not %RC%==0 set RC=%RC2%" in text[close:]

    def test_exit_code_is_recorded_with_space(self, tmp_path):
        """`echo x=%RC%>>f` 里紧挨 > 的数字会被 cmd 当成文件句柄号，必须留空格。"""
        _, raw = self._script(tmp_path)
        assert "exit_code=%RC% >>" in raw.decode(errors="replace")

    def test_spawn_passes_std_handles(self, tmp_path, monkeypatch):
        """真凶回归护栏：分离启动时不显式给标准流，cmd 会因无效句柄立刻夭折。"""
        import subprocess as sp
        seen = {}

        def fake_popen(cmd, **kw):
            seen.update(kw)
            seen["cmd"] = cmd
            return object()

        monkeypatch.setattr(sp, "Popen", fake_popen)
        updater._spawn_installer(tmp_path / "apply_update.bat", tmp_path)

        assert seen["stdin"] == sp.DEVNULL
        # stdout/stderr 必须是真实文件对象（有 fileno），不能是 None
        assert seen["stdout"] is not None and seen["stderr"] is not None
        assert hasattr(seen["stdout"], "fileno")
        # 且确实是分离启动（否则父进程一退，子进程会被一起带走）
        assert seen["creationflags"] & sp.DETACHED_PROCESS


class TestSpawnCrossPlatform:
    """升级流程只在 Windows 走，但代码必须在其它平台可导入可测试。

    回归背景：CREATE_NO_WINDOW / DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP
    都是 Windows 独有的 subprocess 属性。直接引用它们时本机全绿、CI 一到
    ubuntu 就 AttributeError（实测 3.11/3.12 双双失败）。
    """

    def test_no_crash_when_windows_flags_absent(self, tmp_path, monkeypatch):
        import subprocess as sp

        from automind.core import updater

        for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            monkeypatch.delattr(sp, name, raising=False)

        seen = {}

        def fake_popen(cmd, **kw):
            seen.update(kw)
            return object()

        monkeypatch.setattr(sp, "Popen", fake_popen)
        updater._spawn_installer(tmp_path / "apply_update.bat", tmp_path)

        assert seen["creationflags"] == 0, "缺失标志时应退化为 0 而不是报错"
        # 关键点不能因为跨平台适配而丢失：标准流仍必须显式给出
        assert seen["stdin"] is not None
        assert seen["stdout"] is not None
        assert seen["stderr"] is not None

    def test_windows_flags_all_applied_when_present(self, tmp_path, monkeypatch):
        import subprocess as sp

        from automind.core import updater

        # 在非 Windows 上补出这三个常量，保证断言在任何平台都成立
        for name, val in (("CREATE_NO_WINDOW", 0x08000000),
                          ("DETACHED_PROCESS", 0x00000008),
                          ("CREATE_NEW_PROCESS_GROUP", 0x00000200)):
            monkeypatch.setattr(sp, name, getattr(sp, name, val), raising=False)
        want = sp.CREATE_NO_WINDOW | sp.DETACHED_PROCESS | sp.CREATE_NEW_PROCESS_GROUP

        seen = {}
        def fake_popen(cmd, **kw):  # noqa: ARG001 - 签名需与 Popen 一致
            seen.update(kw)
            return object()

        monkeypatch.setattr(sp, "Popen", fake_popen)
        updater._spawn_installer(tmp_path / "apply_update.bat", tmp_path)
        assert seen["creationflags"] == want
