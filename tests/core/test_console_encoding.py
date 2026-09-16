"""Windows 中文环境下日志乱码的根治（v1.7.2）。

## 修的是什么

Windows 上 Python 的 stdio 编码取自**当前代码页**（简中环境 = cp936/GBK）。
只要输出不是"真正的控制台"——被 IDE、CI、启动器、管道、桌面壳接管 ——
Python 写出去的就是 GBK 字节，而接收方几乎总是按 UTF-8 解码：中文日志、
中文报错整片变成乱码。

实测复现（本机）：

    > python -c "import sys; print(sys.stdout.encoding)"
    gbk                      # ← 即便输出被管道接走，仍然是本地代码页

当时的临时解法是让用户自己设 ``PYTHONIOENCODING=utf-8``；用户不该为了
看懂自己的日志去配环境变量。

## 边界（这个文件同时钉住"不许越界"的部分）

* 只处理 Windows，且只在当前编码确实是本地代码页时动手；
* 用户显式设了 ``PYTHONIOENCODING`` / ``PYTHONUTF8`` → **完全不动**；
* 真控制台（isatty）不改编码 —— 它按代码页渲染中文本来就是对的，
  改编码反而会把它弄花；只把 ``errors`` 收紧为 replace。
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - 端到端验证必须真的起一个子进程
import sys
from pathlib import Path

import pytest

from automind.core import logging as alog

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(os.name != "nt", reason="编码问题只在 Windows 上存在")


class _Stream:
    """可观察的假 stdout/stderr。"""

    def __init__(self, encoding: str, tty: bool = False,
                 reconfigurable: bool = True) -> None:
        self.encoding = encoding
        self._tty = tty
        self.calls: list[dict] = []
        if reconfigurable:
            self.reconfigure = self._reconfigure       # type: ignore[assignment]

    def _reconfigure(self, **kw) -> None:
        self.calls.append(kw)
        if kw.get("encoding"):
            self.encoding = kw["encoding"]

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从"没有任何显式设置、解释器也不在 UTF-8 模式"开始。

    最后一行的钉死是必须的：GitHub Actions 给 runner 设了 ``PYTHONUTF8=1``
    （见 desktop-build.yml 里那句注释），于是同一份测试在 CI 上走进的是
    「跳过」分支、在本地走进的是「修复」分支 —— 这种随环境漂移的用例比没有
    还糟（本地绿 CI 红，或反过来）。``sys.flags`` 是只读的，所以把判据抽成
    ``_interpreter_is_utf8()`` 再替换它。
    """
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("AUTOMIND_UTF8_STDIO", raising=False)
    monkeypatch.setattr(alog, "_STDIO_CHECKED", False, raising=False)
    monkeypatch.setattr(alog, "_interpreter_is_utf8", lambda: False)


@pytest.fixture
def fake_stdio():
    """一对可观察的假流。**刻意不在夹具里绑定 sys** —— 见 ``bind_stdio``。"""
    return _Stream("gbk"), _Stream("gbk")


@pytest.fixture
def bind_stdio(monkeypatch):
    """把假流接到 ``sys`` 上，返回 ``(out, err)``。

    为什么必须在这里接、而不是在夹具里接：pytest 的全局捕获会在**调用阶段的
    开始**用自己的对象替换 ``sys.stdout`` / ``sys.stderr``，而夹具在那之前就
    执行完了 —— 夹具里 patch 的对象会被覆盖掉，测试于是在"看着假流、
    实际操作捕获对象"的状态下跑（本文件第一版就栽在这里：单跑通过、
    与其它文件一起跑就失败，因为捕获开关不同）。
    """
    def _bind(out, err=None):
        err = err or out
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        return out, err
    return _bind


# ═══════════════════════════════════════════════════════════
# 1. 该修的必须修
# ═══════════════════════════════════════════════════════════


def test_piped_stdio_gets_switched_to_utf8(fake_stdio, bind_stdio):
    out, err = bind_stdio(*fake_stdio)

    result = alog.ensure_utf8_stdio(force=True)

    assert out.encoding == "utf-8" and err.encoding == "utf-8"
    assert result["stdout"].startswith("utf8")
    assert {"encoding": "utf-8", "errors": "replace"} in out.calls, \
        "errors=replace 是兜底：编码不了时降级成一个替代字符，而不是抛异常"


def test_errors_are_never_left_on_strict(fake_stdio, bind_stdio):
    """一条日志不该因为一个字符编码不了，就换来一屏 UnicodeEncodeError 堆栈。"""
    out, _ = bind_stdio(*fake_stdio)

    alog.ensure_utf8_stdio(force=True)

    assert out.calls and all(c.get("errors") == "replace" for c in out.calls)


# ═══════════════════════════════════════════════════════════
# 2. 不该动的绝不能动
# ═══════════════════════════════════════════════════════════


def test_real_console_keeps_its_own_encoding(fake_stdio, bind_stdio):
    """真控制台按 GBK 渲染中文本来就是正常的，改编码反而会弄花。"""
    out, err = bind_stdio(*fake_stdio)
    out._tty = err._tty = True

    result = alog.ensure_utf8_stdio(force=True)

    assert out.encoding == "gbk", "真控制台的编码必须原样保留"
    assert out.calls == [{"errors": "replace"}]
    assert result["stdout"].startswith("tty-kept")


