"""v1.11.5「在线客服」样板 + branch 新算子 contains_any。

样板是"拿到手就能跑"的承诺：每条走固定话术的分支都要真跑一遍、落到正确的回复；
投诉路径必须先过人工 —— 命令行没有审批通道时停在人工步骤（fail-closed），
而不是绕过坐席直接对客户承诺"已转人工"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from automind.workflow.__main__ import main
from automind.workflow.conditions import check_condition, split_condition
from automind.workflow.loader import WorkflowLoader

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "06-workflow" / "customer_service.yaml"
SAMPLES = ROOT / "automind" / "workflow" / "samples"


@pytest.fixture
def workdir():
    """工作区内的临时目录（沙箱下 %TEMP% 不可写，同 tests/workflow 其它用例）。"""
    d = ROOT / ".workflow_test_tmp" / "cs"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _run(workdir: Path, message: str) -> tuple[int, str]:
    out = workdir / "out"
    code = main(["run", str(EXAMPLE), "--quiet",
                 "--input", f"message={message}", "--input", f"out_dir={out.as_posix()}"])
    reply = out / "cs-reply.md"
    return code, reply.read_text(encoding="utf-8") if reply.exists() else ""


def test_sample_loads_clean() -> None:
    schema, errors, warnings = WorkflowLoader().try_load(EXAMPLE)
    assert schema is not None and not errors and not warnings
    kinds = [s.type for s in schema.steps]
    assert kinds.count("branch") == 4 and "human" in kinds and "agent" in kinds
    # 坐席确认排在"回复客户"之前
    assert schema.index_of("agent_takeover") < schema.index_of("handoff_reply")
    agent = schema.get_step("ai_reply")
    assert set(agent.allowed_tools) == {"file_read", "file_write"}


@pytest.mark.parametrize(("message", "expect"), [
    ("我昨天的订单什么时候发货", "24 小时内发货"),
    ("快递到哪了", "查看物流"),
    ("我要退款", "无理由退货"),
    ("麻烦开一张发票", "电子发票"),
])
def test_fixed_reply_branches_run_for_real(workdir, message, expect) -> None:
    code, reply = _run(workdir, message)
    assert code == 0
    assert expect in reply
    session = (workdir / "out" / "cs-session.md").read_text(encoding="utf-8")
    assert message in session


def test_complaint_stops_at_human_gate_without_channel(workdir) -> None:
    code, reply = _run(workdir, "我要投诉你们")
    assert code == 1, "没有审批通道时投诉路径必须停下"
    assert reply == "", "坐席没确认就不能对客户回复"
    assert (workdir / "out" / "cs-handoff.md").exists(), "人工工单应当已经登记"


def test_packaged_sample_matches_example() -> None:
    """界面一键安装的是随包版本；它与 examples 里的版本只允许路径不同。"""
    packaged = (SAMPLES / "customer_service.yaml").read_text(encoding="utf-8")
    example = EXAMPLE.read_text(encoding="utf-8").replace("examples/06-workflow/", "workflows/")
    assert packaged == example
    assert (SAMPLES / "customer-service-kb.md").read_text(encoding="utf-8") == \
        (EXAMPLE.parent / "customer-service-kb.md").read_text(encoding="utf-8")


# ── contains_any ────────────────────────────────────────────────


def test_contains_any_parses_before_contains() -> None:
    cond = split_condition("{{ inputs.m }} contains_any [退款, 退货]")
    assert cond is not None and cond.op == "contains_any"
    assert cond.right == "[退款, 退货]"
    assert split_condition("{{ inputs.m }} contains 退款").op == "contains"


@pytest.mark.parametrize(("condition", "ok"), [
    ("{{ inputs.m }} contains_any [a, b]", True),
    ("{{ inputs.m }} contains_any {{ inputs.words }}", True),
    ("{{ inputs.m }} contains_any 退款", False),       # 单个词请用 contains
    ("{{ inputs.m }} contains_any []", False),         # 空列表永远不成立
])
def test_contains_any_load_checks(condition, ok) -> None:
    assert (check_condition(condition) == []) is ok


def test_contains_any_evaluates(workdir) -> None:
    flow = workdir / "any.yaml"
    flow.write_text(
        "version: 1\nname: t\ninputs:\n  m:\n    type: string\n    required: false\n"
        "    default: x\n  out:\n    type: string\n    required: false\n    default: o\n"
        "steps:\n"
        "  - id: b\n    type: branch\n"
        "    condition: \"{{ inputs.m }} contains_any [退款, '', 退货]\"\n"
        "    then: hit\n    else: miss\n"
        "  - id: hit\n    type: tool\n    tool: file_write\n"
        "    args: {path: \"{{ inputs.out }}/r.txt\", content: hit}\n"
        "  - id: miss\n    type: tool\n    tool: file_write\n"
        "    args: {path: \"{{ inputs.out }}/r.txt\", content: miss}\n",
        encoding="utf-8")
    out = (workdir / "o").as_posix()
    for text, want in (("我想退货", "hit"), ("你好", "miss")):
        assert main(["run", str(flow), "--quiet", "--input", f"m={text}",
                     "--input", f"out={out}"]) == 0
        # 空串关键词不能让"你好"也命中
        assert (workdir / "o" / "r.txt").read_text(encoding="utf-8") == want
