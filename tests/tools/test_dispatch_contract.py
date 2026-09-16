"""dispatch 的契约必须只有**一种形态**：永远返回 ToolResult（v1.7.2）。

## 修的是什么

``ToolRegistry.dispatch`` 里 ``self.get(tool_name)`` 曾写在 ``try`` 之外：

    tool = self.get(tool_name)      # ← 名字不认识 → ToolNotFoundError 逸出
    try:
        result = await tool.execute(**kwargs)   # ← 工具自己炸了 → 返回失败结果
    except Exception as e:
        return ToolResult(success=False, ...)

于是同一个入口有两种形态，调用方必须两边都处理。实际结果是只有
``react_executor`` 补了那层 ``except``，其余直接 dispatch 的地方（技能、
计划执行器、插件、CLI）全都没有 —— 一次工具名拼错就足以让整条链路崩掉，
而不是让模型看到错误、换个名字重试。

## 另一件事：那条错误的长度

旧消息是 ``Tool 'x' not found. Available: [...30+ 个工具名...]``。它会经过
``tools/output_budget.py`` 的「头+尾」夹取 —— 清单从中段被剪掉，模型拿到的
是一份**残缺**的工具表，比不给还糟（被剪断的半个名字会被当成真工具名再试一次）。
所以这里直接断言：这条错误经过体积夹取后**一字不少**。
"""

from __future__ import annotations

from typing import Any

from automind.core.types import ToolResult
from automind.tools.base import AbstractTool, ToolRegistry, resolve_name, suggest_names
from automind.tools.output_budget import limited_tool_content, limits_from_config


class _PathTool(AbstractTool):
    """带 **kwargs 的工具 —— Python 不会替我们拦住写错的参数名。

    ``file_read`` / ``file_write`` 这类真实工具就是这个形状，它们把写错的参数
    **静默忽略**然后拿默认值跑，产出一个"成功但答非所问"的结果。
    """

    name = "file_read"
    description = "读文件"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["path"],
    }

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.executed.append(dict(kwargs))
        return ToolResult(tool_name=self.name, success=True,
                          output=f"内容:{kwargs.get('path')}")


class _StrictTool(AbstractTool):
    """显式签名 —— 多传参数时由 Python 抛 TypeError。"""

    name = "strict_tool"
    description = "严格签名"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"]}

    async def execute(self, path: str) -> ToolResult:  # type: ignore[override]
        return ToolResult(tool_name=self.name, success=True, output=path)


def _registry() -> tuple[ToolRegistry, _PathTool]:
    reg = ToolRegistry()
    tool = _PathTool()
    reg.register(tool)
    reg.register(_StrictTool())
    reg.register(_PlainTool())
    return reg, tool


class _PlainTool(AbstractTool):
    """与 file_read 毫不相像的工具 —— 用来验证"不像就不硬猜"。"""

    name = "terminal"
    description = "跑命令"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, output="ok")


class _NamedTool(_PlainTool):
    """只为把注册表撑大（复现"可用：[…30+ 个…]"那条超长错误）。"""

    def __init__(self, name: str) -> None:
        self.name = name


# ═══════════════════════════════════════════════════════════
# 1. 未知工具名：返回失败结果，而不是抛异常
# ═══════════════════════════════════════════════════════════


async def test_unknown_tool_name_returns_a_failed_result_instead_of_raising():
    reg, _ = _registry()

    r = await reg.dispatch("no_such_tool_anywhere", path="x")

    assert isinstance(r, ToolResult)
    assert not r.success
    assert r.metadata.get("tool_not_found") is True
    assert "no_such_tool_anywhere" in (r.error or "")


async def test_unknown_tool_error_suggests_the_closest_names():
    reg, _ = _registry()

    r = await reg.dispatch("file_raed", path="x")

    assert "file_read" in (r.error or ""), \
        "拼错一个字母就必须把正确的名字点出来，否则模型只能盲猜"


