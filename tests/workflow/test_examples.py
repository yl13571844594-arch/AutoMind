"""示例文件测试 —— 示例是"最常被抄的模板"，必须永远是好的。

`examples/06-workflow/` 里那两份文件有双重身份：既是给客户看的样板，
也是用户第一次照抄的对象。一份抄了就跑不起来的示例，比没有示例更糟 ——
它会让"工作流即代码"这个概念在第一次尝试时就失去可信度。

因此这里对每一份 `*.yaml` 都做三件事：
  1. 能通过校验（加载期零错误）；
  2. dry-run 能完整跑完且**退出码为 0**（dry-run 的整轮状态是 dry_run，
     没有失败步骤就是全绿）；
  3. 文件里出现的工具名在做完 dry-run 的工具清单里 —— 否则真跑必失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from automind.workflow.__main__ import build_cli_registry, main
from automind.workflow.loader import WorkflowLoader

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "06-workflow"
YAML_FILES = sorted(EXAMPLES.glob("*.yaml"))


def test_examples_exist() -> None:
    names = {p.name for p in YAML_FILES}
    assert "change_request.yaml" in names
    assert "hello.yaml" in names


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: p.name)
def test_example_loads_without_errors(path: Path) -> None:
    schema, errors, warnings = WorkflowLoader().try_load(path)
    assert schema is not None, [e.format() for e in errors]
    assert not errors
    # 警告也应当是零：示例里出现"引用了排后面的步骤"这类提醒会让人以为是坑
    assert not warnings, [w.format() for w in warnings]
    assert schema.version == 1
    assert schema.steps
    assert schema.source_digest


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: p.name)
def test_example_dry_run_passes(path: Path, capsys) -> None:
    """dry-run 必须全绿 —— 这是"客户拿到手就能看"的底线。"""
    args = ["run", str(path), "--dry-run", "--quiet"]
    if path.name == "change_request.yaml":
        # 该示例声明了必填入参，dry-run 也要给全（否则退出码是 2，那是对的）
        args += ["--input", "ticket_id=INC0012345"]
    code = main(args)
    captured = capsys.readouterr()
    assert code == 0, captured.err + captured.out
    assert "试运行计划" in captured.out


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: p.name)
def test_example_tools_are_registered(path: Path) -> None:
    """示例里写的工具必须真存在，否则用户抄完一跑就失败。"""
    schema = WorkflowLoader().load(path)
    registry = build_cli_registry("builtin")
    names = set(registry.list_names())
    for step in schema.steps:
        if step.type == "tool":
            assert step.tool in names, f"{path.name} 里的工具 {step.tool!r} 不在注册表中"


def test_change_request_has_human_gate() -> None:
    """核心样板必须带人工审批环节，且审批在"改生产"那一步**之前**。

    顺序断言是这份测试的重点：审批放到执行之后，流程看上去一模一样
    （步骤齐全、能跑通），但"人批过才动手"这条保证已经没了。
    """
    schema = WorkflowLoader().load(EXAMPLES / "change_request.yaml")
    kinds = [s.type for s in schema.steps]
    assert "human" in kinds
    human_at = next(i for i, s in enumerate(schema.steps) if s.type == "human")
    write_at = schema.index_of("execute_change")
    assert write_at > 0, "示例里应当有一个明确的执行变更步骤"
    assert human_at < write_at
    # 执行变更这一步失败必须能拦住整轮（生产变更失败不能"继续往下走"）
    assert schema.get_step("execute_change").on_failure.kind in ("abort", "retry")


def test_hello_is_offline_and_self_contained() -> None:
    """最小示例不许联网、不许要凭据 —— 它是 CI 的冒烟用例。"""
    schema = WorkflowLoader().load(EXAMPLES / "hello.yaml")
    for step in schema.steps:
        assert step.type == "tool"
        assert step.tool in {"file_write", "file_read"}, step.tool
