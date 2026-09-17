"""Token 预算链路必须真的通电（v1.7.3）。

## 修的是什么

预算这条链路上每一环都写好了：``_pre_call`` 做准入 → 80% 推预警并压缩上下文 →
100% 拒绝本次调用 → ``ResourceManager`` 记账。**但记账那一环写错了字段名**：

    rm.tokens.tokens_used.prompt += ...        # TokenUsage 里没有 .prompt
    rm.tokens.tokens_used.completion += ...    # 也没有 .completion

``TokenUsage`` 的字段是 ``prompt_tokens`` / ``completion_tokens``。于是**每一次**
LLM 调用都在这里抛 ``AttributeError``，又被紧跟的 ``except Exception`` 吞成一条
warning。看起来"只是记不上账"，实际后果是整条链路空转：

* ``usage_fraction()`` 恒为 0 → 80% 预警永不触发；
* 预算驱动的上下文压缩（``_compress_context``）永不执行 → 长任务上下文无上限；
* 100% 拒绝永不生效 → 账单无上限；
* 每次调用还多一条 ``token_accounting_failed`` 告警刷屏。

最阴的地方是**测试是绿的**：``tests/test_reliability_fixes.py`` 直接往
``TokenUsage`` 上设了**正确**的字段名，于是它测的是"如果字段名对了会怎样"，
而生产代码走的是另一条路。这个文件改为**从真实的用量回调灌进去**，
让字段名写错这类错误无处可藏。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeLLM:
    """只提供 ``_attach_usage_sink`` 需要挂的那几个属性。"""

    def __init__(self) -> None:
        self.usage_sink = None
        self.pre_call_hook = None
        self.heartbeat_hook = None
        self.call_timeout = 0.0


def _agent(budget: int = 1000, events: list | None = None):
    """造一个**真 AutoMindAgent 实例**，只补用量与预算相关的属性。

    不整个构造（那要探测环境、建记忆库、连 LLM），但也不另造一个假类 ——
    本文件验的正是产品代码里那几行，用假类就验不到了。
    """
    from automind.agent import AutoMindAgent
    from automind.state.resource_manager import ResourceManager

    agent = object.__new__(AutoMindAgent)
    agent.llm = _FakeLLM()
    agent.resources = ResourceManager(token_budget=budget)
    agent.config = SimpleNamespace(
        execution=SimpleNamespace(llm_call_timeout_seconds=5.0))
    agent._usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "calls": 0}
    agent._budget_warned = False
    agent._next_compact_at = agent._BUDGET_WARN_AT
    agent.react_executor = None
    bucket = events if events is not None else []

    async def _sink(ev):
        bucket.append(ev)

    agent.event_sink = _sink
    agent._attach_usage_sink()
    return agent, bucket


async def _feed(agent, prompt: int, completion: int, calls: int = 1) -> None:
    """从**真实的用量回调**灌数据（而不是直接改对象字段）。"""
    for _ in range(calls):
        await agent.llm.usage_sink({
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
        })


# ═══════════════════════════════════════════════════════════
# 1. 记账：字段名写错就会在这里死掉
# ═══════════════════════════════════════════════════════════


async def test_usage_sink_actually_reaches_the_budget_counter():
    agent, _ = _agent(budget=1000)

    await _feed(agent, prompt=120, completion=80)

    used = agent.resources.tokens.tokens_used
    assert used.prompt_tokens == 120, "用量没有记进 ResourceManager（字段名又写错了？）"
    assert used.completion_tokens == 80
    assert agent.resources.tokens.usage_fraction() == pytest.approx(0.2)


async def test_usage_sink_accumulates_across_calls():
    agent, _ = _agent(budget=1000)

    await _feed(agent, prompt=100, completion=50, calls=3)

    assert agent.resources.tokens.tokens_used.total == 450
    assert agent._usage_total["calls"] == 3, "界面用的累计口径也不能丢"


async def test_no_accounting_failure_warning_is_logged(caplog):
    """记账成功时不许再刷 token_accounting_failed 告警。"""
    import logging

    agent, _ = _agent(budget=1000)
    with caplog.at_level(logging.WARNING, logger="automind.agent"):
        await _feed(agent, prompt=10, completion=5)

    assert not [r for r in caplog.records if "token_accounting_failed" in r.message]


# ═══════════════════════════════════════════════════════════
# 2. 链路：预警、压缩、拒绝都要真的发生
# ═══════════════════════════════════════════════════════════


async def test_budget_warning_and_compaction_fire_at_the_threshold(monkeypatch):
    """这条是本次修复的核心：账记上了，预警与压缩才可能触发。"""
    agent, events = _agent(budget=1000)
    compacted: list[int] = []

    async def _fake_compact():
        compacted.append(1)

    monkeypatch.setattr(agent, "_compress_context", _fake_compact)
    await _feed(agent, prompt=700, completion=150)      # 85% > 80%

    await agent.llm.pre_call_hook()

    assert any(e.get("type") == "budget_warning" for e in events), \
        "用量过 80% 必须推 budget_warning"
    assert compacted, "过阈值必须真的去压缩上下文（这是省钱的那一步）"


async def test_budget_warning_is_not_repeated_every_call(monkeypatch):
    agent, events = _agent(budget=1000)

    async def _noop():
        return None

    monkeypatch.setattr(agent, "_compress_context", _noop)
    await _feed(agent, prompt=850, completion=0)
    await agent.llm.pre_call_hook()
    await agent.llm.pre_call_hook()
    await agent.llm.pre_call_hook()

    warnings = [e for e in events if e.get("type") == "budget_warning"]
    assert len(warnings) == 1, "预警不该每次调用都刷一遍"


async def test_budget_exceeded_refuses_the_call(monkeypatch, caplog):
    """预算用完必须**拒绝本次调用**（RuntimeError 由 LLM 层抛出并中止）。"""
    import logging

    agent, events = _agent(budget=1000)

    async def _noop():
        return None

    monkeypatch.setattr(agent, "_compress_context", _noop)
    await _feed(agent, prompt=900, completion=200)      # 110%

    with caplog.at_level(logging.ERROR, logger="automind.agent"), pytest.raises(RuntimeError):
        await agent.llm.pre_call_hook()

    assert any(e.get("type") == "budget_exceeded" for e in events)
    assert any("token_budget_exhausted" in r.message for r in caplog.records), \
        "超额必须留下 ERROR 级日志，运维要能从日志里发现账单失控"


async def test_normal_usage_does_not_warn(monkeypatch):
    agent, events = _agent(budget=1000)

    async def _noop():
        return None

    monkeypatch.setattr(agent, "_compress_context", _noop)
    await _feed(agent, prompt=50, completion=10)

    await agent.llm.pre_call_hook()

    assert not [e for e in events if e.get("type", "").startswith("budget_")]


# ═══════════════════════════════════════════════════════════
# 3. 静态护栏：这个 bug 形态不许再出现
# ═══════════════════════════════════════════════════════════


def test_no_place_writes_the_wrong_tokenusage_fields():
    """任何人再写 ``tokens_used.prompt`` / ``.completion`` 就直接红。

    动态用例只能证明"我测的这条路径是对的"，而这类字段名错误**恰恰出现在
    没被测到的那条路径上**（原来那个 bug 就是这样活到 v1.7.2 的）。所以再加
    一道静态扫描。

    用 **AST** 而不是正则扫文本：本仓库的注释里会引用这个错误写法来解释
    "为什么不能这么写"（本文件与 agent.py 的注释就是），正则会把说明文字
    当成违规。AST 只看真正的属性访问表达式。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    bad: list[str] = []
    for path in sorted((root / "automind").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr in ("prompt", "completion")
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "tokens_used"):
                bad.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not bad, (
        "TokenUsage 的字段名是 prompt_tokens / completion_tokens；"
        "写成 .prompt/.completion 会让预算链路整体空转（预警/压缩/拒绝全部失效）：\n"
        + "\n".join(bad))
