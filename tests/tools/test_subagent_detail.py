"""``subagent_list`` 的"点名查看"模式（agent_id 详情视图）。

## 为什么需要它

旧 ``subagent_list`` 只回列表 + 每条最多 4000 字符的截断结果。父任务真正要
"拿这个子智能体的结果继续干活"时，截断意味着它只能再派发一次 —— 白烧一遍
token 与时间。本文件的用例钉住三件事：

1. **按 id 取详情**：status / steps / tokens / error / elapsed / output_chars
   等字段齐全，output 是完整结果（上限 ``DETAIL_OUTPUT_CHARS``）；
2. **截断必须标注**：超长时 ``truncated=True`` + ``output_chars=原长``，
   绝不静默变短；
3. **无 agent_id 时旧行为不变**：仍然是"列表 + 结果（受旧上限约束）"。

用注入的 FakeManager 而非真实 ``SubagentManager``：这里验的是工具层如何
**呈现**数据（字段/上限/错误话术），不是运行时如何跑子任务（那由
``tests/planning/test_subagents.py`` 覆盖）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from automind.planning import subagents as runtime
from automind.tools.subagents import DETAIL_OUTPUT_CHARS, SubagentListTool


class FakeManager:
    """最小管理器替身：只为工具层提供 list / get / get_record 三个入口。

    ``records`` 用来模拟真实管理器的 ``get_record``（记录里存的是原文），
    ``details`` 模拟 ``get`` 的 detail（output 已被运行时截到 8000）。
    """

    def __init__(self, items: list[dict] | None = None,
                 details: dict[str, dict] | None = None,
                 records: dict[str, Any] | None = None,
                 has_get_record: bool = True) -> None:
        self.items = list(items or [])
        self.details = dict(details or {})
        self.records = dict(records or {})
        if not has_get_record:
            # 旧/替身管理器可能没有 get_record —— 工具必须优雅退回 detail.output
            self.get_record = None                     # type: ignore[assignment]
        self.calls: list[tuple[Any, ...]] = []

    def list(self, *, session_id: str | None = None) -> list[dict]:
        self.calls.append(("list", session_id))
        return [dict(item) for item in self.items]

    def get(self, agent_id: str, *,
            session_id: str | None = None) -> dict | None:
        self.calls.append(("get", agent_id, session_id))
        detail = self.details.get(agent_id)
        return dict(detail) if detail is not None else None

    def get_record(self, agent_id: str, *,
                   session_id: str | None = None) -> Any | None:
        """真实管理器有这个入口（记录里存输出原文）；测试按需注入。"""
        self.calls.append(("get_record", agent_id, session_id))
        return self.records.get(agent_id)


def _item(agent_id: str, status: str = "done", **extra: Any) -> dict:
    base = {
        "agent_id": agent_id,
        "title": f"子任务 {agent_id}",
        "task": "统计 tests 目录的用例数",
        "status": status,
        "steps": 3,
        "tokens": 120,
        "error": "",
        "stop_reason": "",
        "resumable": False,
        "output_tail": "…",
        "running": status == "running",
        "elapsed_s": 1.5,
    }
    base.update(extra)
    return base


def _detail(agent_id: str, status: str = "done", output: str = "",
            **extra: Any) -> dict:
    base = _item(agent_id, status)
    base["output"] = output
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _bind_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具层的"当前父会话/会话 id"来自运行时桥接；这里固定成测试值。"""
    monkeypatch.setattr(runtime, "current_agent", lambda: object())
    monkeypatch.setattr(runtime, "current_session_id", lambda: "s-test")
    monkeypatch.setattr(runtime, "current_depth", lambda: 0)


# ═══════════════════════════════════════════════════════════
# 1. 按 id 取详情
# ═══════════════════════════════════════════════════════════


async def test_detail_returns_full_output_and_all_fields():
    full = "结论：共 37 个用例，2 个失败（tests/a.py:..）"
    manager = FakeManager(
        items=[_item("sa3")],
        details={"sa3": _detail("sa3", output="短视图")},
        records={"sa3": SimpleNamespace(output=full)},
    )

    result = await SubagentListTool(manager).execute(agent_id="sa3")

    assert result.success is True
    payload = result.output
    assert payload["agent_id"] == "sa3"
    assert payload["status"] == "done"
    assert payload["steps"] == 3
    assert payload["tokens"] == 120
    assert payload["error"] == ""
    assert payload["elapsed_s"] == 1.5
    assert payload["output"] == full, "有记录时必须回原文，而不是 detail 里截断过的版本"
    assert payload["output_chars"] == len(full)
    assert payload["truncated"] is False
    assert "已结束" in payload["note"]
    # 详情模式下不该混入列表字段
    assert "agents" not in payload and "results" not in payload


