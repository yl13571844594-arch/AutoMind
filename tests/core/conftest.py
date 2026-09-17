"""把 pytest 的 ``tmp_path`` 换成"一定可写"的实现，并隔离 pytest 自己的临时目录。

为什么需要这个文件（一条容易踩死的环境坑）：

    pytest 建 ``tmp_path`` 时用的是 ``mkdir(mode=0o700)``。在 Windows 上，
    ``0o700`` 缺少 ``S_IWRITE``，等于给目录打上 **只读属性**；本机沙箱
    （以及部分受限 CI/企业环境）会因此对**该目录内的任何写入**返回
    ``PermissionError: [WinError 5/13]``。表现是：所有用 ``tmp_path`` 的用例
    都在 setup 阶段 ERROR —— 看起来像测试坏了，其实是"目录被建成了只读"。

    实测（同一目录、同一进程）：

        os.mkdir(p)            → 往里写文件 OK
        os.mkdir(p, 0o700)     → 往里写文件 PermissionError
        tempfile.mkdtemp()     → 往里写文件 PermissionError   # 内部也是 0o700

    这是环境行为，不是被测代码的问题；也不该让测试因为这个环境而变红。

做法：在本目录的用例里**覆盖 ``tmp_path`` 夹具**，用 ``os.mkdir`` 的默认权限
建目录，收尾时删除。等价能力（每用例一个独立临时目录、自动清理），但没有那个
只读属性。正常环境（Linux/macOS/普通 Windows）里行为完全一致，因此这不是
"只在沙箱里生效的补丁"，而是"对权限位更宽容的等价实现"。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _make_writable_dir(prefix: str = "automind-test-") -> Path:
    """建一个**默认权限**（可写）的临时目录。

    刻意不用 ``tempfile.mkdtemp``：它内部用 ``0o700``，正是上面那个坑。
    """
    base = Path(tempfile.gettempdir())
    for _ in range(100):
        cand = base / f"{prefix}{os.urandom(4).hex()}"
        try:
            cand.mkdir()                      # 不加 mode：拿到平台默认权限
        except FileExistsError:
            continue
        except OSError:
            # 系统临时目录不可写时退回仓库内（工作区一定可写）
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


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """``tmp_path`` 的别名，便于用例显式表达"这里要的是普通临时目录"。"""
    return tmp_path
