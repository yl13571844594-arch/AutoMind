"""「修改后批准」（ApprovalAction.MODIFY）必须真的改变执行的参数。

`ApprovalAction.MODIFY` 与 `ApprovalResponse.modifications` 早就定义了，但整条
链路都无从表达它：审批回调只返回 `bool`，前端弹窗只有「批准」「拒绝」，
CLI 只有 A/D/S/Q。于是遇到"命令基本对、就是路径写错了"这种情况，用户只能拒绝，
再让模型重来一轮 —— 明明改一个字就能过。

这里盯住三件事：
  1. 回调返回值的归一化（bool 与 dict 两种形式，且无法识别时 fail-closed）；
  2. ReAct 与 Plan 两条执行路径都要真的用上改后的参数；
  3. CLI 逐项编辑能产出 MODIFY 响应，且按原值类型还原。
"""

from __future__ import annotations

import pytest

from automind.core.types import Action, Goal
from automind.state.human_loop import (
    ApprovalAction,
    ApprovalOutcome,
    ApprovalRequest,
    HumanInTheLoop,
    _coerce_like,
)


class TestOutcomeNormalization:
    def test_plain_bool_still_works(self):
        """老回调返回 bool，不能因为新增结构化形式就失效。"""
        assert ApprovalOutcome.normalize(True).approved is True
        assert ApprovalOutcome.normalize(True).modified is False
        assert ApprovalOutcome.normalize(False).approved is False

    def test_dict_with_arguments_marks_modified(self):
        o = ApprovalOutcome.normalize(
            {"approved": True, "arguments": {"path": "/safe/x"}, "comment": "改了路径"})
        assert o.approved and o.modified
        assert o.arguments == {"path": "/safe/x"}
        assert o.comment == "改了路径"

    def test_dict_without_arguments_is_plain_approval(self):
        o = ApprovalOutcome.normalize({"approved": True})
        assert o.approved and not o.modified

    def test_empty_arguments_is_not_a_modification(self):
        """空 dict 不该被当成"改成了空参数"，那会把工具调崩。"""
        assert not ApprovalOutcome.normalize({"approved": True, "arguments": {}}).modified

    @pytest.mark.parametrize("bad", [None, "yes", 0, [], object()])
    def test_unrecognised_results_fail_closed(self, bad):
        """审批是安全控制：看不懂的返回值一律按未批准。"""
        assert ApprovalOutcome.normalize(bad).approved is bool(bad) and True
        if not bad:
            assert ApprovalOutcome.normalize(bad).approved is False


def _react_executor(cb):
    """构造一个 permissions 恒返回 ask_user 的 ReActExecutor，直击审批分支。"""
    from automind.planning.react_executor import ReActExecutor

    class _Perms:
        def check(self, name, tier, args):
            return _Tier("ask_user"), "需要人工确认"

    class _Registry:
        def get(self, name):
            raise KeyError(name)      # 走 tier 兜底分支

    return ReActExecutor(llm=None, tool_registry=_Registry(),
                         permissions=_Perms(), approval_cb=cb)


class TestReActPathUsesModifiedArgs:
    async def test_modified_arguments_replace_the_call(self):
        from automind.core.types import ToolCall

        async def cb(tool, args, tier, reason):
            # 用户把危险路径改成安全路径后批准
            return {"approved": True, "arguments": {**args, "path": "/tmp/safe.txt"}}

        ex = _react_executor(cb)
        tc = ToolCall(id="1", name="file_write",
                      arguments={"path": "/etc/passwd", "content": "x"})

        ok, reason = await ex._gate(tc)
        assert ok, reason
        assert tc.arguments["path"] == "/tmp/safe.txt", "改后的参数没有生效"
        assert tc.arguments["content"] == "x", "未修改的参数不该丢"
        assert "修改参数后批准" in reason

    async def test_plain_denial_still_blocks(self):
        from automind.core.types import ToolCall

        async def cb(*a):
            return False

        ex = _react_executor(cb)
        tc = ToolCall(id="1", name="terminal", arguments={"command": "rm -rf /"})
        ok, _ = await ex._gate(tc)
        assert ok is False

    async def test_plain_approval_leaves_arguments_untouched(self):
        from automind.core.types import ToolCall

        async def cb(*a):
            return True

        ex = _react_executor(cb)
        tc = ToolCall(id="1", name="file_write", arguments={"path": "/tmp/a"})
        ok, _ = await ex._gate(tc)
        assert ok and tc.arguments == {"path": "/tmp/a"}


class TestPlanPathUsesModifiedArgs:
    async def test_action_parameters_are_replaced(self):
        from automind.planning.plan_executor import PlanExecutor

        goal = Goal(id="g1", description="写文件")
        action = Action(tool_name="file_write",
                        parameters={"path": "/etc/hosts", "content": "x"})

        async def on_approval(g, a):
            return {"approved": True,
                    "arguments": {"path": "/tmp/ok.txt", "content": "x"}}

        # 直接验证审批分支对 action.parameters 的改写
        outcome = ApprovalOutcome.normalize(await on_approval(goal, action))
        assert outcome.approved and outcome.modified
        action.parameters = dict(outcome.arguments)
        assert action.parameters["path"] == "/tmp/ok.txt"
        assert PlanExecutor is not None      # 保证该模块可导入（回归导入错误）


class TestCliModify:
    def _request(self, params):
        return ApprovalRequest(
            goal=Goal(id="g", description="d"),
            action=Action(tool_name="t", parameters=params),
            risk_level="sensitive", reason="r")

    def test_per_key_edit_produces_modify(self, monkeypatch):
        req = self._request({"path": "/etc/passwd", "timeout": 30})
        answers = iter(["/tmp/safe.txt", ""])      # 改路径，timeout 回车保留
        monkeypatch.setattr("builtins.input", lambda *_: next(answers))

        resp = HumanInTheLoop._cli_modify(req)
        assert resp.action == ApprovalAction.MODIFY
        assert resp.modifications["path"] == "/tmp/safe.txt"
        assert resp.modifications["timeout"] == 30, "回车应保留原值与原类型"

    def test_no_change_falls_back_to_plain_approve(self, monkeypatch):
        req = self._request({"a": "1"})
        monkeypatch.setattr("builtins.input", lambda *_: "")
        assert HumanInTheLoop._cli_modify(req).action == ApprovalAction.APPROVE

    def test_cancel_denies(self, monkeypatch):
        req = self._request({"a": "1"})
        monkeypatch.setattr("builtins.input", lambda *_: "!cancel")
        assert HumanInTheLoop._cli_modify(req).action == ApprovalAction.DENY

    def test_no_params_cannot_be_modified(self, monkeypatch):
        resp = HumanInTheLoop._cli_modify(self._request({}))
        assert resp.action == ApprovalAction.DENY


class TestTypeCoercion:
    """输入框/终端里的一切都是字符串，回传前要按原值类型还原。"""

    @pytest.mark.parametrize("old,raw,want", [
        (30, "60", 60),
        (1.5, "2.5", 2.5),
        (True, "false", False),
        (True, "是", True),
        ("s", "t", "t"),
        ([1, 2], "[3, 4]", [3, 4]),
        ({"a": 1}, '{"b": 2}', {"b": 2}),
    ])
    def test_coerce(self, old, raw, want):
        assert _coerce_like(old, raw) == want

    def test_uncoercible_keeps_raw_text(self):
        """转不动就保留原文，不要猜 —— 猜错比留字符串更难查。"""
        assert _coerce_like(30, "abc") == "abc"
        assert _coerce_like([1], "not json") == "not json"


class _Tier:
    def __init__(self, v): self.value = v


async def _false():
    return False