async def test_unknown_tool_error_survives_output_budget_intact():
    """这条错误不能再被体积夹取剪断 —— 剪断的工具清单比不给更糟。"""
    reg, _ = _registry()
    # 真实注册表有 30+ 个工具，旧消息正是因此变得很长
    for i in range(40):
        reg.register(_NamedTool(f"some_long_tool_name_{i:02d}"))

    r = await reg.dispatch("browser_navgiate", url="https://example.com")

    text, stat = limited_tool_content(r.error, limits_from_config(), tool="x")

    assert stat["truncated"] is False, "未知工具名的错误不该长到被夹断"
    assert text == r.error
    assert len(r.error) < 400, f"这条错误必须短而可执行，实测 {len(r.error)} 字符"


# ═══════════════════════════════════════════════════════════
# 2. 名字只是"写法不同"：直接解析过去，不该白跑一趟
# ═══════════════════════════════════════════════════════════


async def test_case_and_separator_variants_resolve_to_the_real_tool():
    """``FileRead`` / ``file-read`` / ``FILE_READ`` 都是同一个工具。"""
    for variant in ("FileRead", "file-read", "FILE_READ", "fileRead"):
        reg, tool = _registry()
        r = await reg.dispatch(variant, path="a.txt")
        assert r.success, f"{variant} 应当被解析成 file_read"
        assert tool.executed, f"{variant} 解析成功后必须真的执行了工具"
        assert r.tool_name == "file_read"


async def test_reordered_words_resolve_to_the_real_tool():
    """``read_file`` 与 ``file_read`` 是同一组词，顺序不同而已。"""
    reg, tool = _registry()

    r = await reg.dispatch("read_file", path="a.txt")

    assert r.success and tool.executed


async def test_ambiguous_alias_is_not_guessed():
    """两个工具都可能命中时不许替模型做主 —— 猜错就是执行了另一个工具。

    这里用三个词的排列（而不是 ``read_file``/``file_read`` 两个词）：两个词的
    排列必然与其中一个候选**归一化后完全相同**，属于"可断定"的第一级匹配，
    本来就该直接解析成功。真正有歧义的是"词都对、但谁都不是它"的那种写法。
    """
    names = ["file_batch_read", "file_read_batch"]

    real, tips = resolve_name("read_batch_file", names)

    assert real is None
    assert set(tips) == set(names)


def test_suggestions_do_not_invent_matches():
    assert suggest_names("browser_navigate", ["file_read", "terminal"]) == []
    assert resolve_name("totally_unrelated_xyz", ["file_read"])[0] is None


# ═══════════════════════════════════════════════════════════
# 3. 参数名写错：执行**之前**就拦下
# ═══════════════════════════════════════════════════════════


async def test_typo_param_is_rejected_before_the_tool_runs():
    """写错的参数会被 **kwargs 工具静默忽略 —— 必须在执行前拦下。"""
    reg, tool = _registry()

    r = await reg.dispatch("file_read", pathh="a.txt")

    assert not r.success
    assert "pathh" in (r.error or "") and "path" in (r.error or "")
    assert tool.executed == [], "参数名写错时不该真的执行工具（可能带副作用）"


async def test_unrelated_extra_param_is_passed_through():
    """不能因为"不在 schema 里"就一律拒绝 —— timeout 这类约定参数本就在 schema 外。"""
    reg, tool = _registry()

    r = await reg.dispatch("file_read", path="a.txt", unrelated_thing=1)

    assert r.success and tool.executed


async def test_strict_signature_still_gets_a_readable_typeerror():
    """显式签名的工具由 Python 抛 TypeError，这里只负责翻译成人话。"""
    reg, _ = _registry()

    r = await reg.dispatch("strict_tool", pathh="x")

    assert not r.success
    assert "pathh" in (r.error or "")
    assert "path" in (r.error or "")
