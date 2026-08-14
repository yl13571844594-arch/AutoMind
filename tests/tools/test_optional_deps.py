"""可选依赖的"承诺"必须兑现 —— 提示语里的安装命令得真的能装上东西。

工具缺库时返回的提示是：

    缺少「数据图表绘制」所需的依赖 matplotlib>=3.7。请先安装：pip install matplotlib>=3.7
    （或一次装齐办公套件：pip install 'automind-agent[office]'）

v1.6.0 加了 7 个可选依赖（pillow / python-pptx / matplotlib / psutil /
pyperclip / pytesseract / mutagen）却没同步进 `[office]` extra —— 用户照着
那句提示装完，ocr / ppt / chart / audio / video 五个工具**依旧用不了**。
这类"文档与代码各说各话"的缺陷不会有任何报错，只能靠断言把两边钉在一起。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from automind.tools._toolkit import OPTIONAL_DEPS

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: 仅在特定平台可用、不该进跨平台 extra 的依赖
_PLATFORM_ONLY = {"win32com"}


def _extra(name: str) -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"][name]


def _dist(requirement: str) -> str:
    """"matplotlib>=3.7" → "matplotlib"（小写、下划线归一为连字符）。"""
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip().lower().replace("_", "-")


def test_every_optional_dep_is_installable_via_office_extra():
    office = {_dist(r) for r in _extra("office")}
    missing = sorted(
        pkg for mod, (pkg, _purpose) in OPTIONAL_DEPS.items()
        if mod not in _PLATFORM_ONLY and _dist(pkg) not in office
    )
    assert not missing, (
        f"这些可选依赖在提示语里被指向 [office] extra，但 extra 里没有：{missing}。"
        "用户照提示执行 pip install 'automind-agent[office]' 后工具仍然不可用。"
    )


def test_office_extra_is_a_subset_of_full():
    """[full] 号称"全都要"，不能反而比 [office] 少装东西。"""
    office = {_dist(r) for r in _extra("office")}
    full = {_dist(r) for r in _extra("full")}
    assert not (office - full), f"[full] 缺少 [office] 里的：{sorted(office - full)}"


@pytest.mark.parametrize("module", sorted(OPTIONAL_DEPS))
def test_dependency_hint_is_actionable(module):
    """每条提示都要给出可照抄的命令，且点明用途（而不是甩一个模块名）。"""
    from automind.tools._toolkit import MissingDependency

    msg = str(MissingDependency(module))
    pkg, purpose = OPTIONAL_DEPS[module]
    assert purpose in msg, "提示里要说清楚这个依赖是干什么用的"
    assert f"pip install {pkg}" in msg, "提示里要有可直接复制执行的安装命令"
