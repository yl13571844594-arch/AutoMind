"""静默子进程（v1.11.20）—— 修复 Windows 桌面版"每条命令闪一个空终端窗口"。

## 修的是什么

桌面版是无控制台的 GUI 进程。Windows 上从无控制台进程启动**控制台程序**
（cmd / powershell / pip / git …）时，系统默认给子进程新建一个控制台窗口。
于是用户每执行一步工具就闪一个黑框；环境探活一次要起四五个子进程，像被
弹窗轰炸。修复：``automind.core.proc`` 提供两层兜底 ——

1. ``silent_process_kwargs()``：显式 spawn 点在调用处 ``**`` 展开；
2. ``install_silent_subprocesses()``：入口处包裹 ``Popen.__init__``，
   连第三方库起的子进程也静默。

## 测试要点

* 非 Windows：helper 必须是 no-op、install 必须返回 False（不能引入平台差异）；
* Windows：标志合并、**尊重调用方显式要求的新控制台/脱离进程**、环境变量开关、
  安装幂等且可还原；
* 真实行为：``CREATE_NO_WINDOW`` 起的子进程 ``GetConsoleWindow() == 0``。

注意：本文件会临时改写全局 ``subprocess.Popen.__init__``，每个用例都必须
``try/finally`` 还原（autouse fixture 再兜一层），绝不能把补丁漏给其它测试。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from automind.core import proc

IS_WIN = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _restore_popen() -> None:
    """每个用例前后都保证全局 Popen 处于"未打补丁"状态。"""
    proc.uninstall_silent_subprocesses()
    yield
    proc.uninstall_silent_subprocesses()


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉开关环境变量，避免外部环境干扰判定。"""
    monkeypatch.delenv("AUTOMIND_SILENT_SUBPROCESS", raising=False)


# ═══════════════════════════════════════════════════════════
# 1. 非 Windows：一律 no-op
# ═══════════════════════════════════════════════════════════


@pytest.mark.skipif(IS_WIN, reason="非 Windows 的 no-op 语义只在其它平台验证")
class TestPosixNoop:
    def test_kwargs_do_not_change_anything(self, no_env: None) -> None:
        assert proc.silent_process_kwargs() == {}
        group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        assert proc.silent_process_kwargs(group) == {}

    def test_install_is_a_noop(self, no_env: None) -> None:
        assert proc.install_silent_subprocesses(force=True) is False
        assert proc.silent_subprocesses_installed() is False


# ═══════════════════════════════════════════════════════════
# 2. Windows：标志合并与"显式意图优先"
# ═══════════════════════════════════════════════════════════


@pytest.mark.skipif(not IS_WIN, reason="CREATE_NO_WINDOW 仅 Windows 存在")
class TestWindowsFlags:
    def test_no_window_flag_is_added(self, no_env: None) -> None:
        kwargs = proc.silent_process_kwargs()
        assert kwargs.get("creationflags", 0) & proc.CREATE_NO_WINDOW

    def test_original_flags_are_preserved(self, no_env: None) -> None:
        """CREATE_NEW_PROCESS_GROUP 等原有标志必须保留（超时/取消要杀整棵树）。"""
        group = subprocess.CREATE_NEW_PROCESS_GROUP
        flags = proc.silent_process_kwargs(group)["creationflags"]
        assert flags & group, "CREATE_NEW_PROCESS_GROUP 被覆盖了"
        assert flags & proc.CREATE_NO_WINDOW

    def test_explicit_new_console_is_respected(self, no_env: None) -> None:
        """调用方明确要一个新控制台窗口 → 不得静默化。"""
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        assert proc.silent_process_kwargs(flags) == {"creationflags": flags}

    def test_explicit_detached_process_is_respected(self, no_env: None) -> None:
        """DETACHED_PROCESS = "脱离父进程"（升级器），同样不得改写成静默。"""
        flags = subprocess.DETACHED_PROCESS
        assert proc.silent_process_kwargs(flags) == {"creationflags": flags}

    def test_child_really_has_no_console_window(self, no_env: None) -> None:
        """真行为验证：``CREATE_NO_WINDOW`` 起的子进程没有控制台窗口。"""
        code = ("import ctypes;"
                "print(ctypes.windll.kernel32.GetConsoleWindow())")
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
            **proc.silent_process_kwargs())
        assert done.stdout.strip() == "0", (
            f"子进程仍然挂着控制台窗口：{done.stdout!r}")


# ═══════════════════════════════════════════════════════════
# 3. 开关与自动检测
# ═══════════════════════════════════════════════════════════


@pytest.mark.skipif(not IS_WIN, reason="安装补丁仅 Windows 生效")
class TestInstallDecision:
    def test_env_zero_forces_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTOMIND_SILENT_SUBPROCESS", "0")
        before = subprocess.Popen.__init__
        assert proc.install_silent_subprocesses(force=True) is False
        assert subprocess.Popen.__init__ is before, "强制关闭时仍然改了 Popen"

    def test_env_one_forces_on_and_uninstall_restores(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTOMIND_SILENT_SUBPROCESS", "1")
        before = subprocess.Popen.__init__
        try:
            assert proc.install_silent_subprocesses() is True
            assert proc.silent_subprocesses_installed() is True
            assert subprocess.Popen.__init__ is not before, "没有真的装上补丁"
            # 幂等：再装一次不重复包裹、返回 True
            patched = subprocess.Popen.__init__
            assert proc.install_silent_subprocesses() is True
            assert subprocess.Popen.__init__ is patched
        finally:
            assert proc.uninstall_silent_subprocesses() is True
        assert subprocess.Popen.__init__ is before, "卸载后没有恢复原始 __init__"
        assert proc.silent_subprocesses_installed() is False
        # 再次卸载是 no-op（不抛异常、返回 False）
        assert proc.uninstall_silent_subprocesses() is False

    def test_auto_detection_follows_the_console_probe(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTOMIND_SILENT_SUBPROCESS", raising=False)
        monkeypatch.setattr(proc, "_console_missing", lambda: False)
        assert proc.install_silent_subprocesses() is False, "有控制台时不该改窗口行为"

        monkeypatch.setattr(proc, "_console_missing", lambda: True)
        try:
            assert proc.install_silent_subprocesses() is True
        finally:
            proc.uninstall_silent_subprocesses()


# ═══════════════════════════════════════════════════════════
# 4. 全局补丁的实际效果（用假 Popen 观察 kwargs）
# ═══════════════════════════════════════════════════════════


@pytest.mark.skipif(not IS_WIN, reason="补丁仅 Windows 安装")
class TestGlobalPatch:
    def test_patch_adds_flag_and_respects_explicit_console(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTOMIND_SILENT_SUBPROCESS", "1")
        seen: dict = {}

        class _FakePopen:
            def __init__(self, args, **kwargs):   # noqa: ARG002 - 只关心 kwargs
                seen.clear()
                seen.update(kwargs)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        try:
            assert proc.install_silent_subprocesses() is True
            subprocess.Popen(["x"])
            assert seen.get("creationflags", 0) & proc.CREATE_NO_WINDOW, (
                f"补丁没有给子进程加 CREATE_NO_WINDOW：{seen}")

            subprocess.Popen(["x"], creationflags=subprocess.CREATE_NEW_CONSOLE)
            assert seen["creationflags"] == subprocess.CREATE_NEW_CONSOLE, (
                "显式 CREATE_NEW_CONSOLE 被补丁改写了")
        finally:
            proc.uninstall_silent_subprocesses()
