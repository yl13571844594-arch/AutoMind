"""dispatch 的失败反馈必须让模型能自愈（v1.7.0）。

回归的是一类**不崩溃、但模型学不到任何东西**的缺陷：模型把参数名写错时，
``ToolRegistry.dispatch`` 只把 ``str(exc)`` 交给模型，于是观察里既没有异常
类型，也没有本工具的合法参数名 —— 模型只能盲猜重试，命中率全看运气。

这里盯住三条最常走的路：
1. 少传参数（工具内部 ``kwargs["x"]`` → KeyError）；
2. 参数名写错（显式签名 → TypeError: unexpected keyword argument）；
3. 工具自身抛的其他异常（不能被上面的语义化覆盖掉，类型必须保留）。
"""

from __future__ import annotations

from typing import Any

from automind.core.types import ToolResult
from automind.tools.base import AbstractTool, ToolRegistry, describe_tool_error


class _NeedyTool(AbstractTool):
    """模拟 ``kwargs["path"]`` 取值的工具（file_editor 就是这种写法）。"""

    name = "needy"
    description = "test tool"
    parameters = {
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, content: str | None = None, **kwargs: Any) -> ToolResult:
        path = kwargs["path"]                      # 少传 → KeyError('path')
        return ToolResult(tool_name=self.name, success=True, output=path)


class _StrictTool(AbstractTool):
    """显式签名 —— 多传参数时由 Python 抛 TypeError。"""

    name = "strict"
    description = "test tool"
    parameters = {"properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, path: str) -> ToolResult:  # type: ignore[override]
        return ToolResult(tool_name=self.name, success=True, output=path)


class _BrokenTool(AbstractTool):
    """工具内部出错 —— 与调用契约无关，类型信息必须保留。"""

    name = "broken"
    description = "test tool"
    parameters = {"properties": {"path": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise ValueError("底层炸了")


async def test_missing_param_tells_the_model_which_param_and_the_valid_list():
    reg = ToolRegistry()
    reg.register(_NeedyTool())

    r = await reg.dispatch("needy", content="x")           # 忘了 path

    assert not r.success
    assert "path" in r.error, "必须点名缺的是哪个参数"
    assert "content" in r.error, "必须列出本工具的合法参数名，模型才知道还能传什么"


async def test_wrong_param_name_is_reported_as_unknown_not_as_a_cryptic_key():
    reg = ToolRegistry()
    reg.register(_StrictTool())

    r = await reg.dispatch("strict", pathh="x")            # 拼错成 pathh

    assert not r.success
    assert "pathh" in r.error, "必须回显模型写错的参数名"
    assert "path" in r.error, "必须给出正确的参数名"


async def test_unrelated_exception_keeps_its_type_and_message():
    reg = ToolRegistry()
    reg.register(_BrokenTool())

    r = await reg.dispatch("broken", path="x")

    assert not r.success
    assert "ValueError" in r.error and "底层炸了" in r.error, \
        "非调用契约类异常不能被语义化改写掉类型"


class _BareTool(AbstractTool):
    """连 parameters 都没声明 —— 拼接提示时不能崩。"""

    name = "bare"
    description = "test tool"

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("x")


async def test_keyerror_from_tool_internals_is_not_mistaken_for_missing_param():
    """工具自身的 bug 不该被包装成"你少传了参数"，那会把模型带偏。"""
    err = describe_tool_error(_BrokenTool(), KeyError("some_internal_key"))

    assert "缺少必需参数" not in err, "内部 KeyError 不是模型该补的参数"
    assert "KeyError" in err, "但类型仍要如实保留"


def test_tool_without_declared_parameters_does_not_crash():
    assert describe_tool_error(_BareTool(), RuntimeError("x")) == "RuntimeError: x"
