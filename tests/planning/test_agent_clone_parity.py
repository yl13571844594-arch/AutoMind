"""会话克隆与 `__init__` 的属性一致性 —— 防止"只在克隆路径上炸"的漏项。

## 为什么需要这个文件

`AutoMindAgent.clone_for_session()` 用 ``object.__new__(type(self))`` 构造克隆，
**不经过 `__init__`**，属性靠一份**手工维护的清单**逐个复制。于是每往
`__init__` 里加一个实例属性，就有一次机会忘记同步到克隆 —— 而漏掉的后果是
一个只在"Web 端按会话跑任务"时才出现的 `AttributeError`。

v1.7.5 真实发生过：新增的 ``_last_checkpoint_at`` 没进克隆清单，
导致**每一次 Web 会话跑任务都在按步落盘时失败**（报
``'AutoMindAgent' object has no attribute '_last_checkpoint_at'``）。

而它躲过了当时的全部验证，原因很具体：**评测器每个用例都新建一个 agent**
（`AutoMindExecutor` 的文档写着"每个任务建一个独立的 AutoMindAgent"），
根本不走克隆路径；单测里也没有任何一条比对过克隆的完整性。
**测试路径与线上路径不同，是这类漏项最典型的藏身处。**

所以这里不写"某个属性存在吗"（那种用例永远追不上新增的字段），
而是**自动比对**：凡是 `__init__` 设过的属性，克隆上都必须也能取到。
以后再加字段，忘记同步就会在这里立刻红。
"""

from __future__ import annotations

import asyncio

import pytest

from automind.agent import AutoMindAgent
from automind.core.config import AgentConfig


@pytest.fixture
def base_agent(tmp_path):
    """一个真实的 Agent（不联网：没有 API Key 时 `_init_llm` 只记错误）。"""
    cfg = AgentConfig(project_root=str(tmp_path))
    cfg.execution.checkpoint_dir = str(tmp_path / "ckpts")
    agent = AutoMindAgent(cfg)
    yield agent
    asyncio.run(agent.close())


class TestCloneAttributeParity:
    def test_clone_has_every_attribute_the_init_sets(self, base_agent):
        """**核心断言**：`__init__` 设过的属性，克隆上必须一个都不少。

        这条用例的价值不在于它现在通过，而在于**以后新增字段时会失败**。
        """
        clone = base_agent.clone_for_session()
        base_attrs = set(vars(base_agent))
        clone_attrs = set(vars(clone))

        missing = sorted(base_attrs - clone_attrs)
        assert not missing, (
            "这些属性在 __init__ 里设置了，但 clone_for_session() 没有同步 —— "
            "任何走到克隆的任务都会 AttributeError：\n  "
            + "\n  ".join(missing)
            + "\n请在 clone_for_session() 里补上（共享的放进 _SHARED_ON_CLONE，"
              "独享的就地赋值）。")
        asyncio.run(clone.close())

    def test_every_base_attribute_is_readable_on_the_clone(self, base_agent):
        """逐个真正读一遍 —— 有些属性可能靠 `__getattr__` 之类间接提供，
        只比 `vars()` 键会漏判。"""
        clone = base_agent.clone_for_session()
        unreadable: list[str] = []
        for name in vars(base_agent):
            try:
                getattr(clone, name)
            except AttributeError:
                unreadable.append(name)
        asyncio.run(clone.close())
        assert not unreadable, f"克隆上读不到的属性：{unreadable}"


class TestCheckpointOnClone:
    def test_checkpoint_works_on_a_session_clone(self, base_agent):
        """按步落盘必须能在克隆上跑通 —— 这正是 v1.7.5 线上报错的那条路径。"""
        clone = base_agent.clone_for_session()
        _attach_stub_llm(clone)
        clone.session_id = "clone-ckpt-test"
        clone._agent_state.session_id = "clone-ckpt-test"

        async def go() -> str:
            try:
                return await clone._maybe_checkpoint(force=True)
            finally:
                await clone.close()

        cid = asyncio.run(go())
        assert cid, "克隆上按步落盘应当真的写出一个检查点"
        assert (base_agent.config.execution.checkpoint_dir
                and (clone.checkpoint_mgr.checkpoint_dir / f"{cid}.json").exists())

    def test_checkpoint_sidepath_never_raises(self, base_agent):
        """检查点是旁路：即使内部状态被破坏，也只能返回空串，不能抛异常。

        这条挡住的是"为了修一个字段缺失而引入新崩溃"的改法。
        """
        clone = base_agent.clone_for_session()
        _attach_stub_llm(clone)
        # 把节流状态与非关键依赖都去掉，模拟"克隆漏同步字段"的最坏情况
        for attr in ("_last_checkpoint_at", "_checkpoint_saves"):
            if attr in vars(clone):
                delattr(clone, attr)
        clone._agent_state = None          # 序列化必然失败

        async def go() -> str:
            try:
                return await clone._maybe_checkpoint(force=True)
            finally:
                await clone.close()

        assert asyncio.run(go()) == "", "旁路失败必须安静返回，而不是把任务弄挂"


def _attach_stub_llm(agent) -> None:
    """挂一个最小 LLM 替身：落盘要读 `llm.usage`，没配 Key 时它是 None。"""
    from automind.core.types import TokenUsage

    class _Stub:
        usage = TokenUsage()

    agent.llm = _Stub()
