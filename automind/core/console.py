"""控制台编码自愈 — 让中文输出在任何终端下都不会把进程打崩。

背景
----
Windows 中文版的控制台默认代码页是 936（GBK），Python 的 ``sys.stdout``
会跟着用 ``cp936`` 且 ``errors='strict'``。只要往里写一个 GBK 表示不了的
字符（横幅里的 ``╔``、日志里的 ``✅``、报告里的 emoji），就是一个
``UnicodeEncodeError`` —— 不是显示成乱码，而是**整个进程崩掉**。

受影响的是所有控制台入口，不止启动横幅一处：

* ``python -m automind.server`` 的启动横幅与 uvicorn 日志；
* ``automind <task>`` 的任务执行报告；
* ``automind`` REPL / TUI 的交互界面。

``launch.bat`` 里的 ``chcp 65001`` 只掩盖了这一个入口 —— 照 README 直接敲
命令的用户拿到的仍然是崩溃栈。所以自愈必须做在进程内。

做法
----
1. 若能拿到真实控制台，把输入/输出代码页切到 65001（等价于 ``chcp 65001``），
   并在进程退出时**还原**回原代码页 —— 不给用户的 shell 留下副作用。
2. 把 ``sys.stdout`` / ``sys.stderr`` 重配为 UTF-8 且 ``errors="replace"``。
   即便第 1 步失败（无控制台、权限受限、被重定向），也只会显示成替代字符，
   永远不再抛 ``UnicodeEncodeError``。

调用是幂等的，重复调用无副作用。
"""

from __future__ import annotations

import atexit
import sys
from typing import Any

_UTF8_CODEPAGE = 65001
_applied = False


def _reconfigure_stream(stream: Any) -> None:
    """把单个文本流重配为 UTF-8 / errors=replace（失败静默跳过）。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # 非 TextIOWrapper（如测试里的 StringIO）
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # 流已分离/只读/不支持重配 —— 不值得为它中断启动
        pass


def _switch_windows_codepage() -> bool:
    """把控制台代码页切到 UTF-8，并登记退出时还原。返回是否切换成功。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except Exception:
        return False

    try:
        old_out = int(kernel32.GetConsoleOutputCP())
        old_in = int(kernel32.GetConsoleCP())
    except Exception:
        return False
    if not old_out:  # 0 = 没有附着的控制台（被重定向 / 无窗口进程）
        return False
    if old_out == _UTF8_CODEPAGE and old_in == _UTF8_CODEPAGE:
        return True

    ok = False
    try:
        ok = bool(kernel32.SetConsoleOutputCP(_UTF8_CODEPAGE))
        kernel32.SetConsoleCP(_UTF8_CODEPAGE)
    except Exception:
        return False
    if not ok:
        return False

    def _restore() -> None:
        # 代码页是整个控制台窗口共享的，退出前必须还原，
        # 否则用户回到 cmd 后其它程序的 GBK 输出会变乱码。
        try:
            kernel32.SetConsoleOutputCP(old_out)
            kernel32.SetConsoleCP(old_in)
        except Exception:
            pass

    atexit.register(_restore)
    return True


def enable_utf8_console() -> bool:
    """让当前进程的控制台输出能安全承载中文与符号。

    Returns:
        True 表示控制台已确实处于 UTF-8 代码页（中文可正常显示）；
        False 表示只做了"不崩溃"兜底（非 Windows 控制台，或切换失败，
        此时不可编码字符会显示成替代符号而非抛异常）。
    """
    global _applied
    if _applied:
        return True
    _applied = True

    switched = False
    if sys.platform == "win32":
        switched = _switch_windows_codepage()

    # 无论代码页是否切成功都要重配流：这是"不崩溃"的最后一道保险
    _reconfigure_stream(sys.stdout)
    _reconfigure_stream(sys.stderr)
    _reconfigure_stream(sys.stdin)
    return switched or sys.platform != "win32"
