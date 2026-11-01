"""静默子进程 —— 修复 Windows 上"每跑一条命令就闪一个黑框"的观感问题。

## 问题

桌面版是无控制台的 GUI 进程（``console=False`` / pythonw）。Windows 上从
**没有控制台**的进程启动一个控制台程序（``cmd.exe`` / ``powershell`` /
``pip`` / ``git`` …）时，系统默认会给子进程**新建一个控制台窗口** —— 除非
调用方显式传 ``CREATE_NO_WINDOW``。于是用户每执行一步工具，屏幕上就闪一个
空的黑框；而环境探活（``pip --version`` / ``git --version`` / ``node --version``）
一次任务就要起好几个，观感像被弹窗轰炸。任务跑得越久，闪得越多。

## 做法（两层，互为兜底）

1. **全局补丁**（:func:`install_silent_subprocesses`）：桌面版与 CLI 入口在
   启动最早期把它装上，包裹 ``subprocess.Popen.__init__``，给每个**未显式
   指定控制台语义**的 spawn OR 进 ``CREATE_NO_WINDOW``。这样连第三方库起的
   子进程（uvicorn reloader / 下载器 / 更新器）也一起静默。
2. **显式 spawn 点**（:func:`silent_process_kwargs`）：本包的工具在调用处直接
   ``**silent_process_kwargs()``。即便全局补丁没启用（库调用方、或服务器进程
   本身就带控制台），也不会闪窗。

## 边界（不做什么）

* 只加 ``CREATE_NO_WINDOW``，**不设** ``startupinfo`` / ``SW_HIDE``：那会把
  用户**主动**打开的 GUI 程序一起藏起来（例如让模型打开一个编辑器，用户却
  什么也看不到）。
* 调用方显式要求 ``CREATE_NEW_CONSOLE`` / ``DETACHED_PROCESS`` 时一律不动 ——
  那是"我就是要一个独立窗口 / 要脱离父进程"的明确意图（升级器就是这么用的）。
* 开关 ``AUTOMIND_SILENT_SUBPROCESS``：``0`` 强制关、``1`` 强制开；未设置时
  只在**检测到没有控制台**时才启用（有控制台的命令行用户不需要，也不该被改
  掉子进程的窗口行为，否则排障时反而少了一个可见信号）。
* 全程不抛异常：安装失败只记一条 warning 并返回 False —— 静默化是体验优化，
  绝不能因为它让应用起不来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.core.proc")

#: 是否运行在 Windows 上（POSIX 上这些创建标志不存在，静默化无从谈起）
IS_WINDOWS = sys.platform == "win32"

#: 创建进程时不显示控制台窗口的标志（POSIX 上不存在 → 0，表示"不加任何标志"）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 调用方**显式**表达的"独立控制台 / 脱离父进程"语义 —— 遇到它们绝不改写
_EXPLICIT_CONSOLE_FLAGS = (
    getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    | getattr(subprocess, "DETACHED_PROCESS", 0)
)

#: 原始 ``Popen.__init__``（安装补丁前保存，卸载时还原）
_original_popen_init: Any = None
#: 补丁是否已安装（幂等标记）
_installed = False


def silent_process_kwargs(creationflags: int = 0) -> dict[str, int]:
    """给显式 spawn 点用的"静默参数"。

    用法::

        await asyncio.create_subprocess_shell(cmd, **silent_process_kwargs())
        subprocess.run([...], capture_output=True, **silent_process_kwargs())

    Args:
        creationflags: 调用方原本要传的创建标志（例如
            ``CREATE_NEW_PROCESS_GROUP``）。**原有标志必须保留** —— 它们
            往往承担着别的语义（进程组 = 超时/取消时能杀整棵树）。

    Returns:
        可直接 ``**`` 展开进 ``Popen`` / ``create_subprocess_*`` 的关键字参数。
        * 非 Windows：``{}``（不改变任何行为）；
        * 已含 ``CREATE_NEW_CONSOLE`` / ``DETACHED_PROCESS``：原样透传，不强行
          加静默标志（调用方明确要窗口/要脱离，见模块注释）；
        * 其余：``{"creationflags": creationflags | CREATE_NO_WINDOW}``。
    """
    if not IS_WINDOWS:
        return {}
    flags = int(creationflags or 0)
    if flags and (flags & _EXPLICIT_CONSOLE_FLAGS):
        # 显式语义优先：透传原 flags，不叠加 CREATE_NO_WINDOW
        return {"creationflags": flags}
    if not CREATE_NO_WINDOW:                    # pragma: no cover - 仅异常环境
        return {"creationflags": flags} if flags else {}
    return {"creationflags": flags | CREATE_NO_WINDOW}


def _console_missing() -> bool:
    """当前进程是否**没有**控制台（GUI 启动形态）。

    ``sys.stdout is None`` 是冻结 GUI 程序的典型形态（无控制台时标准流为空）；
    若标准流被兜底桩替换过，再问一次 Win32：``GetConsoleWindow() == 0`` 表示
    没有关联的控制台窗口。查询失败一律当作"有控制台"（保守：不启用补丁）。
    """
    if sys.stdout is None or sys.stderr is None:
        return True
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleWindow()) == 0
    except Exception:                           # pragma: no cover - 查询失败按有控制台
        return False


def _silent_enabled(force: bool | None) -> bool:
    """是否应该启用静默化（环境变量 > 显式 force > 自动检测无控制台）。"""
    raw = str(os.environ.get("AUTOMIND_SILENT_SUBPROCESS", "") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    if force:
        return True
    return _console_missing()


def install_silent_subprocesses(force: bool | None = None) -> bool:
    """安装全局静默补丁：包裹 ``subprocess.Popen.__init__``。

    包装器只做一件事：若调用方**没有**显式要求新控制台/脱离进程，就把
    ``CREATE_NO_WINDOW`` OR 进 ``creationflags``。其余参数原样交给原始实现，
    因此不会改变 shell/管道/环境等任何语义。

    幂等：重复调用返回 True（已安装）。非 Windows 永远返回 False。
    安装失败只记 warning 并返回 False，绝不抛异常。

    Args:
        force: True = 无视"是否有控制台"的自动检测强制启用；None/False =
            按环境变量与自动检测决定。
    """
    global _installed, _original_popen_init
    if not IS_WINDOWS:
        return False
    if _installed:
        return True
    if not _silent_enabled(force):
        return False
    try:
        original = subprocess.Popen.__init__
        _original_popen_init = original

        def _patched_popen_init(self: Any, *args: Any, **kwargs: Any) -> None:
            # 补丁只影响"窗口是否可见"，任何异常都必须回落到原始行为：
            # creationflags 被别处改成怪值也不该让 spawn 直接失败。
            try:
                flags = int(kwargs.get("creationflags") or 0)
                if not (flags & _EXPLICIT_CONSOLE_FLAGS):
                    kwargs["creationflags"] = flags | CREATE_NO_WINDOW
            except Exception:                   # pragma: no cover - 防御性
                kwargs.pop("creationflags", None)
            original(self, *args, **kwargs)

        subprocess.Popen.__init__ = _patched_popen_init    # type: ignore[method-assign]
        _installed = True
        logger.info("silent_subprocess_installed")
        return True
    except Exception as e:                      # pragma: no cover - 防御性
        logger.warning("silent_subprocess_install_failed", error=str(e))
        return False


def uninstall_silent_subprocesses() -> bool:
    """还原 ``subprocess.Popen.__init__``（测试 / 排障用）。

    Returns:
        True = 确实还原了补丁；False = 当前没有安装（或还原失败）。
    """
    global _installed, _original_popen_init
    if not _installed or _original_popen_init is None:
        _installed = False
        return False
    try:
        subprocess.Popen.__init__ = _original_popen_init   # type: ignore[method-assign]
    except Exception as e:                      # pragma: no cover - 防御性
        logger.warning("silent_subprocess_uninstall_failed", error=str(e))
        return False
    _installed = False
    _original_popen_init = None
    logger.info("silent_subprocess_uninstalled")
    return True


def silent_subprocesses_installed() -> bool:
    """当前是否已安装全局静默补丁（测试与状态展示用）。"""
    return bool(_installed)
