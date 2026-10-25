"""环境探测必须描述**任务工作区**，而不是 agent 进程碰巧所在的目录（v1.11.18）。

## 修的是什么

``EnvironmentDetector.detect(project_root)`` 里的 pip / git / node / docker 探活
此前都继承**进程 cwd**。在 Web 服务里 cwd 恰好等于项目目录，看不出问题；
但评测 / 工作流 / 会话隔离会先把 ``config.project_root`` 指到临时工作区，
而进程 cwd 仍在仓库根 —— 于是系统提示里同时出现：

    Current Directory: C:\\...\\Temp\\automind-eval-xxx\\smoke_chinese_content\\work
    Git Repository:   C:\\Users\\Administrator\\Desktop\\自动化

模型据此认为"项目和仓库是同一件事"，跑去仓库里翻源码、翻评测断言定义来
确认自己有没有写对文件。实测把"写一个中文文件"这种简单任务烧到 30 万 token
（v1.11.18 全量复跑 smoke_chinese_content 的真实失败形态）。

## 做法

``detect()`` 把 ``project_root`` 解析成绝对路径（不存在时退化为不指定），
所有子进程探活统一带 ``cwd``。这样临时工作区不是 git 仓库时就不会出现
"Git Repository"，环境上下文与工具实际工作目录一致。
"""

from __future__ import annotations

import subprocess

from automind.context.env_detector import EnvironmentDetector


class TestProbesRunInProjectRoot:
    def test_all_probes_use_project_root_as_cwd(self, tmp_path, monkeypatch):
        """detect(tmp) 的每一次子进程调用都必须以 tmp 为 cwd。"""
        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": list(cmd), **kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        info = EnvironmentDetector.detect(tmp_path)

        assert info.cwd == str(tmp_path.resolve())
        assert calls, "detect() 竟然一次子进程都没跑（探活逻辑变了？）"
        expected = str(tmp_path.resolve())
        for call in calls:
            assert call.get("cwd") == expected, (
                f"探活没有在项目根执行：{call['cmd']} → cwd={call.get('cwd')!r}，"
                f"期望 {expected!r}")

    def test_git_root_belongs_to_project_not_process_cwd(self, tmp_path, monkeypatch):
        """git rev-parse 的返回值来自**工作区**；工作区不是仓库时 git_root 为空。"""

        def fake_run(cmd, **kwargs):
            cmd = list(cmd)
            if cmd[:2] == ["git", "rev-parse"]:
                # 工作区不是 git 仓库：即使进程 cwd 在仓库里也必须返回空
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        info = EnvironmentDetector.detect(tmp_path)
        assert info.git_available is True
        assert info.git_root == ""
        assert info.git_branch == ""

    def test_missing_project_root_does_not_crash(self, tmp_path, monkeypatch):
        """目录还不存在时也要能探测（不指定 cwd），不能构造 Agent 就失败。"""
        def fake_run(cmd, **kwargs):
            assert "cwd" not in kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        info = EnvironmentDetector.detect(tmp_path / "not-created-yet")
        assert info.cwd.endswith("not-created-yet")
        assert isinstance(info.pip_available, bool)
