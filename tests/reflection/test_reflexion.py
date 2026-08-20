"""Reflexion —— "从失败里学到东西，下次别再犯"。

这条链路坏掉时最隐蔽：反思照样生成、界面照样不报错，只是**教训再也检索不回来**，
Agent 每次都从零开始犯同样的错。所以重点测"存得进、取得出、拼得成提示词"。
"""

from __future__ import annotations

import pytest

from automind.reflection.reflexion import Reflection, ReflexionEngine


class FakeLLM:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def generate(self, messages, tools=None, stop=None):
        self.calls += 1
        return type("R", (), {"text": self.text})()


class FakeMemory:
    """够用的长期记忆替身：记录写入、按子串匹配检索。"""

    def __init__(self, fail_add=False, fail_search=False):
        self.added: list[tuple[list[str], list[dict]]] = []
        self.fail_add = fail_add
        self.fail_search = fail_search

    async def add(self, documents, metadatas=None, **kw):
        if self.fail_add:
            raise RuntimeError("向量库写入失败")
        self.added.append((documents, metadatas or []))

    async def search(self, query, k=3, **kw):
        if self.fail_search:
            raise RuntimeError("向量库检索失败")
        return [
            {"document": d, "metadata": m}
            for docs, metas in self.added
            for d, m in zip(docs, metas)
            if query.lower() in str(m.get("task", "")).lower()
        ][:k]


class TestReflect:
    async def test_without_llm_still_produces_a_reflection(self):
        """没有 LLM 也要能反思 —— 否则本地模型不可用时这条链路直接断掉。"""
        eng = ReflexionEngine(llm=None)
        r = await eng.reflect("写个脚本", "failure", "第 2 步报错")
        assert isinstance(r, Reflection)
        assert r.task == "写个脚本" and r.outcome == "failure"
        assert r.timestamp > 0, "没有时间戳的话，后续无法按时间排序/淘汰"
        assert eng.reflections == [r]

    async def test_reflection_is_written_to_long_term_memory(self):
        mem = FakeMemory()
        eng = ReflexionEngine(llm=None, long_term_memory=mem)
        await eng.reflect("导出报表", "failure", "trace")

        assert len(mem.added) == 1, "反思没有写进长期记忆，等于没学到"
        _docs, metas = mem.added[0]
        assert metas[0]["type"] == "reflection"
        assert metas[0]["task"] == "导出报表"
        assert metas[0]["outcome"] == "failure"

    async def test_memory_failure_does_not_break_the_task(self):
        """向量库挂了不该让整个任务失败 —— 反思是增益，不是主流程。"""
        eng = ReflexionEngine(llm=None, long_term_memory=FakeMemory(fail_add=True))
        r = await eng.reflect("任务", "failure", "trace")
        assert isinstance(r, Reflection)
        assert len(eng.reflections) == 1

    async def test_llm_path_is_used_when_available(self):
        llm = FakeLLM('{"self_criticism": "路径写死了", '
                      '"mistakes": ["硬编码路径"], "lessons": ["先确认工作目录"]}')
        eng = ReflexionEngine(llm=llm)
        r = await eng.reflect("任务", "failure", "trace")
        assert llm.calls == 1
        assert "路径" in r.self_criticism or r.self_criticism

    async def test_malformed_llm_output_falls_back(self):
        """LLM 返回的不是 JSON 时要降级，而不是把任务带崩。"""
        eng = ReflexionEngine(llm=FakeLLM("完全不是 JSON"))
        r = await eng.reflect("任务", "failure", "trace")
        assert isinstance(r, Reflection) and r.task == "任务"


class TestRetrieve:
    async def test_keyword_search_without_memory(self):
        """没有向量库时退化为关键词匹配，而不是永远返回空。"""
        eng = ReflexionEngine(llm=None)
        await eng.reflect("导出 Excel 报表", "failure", "trace")
        await eng.reflect("清理日志文件", "success", "trace")

        hits = await eng.retrieve_relevant("Excel", k=3)
        assert hits, "关键词能对上却什么都没检索到"
        assert any("Excel" in h.task for h in hits)

    async def test_respects_k(self):
        eng = ReflexionEngine(llm=None)
        for i in range(6):
            await eng.reflect(f"报表任务 {i}", "failure", "trace")
        assert len(await eng.retrieve_relevant("报表", k=2)) <= 2

    async def test_search_failure_degrades_gracefully(self):
        eng = ReflexionEngine(llm=None,
                              long_term_memory=FakeMemory(fail_search=True))
        await eng.reflect("任务", "failure", "trace")
        assert isinstance(await eng.retrieve_relevant("任务"), list)

    async def test_no_reflections_returns_empty(self):
        assert await ReflexionEngine(llm=None).retrieve_relevant("任何") == []


class TestLessonsPrompt:
    def test_empty_when_nothing_learned(self):
        """没有经验时不该往提示词里塞一段空壳，白占 token。"""
        assert ReflexionEngine(llm=None).get_lessons_prompt("任务") == ""

    async def test_includes_past_lessons(self):
        eng = ReflexionEngine(llm=None)
        eng.reflections.append(Reflection(
            task="导出 Excel 报表", outcome="failure",
            self_criticism="路径写死了",
            mistakes=["硬编码路径"], lessons=["先确认工作目录"],
            timestamp=1.0))

        prompt = eng.get_lessons_prompt("导出 Excel")
        assert prompt, "有历史教训却没拼进提示词，等于白学"
        assert "先确认工作目录" in prompt

    @pytest.mark.parametrize("task", ["", "   "])
    def test_blank_task_does_not_crash(self, task):
        assert isinstance(ReflexionEngine(llm=None).get_lessons_prompt(task), str)
