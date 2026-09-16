"""外部可执行文件缺失时，给的必须是**装得上**的办法（v1.7.2）。

## 修的是什么

``_toolkit.need("pytesseract")`` 此前只管 Python 包在不在。可 pytesseract 只是
tesseract **引擎的壳**：``pip install pytesseract`` 装完，真正干活的外部程序
仍然不在系统里，OCR 照旧失败 —— 而错误信息里给的还是那句 ``pip install``。
用户照着做一遍，问题一字不变，形成"提示→照做→还失败→看提示"的死循环。

ffmpeg 同理：视频工具只说了句"请先安装 ffmpeg 并加入 PATH"，没说要怎么装、
装到哪；Windows 用户多半会去 ``pip install ffmpeg``（PyPI 上确实有个同名包，
但它不提供可执行文件）。

这里把两类缺失**分开**：缺 Python 包 → pip 命令；缺外部程序 → 该平台的
安装命令（winget / brew / apt），并且明确写出"这不是 Python 包"。
"""

from __future__ import annotations

import os
import sys

import pytest

from automind.tools import _toolkit as tk

# ═══════════════════════════════════════════════════════════
# 1. 提示的措辞：不许再给 pip 药方
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("binary", ["ffmpeg", "ffprobe", "tesseract", "git"])
def test_missing_binary_message_never_recommends_pip(binary):
    msg = str(tk.MissingBinary(binary))

    assert f"pip install {binary}" not in msg, \
        "外部程序装不上 pip —— 给 pip 命令就是把人往死路上引"
    assert "pip install" not in msg.split("安装办法")[0] or "不是 Python 包" in msg
    assert binary in msg


@pytest.mark.parametrize("binary", ["ffmpeg", "tesseract", "git"])
def test_missing_binary_message_names_an_install_command_for_this_platform(binary):
    msg = str(tk.MissingBinary(binary))
    key = "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
    expected = tk._EXTERNAL[binary][1][key]

    assert expected in msg, "必须给出本平台可直接照抄的安装命令"


def test_missing_binary_with_module_says_the_pip_part_is_already_done():
    """装了壳、没装引擎 —— 提示必须点破这一层，否则用户会以为装漏了 python 包。"""
    msg = str(tk.MissingBinary("tesseract", module="pytesseract"))

    assert "pytesseract" in msg and "已经装上" in msg
    assert "tesseract" in msg


def test_env_var_escape_hatch_is_documented():
    """装在非标准目录时给一条出路，而不是逼用户去改系统 PATH。"""
    msg = str(tk.MissingBinary("tesseract"))

    assert "AUTOMIND_TESSERACT_CMD" in msg


# ═══════════════════════════════════════════════════════════
# 2. need() 要连带检查外部程序
# ═══════════════════════════════════════════════════════════


def test_need_reports_missing_binary_even_though_the_module_imports(monkeypatch):
    """这是本文件最核心的一条：模块导得进来，但外部程序不在 → 必须报外部程序。"""
    monkeypatch.setitem(tk.MODULE_BINARIES, "json", ("definitely-not-installed-xyz",))

    with pytest.raises(tk.MissingBinary) as e:
        tk.need("json")                      # json 一定导得进来

    assert e.value.binary == "definitely-not-installed-xyz"


def test_need_still_reports_missing_python_package(monkeypatch):
    monkeypatch.delitem(tk.MODULE_BINARIES, "json", raising=False)

    with pytest.raises(tk.MissingDependency) as e:
        tk.need("a_module_that_cannot_exist_xyz")

    assert e.value.module == "a_module_that_cannot_exist_xyz"


def test_need_binary_raises_with_install_hint():
    with pytest.raises(tk.MissingBinary):
        tk.need_binary("definitely-not-installed-xyz")


def test_need_binary_accepts_an_env_override(tmp_path, monkeypatch):
    """AUTOMIND_<NAME>_CMD 指向真实文件时应当直接采用它。"""
    fake = tmp_path / "mybin.exe"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AUTOMIND_MYBIN_XYZ_CMD", str(fake))

    assert tk.find_binary("mybin_xyz") == str(fake)
    assert tk.need_binary("mybin_xyz") == str(fake)


def test_find_binary_returns_none_instead_of_raising():
    """"有没有"和"没有怎么办"是两件事：界面自检只要前者。"""
    assert tk.find_binary("definitely-not-installed-xyz") is None


# ═══════════════════════════════════════════════════════════
# 3. 工具返回：两类缺失不能混成一个字段
# ═══════════════════════════════════════════════════════════


def test_err_keeps_binary_and_package_failures_apart():
    r_bin = tk.err("ocr_tool", tk.MissingBinary("tesseract", module="pytesseract"))
    r_pkg = tk.err("ocr_tool", tk.MissingDependency("pytesseract"))

    assert r_bin.output.get("missing_binary") == "tesseract"
    assert "install_hint" in r_bin.output
    assert "missing_dependency" not in r_bin.output
    assert r_pkg.output.get("missing_dependency", "").startswith("pytesseract")
    assert "missing_binary" not in r_pkg.output


async def test_video_tool_tells_you_how_to_install_ffmpeg(tmp_path, monkeypatch):
    """视频工具此前只说"请先安装 ffmpeg 并加入 PATH"，等于没说怎么装。"""
    from automind.tools.media_tools import VideoTool

    monkeypatch.setattr(tk, "find_binary", lambda *_a, **_kw: None)
    monkeypatch.setattr("automind.tools.media_tools.find_binary", lambda *_a, **_kw: None)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")

    r = await VideoTool().execute(action="info", path=str(video))

    assert not r.success
    assert "ffprobe" in (r.error or "")
    # "pip install ffmpeg" 是一条走不通的路：PyPI 上那个同名包不提供可执行文件
    assert "pip install ffprobe" not in (r.error or "")
    assert tk._EXTERNAL["ffprobe"][1][
        "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
    ] in (r.error or "")


def test_binary_install_hint_is_never_empty():
    for binary in tk._EXTERNAL:
        assert tk.binary_install_hint(binary).strip()
