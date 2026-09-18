"""环境探测的健壮性 —— 探活不能把"启动不了"变成"跑挂了"（v1.7.4）。

## 修的是什么

``EnvironmentDetector._check_command`` 抓的是 ``FileNotFoundError`` 与
``TimeoutExpired``，漏掉了同属 ``OSError`` 的 ``PermissionError``：

* Windows 上把**目录**塞进 ``PATH`` 时 ``CreateProcess`` 抛
  ``PermissionError( WinError 5)``；
* 受限令牌（应用容器 / 杀软拦截）同样抛 ``PermissionError``。

而这些调用发生在 ``AutoMindAgent.__init__`` 里 —— 抛出去就是**构造 Agent
失败**：界面白屏、任务全挂，而根因只是"环境里有个 git 探不到"。
探活函数的契约是回答"能不能用"（不能用 = False），不是把异常往上扔。

顺带钉住：探活失败绝不能返回 True —— 那会让上层以为 pip 可用，
然后在真正调用时炸在更远的地方。
"""

from __future__ import annotations

import subprocess

import pytest

from automind.context.env_detector import EnvironmentDetector


class TestCheckCommandToleratesAnyStartupFailure:
    def test_permission_error_on_windows_directory_in_path(self, monkeypatch):
        """PATH 里有目录 → WinError 5。必须回答"不可用"而不是抛异常。"""
        def boom(*_a, **_k):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(subprocess, "run", boom)
        assert EnvironmentDetector._check_command(["pip", "--version"]) is False

    def test_not_a_directory_error(self, monkeypatch):
        def boom(*_a, **_k):
            raise NotADirectoryError(20, "Not a directory")

        monkeypatch.setattr(subprocess, "run", boom)
        assert EnvironmentDetector._check_command(["node", "--version"]) is False

    def test_generic_oserror(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr(subprocess, "run", boom)
        assert EnvironmentDetector._check_command(["docker", "--version"]) is False

    def test_file_not_found_still_false(self, monkeypatch):
        def boom(*_a, **_k):
            raise FileNotFoundError(2, "No such file")

        monkeypatch.setattr(subprocess, "run", boom)
        assert EnvironmentDetector._check_command(["nope"]) is False

    def test_timeout_counts_as_unavailable(self, monkeypatch):
        """5 秒没返回（如首次运行被防病毒扫描）= 现在用不了。"""
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(subprocess, "run", boom)
        assert EnvironmentDetector._check_command(["git", "--version"]) is False

    def test_success_is_true(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda *_a, **_k: subprocess.CompletedProcess([], 0, b"", b""))
        assert EnvironmentDetector._check_command(["python", "--version"]) is True


class TestDetectSurvivesABrokenEnvironment:
    def test_detect_does_not_raise_when_no_command_can_start(self, monkeypatch):
        """整个环境里一个命令都起不来时，detect() 仍要给出结果。"""
        def boom(*_a, **_k):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(subprocess, "run", boom)
        info = EnvironmentDetector.detect(".")

        assert info.pip_available is False
        assert info.git_available is False
        assert info.node_available is False
        assert info.docker_available is False
        # 基本信息不依赖子进程，必须照常有值
        assert info.python_executable
        assert info.os_name


@pytest.mark.parametrize("attr", ["pip_available", "git_available",
                                  "node_available", "docker_available"])
def test_flags_are_real_booleans(monkeypatch, attr):
    """必须是真 bool —— 界面/评测按布尔渲染，"未知"不该伪装成可用。"""
    def boom(*_a, **_k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(subprocess, "run", boom)
    info = EnvironmentDetector.detect(".")
    assert isinstance(getattr(info, attr), bool)
