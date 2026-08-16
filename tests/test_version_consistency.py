"""版本号必须四处一致 —— 且要在**打 tag 之前**就发现不一致。

v1.6.0 发布时 `desktop/installer.iss` 被漏掉，仍停在 1.5.2。
`build_release.ps1` 里确实有一道版本一致性检查，但它只在**构建安装包时**
才跑 —— 也就是 tag 已经推上去、PyPI 与三平台构建都已触发之后才炸。
把同一条约束放进 pytest，push 前的常规测试就能拦住它。

`automind/__init__.py` 是唯一数据源（§14.1），其余三处跟它对齐。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from automind import __version__

_ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    """读文本并容忍 BOM（installer.iss 是 UTF-8-BOM）。"""
    raw = path.read_bytes()
    return raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")


def test_version_looks_like_a_release():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), \
        f"版本号格式不对：{__version__}（tag 工作流只认 v*.*.*）"


def test_pyproject_matches():
    data = tomllib.loads(_read(_ROOT / "pyproject.toml"))
    assert data["project"]["version"] == __version__, "pyproject.toml 版本未对齐"


def test_web_package_json_matches():
    data = json.loads(_read(_ROOT / "web" / "package.json"))
    assert data["version"] == __version__, "web/package.json 版本未对齐"


def test_windows_installer_matches():
    """installer.iss 决定安装包文件名与「程序和功能」里显示的版本。"""
    iss = _ROOT / "desktop" / "installer.iss"
    if not iss.exists():
        pytest.skip("社区版源码包不含 desktop/")
    m = re.search(r'#define\s+AppVersion\s+"([\d.]+)"', _read(iss))
    assert m, "installer.iss 里找不到 #define AppVersion"
    assert m.group(1) == __version__, (
        f"installer.iss={m.group(1)} 与 __init__.py={__version__} 不一致 —— "
        "打 tag 后 Windows 安装包构建会直接失败"
    )


def test_changelog_has_an_entry_for_this_version():
    """发版了却没写更新日志，等于用户升级后不知道变了什么。"""
    text = _read(_ROOT / "CHANGELOG.md")
    assert f"## [{__version__}]" in text, f"CHANGELOG.md 缺少 [{__version__}] 小节"


def test_manual_changelog_shows_this_version():
    """使用手册内嵌的更新日志同样要能查到当前版本。"""
    manual = _ROOT / "automind" / "static" / "manual.html"
    if not manual.exists():
        pytest.skip("未构建手册")
    text = _read(manual)
    assert f"v{__version__}" in text, f"手册里没有 v{__version__} 的更新日志条目"
