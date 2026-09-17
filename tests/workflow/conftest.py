"""工作流测试的公共夹具。

设计原则：**全部离线、全部可断言**。

* 假工具直接实现 `AbstractTool` + 真 `ToolRegistry` —— 用真注册表而不是再写一个
  假注册表，是因为工作流执行器依赖 `dispatch` 的**契约**（无论发生什么都返回
  ToolResult）。若测试里绕过它，测出来的就不是生产路径。
* 假 LLM 只记录收到的 messages，不做任何生成 —— `llm` 步骤的语义就是
  "把渲染好的提示词交给一次生成"，测这个即可，不需要真模型。
* 所有计数器都是显式的（`calls`），因为"dry-run 不产生副作用"这类断言
  只能靠"工具到底被没被调用"来证明，不能靠输出长得像不像。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool, ToolRegistry
from automind.workflow.loader import WorkflowLoader
from automind.workflow.schema import WorkflowSchema


class RecordingTool(AbstractTool):
    """记录调用次数与参数的假工具；可按脚本返回成功/失败。"""

    name = "recorder"
    description = "测试用假工具：记录每次调用的参数，并按脚本返回结果"
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
    permission_tier = PermissionTier.SAFE

    def __init__(self) -> None:
        #: 每次调用的参数
        self.calls: list[dict[str, Any]] = []
        #: 依次返回的失败原因（用完后一律成功）
        self.failures: list[str] = []
        #: 每次调用的模拟耗时（秒），用于超时用例
        self.delay: float = 0.0
        #: 返回的成功输出（None = 回显收到的参数）
        self.output: Any = None
        #: 抛异常而不是返回失败结果（验证"异常不许吞掉"）
        self.raise_error: BaseException | None = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_error is not None:
            raise self.raise_error
        if self.failures:
            return ToolResult(tool_name=self.name, success=False,
                              error=self.failures.pop(0), exit_code=1)
        return ToolResult(tool_name=self.name, success=True,
                          output=self.output if self.output is not None else kwargs)


class FakeLLM:
    """假模型：记录提示词，返回固定文本（或按需抛错）。"""

    def __init__(self, text: str = "（模型生成的文本）") -> None:
        self.text = text
        self.calls: list[list[dict[str, Any]]] = []
        self.error: BaseException | None = None

    async def generate(self, messages: list[dict[str, Any]], **_: Any) -> Any:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.text)


class _FakeResponse:
    """模拟 `automind.core.types.LLMResponse`：执行器只依赖 `.text`。"""

    def __init__(self, text: str) -> None:
        self.text = text


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(RecordingTool())
    return reg


@pytest.fixture
def recorder(registry: ToolRegistry) -> RecordingTool:
    return registry.get("recorder")          # type: ignore[return-value]


@pytest.fixture
def loader() -> WorkflowLoader:
    """不带工具清单的加载器（避免测试结果随内置工具增减而变）。"""
    return WorkflowLoader()


@pytest.fixture(scope="session")
def _workspace_tmp_root():
    """工作区内的临时根目录（**不**依赖 pytest 的 `tmp_path`）。

    为什么不用 `tmp_path`：本机沙箱把 `%TEMP%` 映射成只读，pytest 的
    `tmp_path_factory` 在 setup 阶段就抛 `PermissionError: [WinError 5]`，
    用例连跑都跑不起来（实测）。工作区是可写的，所以临时文件统一放这里。
    放在 `.workflow_test_tmp/` 是为了让人一眼看出这是测试残留、可直接删。
    """
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / ".workflow_test_tmp"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def wx_tmp(_workspace_tmp_root, request):
    """每个用例一个独立子目录，用例结束即删。"""
    import shutil

    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request.node.name)
    path = _workspace_tmp_root / safe[:80]
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def parse(text: str, *, loader: WorkflowLoader | None = None) -> WorkflowSchema:
    """加载一段 YAML，失败即抛 —— 测试里断言 schema 形状用。"""
    return (loader or WorkflowLoader()).parse(text)


def issues_of(text: str, *, loader: WorkflowLoader | None = None) -> list[str]:
    """加载一段 YAML，返回全部错误信息（拼接后的可读文本列表）。"""
    schema, errors, warnings = (loader or WorkflowLoader()).try_parse(text)
    assert schema is None, "期望校验失败，但它通过了"
    return [e.format() for e in errors]


@pytest.fixture
def approval_recorder() -> Any:
    """审批回调记录器：可配置批准/拒绝/抛异常，并记下收到的提示词。"""

    class _Approval:
        def __init__(self, approved: bool = True, error: BaseException | None = None,
                     comment: str = "") -> None:
            self.approved = approved
            self.error = error
            self.comment = comment
            self.prompts: list[str] = []
            self.steps: list[str] = []

        async def __call__(self, step: Any, prompt: str) -> Any:
            self.steps.append(getattr(step, "id", str(step)))
            self.prompts.append(prompt)
            if self.error is not None:
                raise self.error
            if self.comment:
                return {"approved": self.approved, "comment": self.comment}
            return self.approved

    return _Approval


def make_inputs(**kwargs: Any) -> dict[str, Any]:
    return dict(kwargs)


def stopwatch() -> float:
    return time.perf_counter()