def test_explicit_pythonioencoding_wins(monkeypatch, fake_stdio, bind_stdio):
    out, _ = bind_stdio(*fake_stdio)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

    result = alog.ensure_utf8_stdio(force=True)

    assert out.calls == [], "用户/上层明确指定了编码，不许覆盖"
    assert "skipped" in result


def test_opt_out_env_var_is_honoured(monkeypatch, fake_stdio, bind_stdio):
    out, _ = bind_stdio(*fake_stdio)
    monkeypatch.setenv("AUTOMIND_UTF8_STDIO", "0")

    assert "skipped" in alog.ensure_utf8_stdio(force=True)
    assert out.calls == []


def test_already_utf8_is_left_alone(fake_stdio, bind_stdio):
    out, _ = bind_stdio(*fake_stdio)
    out.encoding = "utf-8"

    result = alog.ensure_utf8_stdio(force=True)

    assert out.calls == []
    assert result["stdout"] == "already-utf8"


def test_non_windows_is_skipped(monkeypatch, fake_stdio, bind_stdio):
    out, _ = bind_stdio(*fake_stdio)
    monkeypatch.setattr(alog.os, "name", "posix")

    assert "skipped" in alog.ensure_utf8_stdio(force=True)
    assert out.calls == []


def test_utf8_mode_interpreter_is_left_alone(monkeypatch, fake_stdio, bind_stdio):
    """``-X utf8`` / ``PYTHONUTF8=1`` 下无事可做 —— 这条正是 CI 上走的分支。"""
    out, _ = bind_stdio(*fake_stdio)
    monkeypatch.setattr(alog, "_interpreter_is_utf8", lambda: True)

    result = alog.ensure_utf8_stdio(force=True)

    assert "skipped" in result
    assert out.calls == []


def test_streams_without_reconfigure_do_not_crash(monkeypatch):
    """被上层替换过的流（StringIO / 桌面壳包装）没有 reconfigure —— 不能因此崩。"""
    monkeypatch.setattr(sys, "stdout", _Stream("gbk", reconfigurable=False))
    monkeypatch.setattr(sys, "stderr", _Stream("gbk", reconfigurable=False))

    result = alog.ensure_utf8_stdio(force=True)

    assert result["stdout"] == "not-reconfigurable"


def test_none_stream_is_tolerated(monkeypatch):
    """pythonw / 冻结包里 sys.stdout 可能是 None。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    assert alog.ensure_utf8_stdio(force=True)["stdout"] == "none"


def test_reconfigure_failure_does_not_take_the_process_down(fake_stdio, bind_stdio):
    out, _ = bind_stdio(*fake_stdio)

    def boom(**kw):
        raise OSError("句柄已关闭")

    out.reconfigure = boom                           # type: ignore[assignment]

    result = alog.ensure_utf8_stdio(force=True)

    assert result["stdout"].startswith("failed:"), \
        "乱码远好过启动失败：改不动就如实记下来"


# ═══════════════════════════════════════════════════════════
# 3. 接线：不用每个入口各记一次
# ═══════════════════════════════════════════════════════════


def test_get_logger_triggers_the_check(fake_stdio, bind_stdio):
    """25+ 个模块在导入期就调 get_logger —— 等价于"本库被使用的第一现场"。"""
    out, _ = bind_stdio(*fake_stdio)
    assert alog._STDIO_CHECKED is False

    alog.get_logger("automind.test")

    assert alog._STDIO_CHECKED is True
    assert out.encoding == "utf-8"


def test_check_runs_only_once(fake_stdio, bind_stdio):
    out, _ = bind_stdio(*fake_stdio)
    alog.ensure_utf8_stdio()
    out.encoding = "gbk"                             # 假装又被改回去了

    alog.ensure_utf8_stdio()                         # 第二次应当是空操作

    assert out.encoding == "gbk"
    assert alog.ensure_utf8_stdio(force=True)["stdout"].startswith("utf8")


# ═══════════════════════════════════════════════════════════
# 4. 端到端：真起一个进程，中文必须能按 UTF-8 读懂
# ═══════════════════════════════════════════════════════════


def test_real_process_output_is_utf8_after_touching_our_logging():
    """复现用户的处境：Windows 简中 + stdout 被管道接管（编码 = 本地代码页）。

    只断言"修好之后"的结果 —— 从不设置 PYTHONIOENCODING，让子进程天然处于
    用户遇到的那个状态（本机实测为 gbk），再看它写出来的字节能不能按 UTF-8 读。
    """
    code = (
        "from automind.core.logging import get_logger\n"
        "get_logger('automind.e2e')\n"
        "print('中文日志不再乱码')\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           cwd=str(ROOT), env=env, timeout=120)
    except PermissionError:                # pragma: no cover - 受限沙箱
        pytest.skip("当前环境禁止创建子进程管道，无法做端到端验证")

    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")

    text = p.stdout.decode("utf-8")        # ← 关键：按 UTF-8 解必须干净
    assert "中文日志不再乱码" in text
    assert "\ufffd" not in text, "输出里出现了替换字符，说明字节不是 UTF-8"
