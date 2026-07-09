"""仓库根 conftest — 开发环境下让商业扩展包（pro/automind_pro）可被导入。

发行的社区版源码包不含 pro/ 目录，此逻辑自动失效，不影响社区用户。
"""

import sys
from pathlib import Path

_PRO_DIR = Path(__file__).parent / "pro"
if _PRO_DIR.is_dir() and str(_PRO_DIR) not in sys.path:
    sys.path.insert(0, str(_PRO_DIR))
