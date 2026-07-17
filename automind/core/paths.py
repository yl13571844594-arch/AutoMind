"""数据目录统一解析 — 开发/pip 模式与桌面（冻结）模式的唯一分叉点。

解析优先级（``data_dir()``）：
    1. 环境变量 ``AUTOMIND_DATA_DIR``（显式指定，最高优先）；
    2. 冻结环境（PyInstaller 等，``sys.frozen``）→ 平台标准应用数据目录：
         Windows  %APPDATA%\\AutoMind
         macOS    ~/Library/Application Support/AutoMind
         Linux    $XDG_DATA_HOME/automind 或 ~/.local/share/automind
    3. 其余（开发 / pip 安装）→ 当前工作目录 ``.automind``（保持既有行为，
       对现有用户与全部测试零影响）。

约定：
    - **服务级数据**（配置/数据库/知识库/专家/技能/缓存等）一律经本模块取路径；
    - **项目级数据**（chroma 记忆、checkpoints、project_index）跟随
      ``project_root``，不在本模块管辖 —— 换工作区即换目录是特性而非缺陷。

桌面封装（desktop/main.py）在启动最早期设置 ``AUTOMIND_DATA_DIR``，
使多处模块无论以何种顺序导入都取到一致目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "AutoMind"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 等冻结环境。"""
    return bool(getattr(sys, "frozen", False))


def _platform_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME.lower()


def data_dir() -> Path:
    """应用数据根目录（不保证已创建；写入方各自 mkdir）。"""
    env = os.environ.get("AUTOMIND_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if is_frozen():
        return _platform_data_dir()
    return Path(".automind")


def config_file() -> Path:
    """主配置文件（API Key / 工作区 / 偏好，纯文本 JSON）。

    开发/pip 模式保持既有的 ``./.automind_config.json``（cwd 根下，便于手改）；
    冻结/显式数据目录模式收敛到 ``<data_dir>/config.json``。
    """
    env = os.environ.get("AUTOMIND_DATA_DIR", "").strip()
    if env or is_frozen():
        return data_dir() / "config.json"
    return Path(".automind_config.json")


def db_file() -> Path:
    """主 SQLite 库（任务历史/团队任务/限额 kv）。"""
    return data_dir() / "automind.db"


def sessions_db_file() -> Path:
    """会话 SQLite 库（多用户对话历史）。"""
    return data_dir() / "sessions.db"


def kb_dir() -> Path:
    """知识库目录（kb.db + 旧 JSON 备份）。"""
    return data_dir() / "kb"


def skills_dir() -> Path:
    """自定义技能目录。"""
    return data_dir() / "skills"


def experts_file() -> Path:
    return data_dir() / "experts.json"


def legacy_file(name: str) -> Path:
    """数据目录下的旧版 JSON 平面文件（仅供一次性迁移读取）。"""
    return data_dir() / name


def describe() -> dict:
    """诊断信息（/api/health 与桌面「打开数据目录」用）。"""
    return {
        "data_dir": str(data_dir().resolve()),
        "config_file": str(config_file().resolve()),
        "frozen": is_frozen(),
        "env_override": bool(os.environ.get("AUTOMIND_DATA_DIR", "").strip()),
    }
