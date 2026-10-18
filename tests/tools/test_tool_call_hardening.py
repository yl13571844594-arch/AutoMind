"""v1.7.6 工具调用成功率：截断参数拒绝执行 / 参数名可确定修正 / 类型纠正 / 必需参数预检。"""

from __future__ import annotations

import json

import pytest

from automind.core.json_utils import parse_tool_arguments, parse_tool_arguments_checked
from automind.core.types import LLMResponse, ToolCall, ToolResult
from automind.tools.base import (
    AbstractTool,
    ToolRegistry,
    coerce_param_types,
    normalize_param_names,
)


class _Recorder(AbstractTool):
    name = "file_write"
    description = "write"
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
            "timeout": {"type": "integer"},
            "append": {"type": "boolean"},
            "tags": {"type": "array"},
        },
        "required": ["file_path", "content"],
    }

    def __init__(self):
        self.calls = []

    async def execute(self, **kw):
        self.calls.append(kw)
        return ToolResult(tool_name=self.name, success=True, output="ok")


# ── 参数 JSON 解析 ───────────────────────────────────────────


def test_valid_args_parse_clean():
    args, err = parse_tool_arguments_checked('{"file_path": "a.py", "content": "x"}')
    assert args == {"file_path": "a.py", "content": "x"} and err == ""


def test_truncated_args_are_flagged_not_repaired():
    raw = '{"file_path": "big.py", "content": "def f():\\n    return 1\\n# 下面还有很多'
    args, err = parse_tool_arguments_checked(raw)
    assert args == {} and "截断" in err
    # 旧接口行为不变（仍尽力修补），工具调用路径改用 checked 版本
    assert parse_tool_arguments(raw).get("file_path") == "big.py"


def test_garbage_args_flagged():
    args, err = parse_tool_arguments_checked("not json at all")
    assert args == {} and "不是合法" in err


def test_empty_args_are_fine():
    assert parse_tool_arguments_checked("") == ({}, "")


# ── 参数名 / 类型 ────────────────────────────────────────────


def test_param_names_fixed_when_certain():
    t = _Recorder()
    out, ren = normalize_param_names(t, {"filePath": "a", "Content": "b"})
    assert out == {"file_path": "a", "content": "b"}
    assert ren == {"filePath": "file_path", "Content": "content"}


def test_param_name_not_overwritten_when_both_given():
    out, ren = normalize_param_names(_Recorder(), {"filePath": "a", "file_path": "b"})
    assert out["file_path"] == "b" and ren == {}


@pytest.mark.parametrize("given,expected", [
    ({"timeout": "600"}, {"timeout": 600}),
    ({"timeout": 30.0}, {"timeout": 30}),
    ({"append": "true"}, {"append": True}),
    ({"append": "否"}, {"append": False}),
    ({"append": 1}, {"append": True}),
    ({"tags": '["a", "b"]'}, {"tags": ["a", "b"]}),
    ({"tags": "solo"}, {"tags": ["solo"]}),
])
def test_type_coercion(given, expected):
    out, fixed = coerce_param_types(_Recorder(), given)
    assert out == expected and fixed


def test_ambiguous_values_left_alone():
    out, fixed = coerce_param_types(_Recorder(), {"timeout": "ten minutes", "append": "maybe"})
    assert out == {"timeout": "ten minutes", "append": "maybe"} and fixed == []


# ── dispatch 整体 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_repairs_and_runs():
    reg = ToolRegistry()
    t = _Recorder()
    reg.register(t)
    r = await reg.dispatch("FileWrite", filePath="a.py", content="x", timeout="5")
    assert r.success
    assert t.calls == [{"file_path": "a.py", "content": "x", "timeout": 5}]


@pytest.mark.asyncio
async def test_dispatch_reports_missing_required_without_running():
    reg = ToolRegistry()
    t = _Recorder()
    reg.register(t)
    r = await reg.dispatch("file_write", file_path="a.py")
    assert not r.success and "content" in r.error and "未执行" in r.error
    assert t.calls == []


# ── ReAct：残缺参数不执行 ───────────────────────────────────


class _OneShotLLM:
    """第一轮给一个参数被截断的调用，第二轮结束。"""

    def __init__(self):
        self.n = 0
        self.seen = []

    async def generate(self, messages, tools=None, stop=None):
        self.n += 1
        self.seen.append(messages[-1])
        if self.n == 1:
            return LLMResponse(text="写文件", tool_calls=[ToolCall(
                id="c1", name="file_write", arguments={},
                arguments_error="工具参数 JSON 不完整（共 9000 字符，结尾被截断）。本次调用**未执行**。")])
        return LLMResponse(text="改为分段写入，完成", tool_calls=None)


@pytest.mark.asyncio
async def test_react_does_not_execute_broken_call_and_explains():
    from automind.planning.react_executor import ReActExecutor
    reg = ToolRegistry()
    t = _Recorder()
    reg.register(t)
    llm = _OneShotLLM()
    ex = ReActExecutor(llm, reg, max_iterations=5)
    await ex.run("写个大文件")
    assert t.calls == []                                   # 没有带着空参数执行
    tool_msg = json.dumps(llm.seen[-1], ensure_ascii=False)
    assert "截断" in tool_msg and "拆小" in tool_msg
