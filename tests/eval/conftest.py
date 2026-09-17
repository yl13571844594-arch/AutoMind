"""把 pytest 的 ``tmp_path`` 换成"一定可写"的实现。

为什么需要它（一条容易踩死的环境坑）：

    pytest 建 ``tmp_path`` 时用的是 ``mkdir(mode=0o700)``。在 Windows 上
    ``0o700`` 缺少 ``S_IWRITE``，等于给目录打上**只读属性**；本机沙箱
    （以及部分受限 CI/企业环境）会因此对**该目录内的任何写入**返回
    ``PermissionError: [WinError 5/13]``。表现是所有用 ``tmp_path`` 的用例在
    setup 阶段 ERROR —— 看起来像测试坏了，其实是"目录被建成了只读"。

    实测（同一目录、同一进程）：

        os.mkdir(p)          → 往里写文件 OK
        os.mkdir(p, 0o700)   → 往里写文件 PermissionError
        tempfile.mkdtemp()   → 往里写文件 PermissionError   # 内部也是 0o700

    这是环境行为，不是被测代码的问题，也不该让评测框架的测试因为它变红。

做法：覆盖 ``tmp_path`` 夹具，用 ``os.mkdir`` 默认权限建目录、用完删除。
正常环境里行为完全一致（每用例一个独立临时目录、自动清理）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _make_writable_dir(prefix: str = "automind-eval-test-") -> Path:
    """建一个**默认权限**（可写）的临时目录（刻意不用 ``tempfile.mkdtemp``）。"""
    base = Path(tempfile.gettempdir())
    for _ in range(100):
        cand = base / f"{prefix}{os.urandom(4).hex()}"
        try:
            cand.mkdir()                      # 不加 mode：拿到平台默认权限
        except FileExistsError:
            continue
        except OSError:
            cand = Path(__file__).resolve().parents[2] / f".test_tmp_{os.urandom(3).hex()}"
            cand.mkdir(parents=True)
        return cand
    raise RuntimeError("无法创建可写的临时目录")


@pytest.fixture
def tmp_path() -> Iterator[Path]:             # noqa: PT004 - 覆盖 pytest 内建夹具
    """覆盖内建 ``tmp_path``：保证目录真的可写（原因见模块文档）。"""
    d = _make_writable_dir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
