"""``python -m automind.eval`` 的入口。

单独一个文件而不是把 CLI 塞进 ``__init__.py``：``__init__`` 会被
``import automind.eval`` 触发（程序化调用方、测试、server 都走它），把
argparse 与 ``sys.exit`` 放进去会让"只是导入这个包"也可能退出进程。
"""

from __future__ import annotations

import sys

from automind.eval.runner import main

if __name__ == "__main__":
    sys.exit(main())