async def test_detail_falls_back_to_manager_detail_output():
    """没有 get_record（或记录已清理）时退回 detail.output，不能直接给空串。"""
    manager = FakeManager(
        items=[_item("sa1")],
        details={"sa1": _detail("sa1", output="来自 detail 的结果")},
        has_get_record=False,
    )

    result = await SubagentListTool(manager).execute(agent_id="sa1")

    assert result.success is True
    assert result.output["output"] == "来自 detail 的结果"
    assert result.output["output_chars"] == len("来自 detail 的结果")


async def test_detail_clips_long_output_and_flags_it():
    long_output = "A" * (DETAIL_OUTPUT_CHARS + 5000)
    manager = FakeManager(
        items=[_item("sa7")],
        details={"sa7": _detail("sa7", output="short")},
        records={"sa7": SimpleNamespace(output=long_output)},
    )

    result = await SubagentListTool(manager).execute(agent_id="sa7")

    payload = result.output
    assert payload["truncated"] is True
    assert payload["output_chars"] == len(long_output)
    assert "已截断" in payload["output"]
    assert str(len(long_output)) in payload["output"], "必须标注原长"
    assert payload["output"].startswith("A" * 100)
    assert len(payload["output"]) < len(long_output)
    # 头注释说"上限 20000 字符"：截断点之前的内容必须完整保留
    assert payload["output"][:DETAIL_OUTPUT_CHARS] == long_output[:DETAIL_OUTPUT_CHARS]


async def test_detail_of_running_agent_says_it_is_progress_not_result():
    manager = FakeManager(
        items=[_item("sa5", status="running")],
        details={"sa5": _detail("sa5", status="running", output="已扫描 3/10 个文件")},
    )

    result = await SubagentListTool(manager).execute(agent_id="sa5")

    assert result.success is True
    assert result.output["status"] == "running"
    assert "仍在运行" in result.output["note"]
    assert "subagent_send" in result.output["note"]


async def test_unknown_agent_id_is_actionable_and_lists_available_ids():
    manager = FakeManager(items=[_item("sa1"), _item("sa2", status="running")])

    result = await SubagentListTool(manager).execute(agent_id="sa999")

    assert result.success is False
    message = result.error or ""
    assert "sa999" in message
    assert "sa1" in message and "sa2" in message, "错误里必须给出可用 id，省一次瞎猜"
    assert "subagent_list" in message
    assert "不带 agent_id" in message


async def test_unknown_agent_id_with_empty_session_explains_how_to_start():
    manager = FakeManager()

    result = await SubagentListTool(manager).execute(agent_id="sa1")

    assert result.success is False
    assert "还没有子智能体" in (result.error or "")


# ═══════════════════════════════════════════════════════════
# 2. 无 agent_id：旧行为不变
# ═══════════════════════════════════════════════════════════


async def test_without_agent_id_keeps_old_list_and_results_behaviour():
    manager = FakeManager(
        items=[_item("sa1"), _item("sa2", status="running")],
        details={"sa1": _detail("sa1", output="最终结果"),
                 "sa2": _detail("sa2", status="running", output="进度")},
    )

    result = await SubagentListTool(manager).execute()

    assert result.success is True
    payload = result.output
    assert payload["count"] == 2 and payload["running"] == 1
    assert [x["agent_id"] for x in payload["agents"]] == ["sa1", "sa2"]
    assert payload["results"] == {"sa1": "最终结果"}
    assert "results_note" not in payload
    assert "output_chars" not in payload and "truncated" not in payload
    assert "仍有子智能体在运行" in payload["note"]


async def test_blank_agent_id_still_uses_list_mode():
    manager = FakeManager(items=[_item("sa1")],
                          details={"sa1": _detail("sa1", output="x")})

    result = await SubagentListTool(manager).execute(agent_id="   ")

    assert result.success is True
    assert "agents" in result.output


async def test_list_long_results_are_still_capped():
    """列表模式继续受旧上限保护：不能因为详情能力上线就把父上下文撑爆。"""
    manager = FakeManager(
        items=[_item("sa1")],
        details={"sa1": _detail("sa1", output="B" * 9000)},
    )

    result = await SubagentListTool(manager).execute()

    payload = result.output
    assert payload["results_note"], "有截断就必须显式说明"
    assert len(payload["results"]["sa1"]) < 9000
    assert "截断" in payload["results"]["sa1"]


# ═══════════════════════════════════════════════════════════
# 3. 契约 / 上下文
# ═══════════════════════════════════════════════════════════


def test_schema_exposes_optional_agent_id():
    props = SubagentListTool.parameters["properties"]
    assert props["agent_id"]["type"] == "string"
    assert SubagentListTool.parameters["required"] == []
    assert "agent_id" in SubagentListTool.description


async def test_both_modes_require_a_running_task_context(monkeypatch):
    monkeypatch.setattr(runtime, "current_agent", lambda: None)
    manager = FakeManager(items=[_item("sa1")])

    listed = await SubagentListTool(manager).execute()
    detail = await SubagentListTool(manager).execute(agent_id="sa1")

    for result in (listed, detail):
        assert result.success is False
        assert "任务" in (result.error or "") and "Agent" in (result.error or "")
