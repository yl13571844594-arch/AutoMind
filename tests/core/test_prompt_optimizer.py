"""问题优化模块单元测试 —— 模糊识别 / 规则结构 / LLM JSON 与各类兜底。

这些用例锁住的是「✨ 优化按钮永远可用」这条产品承诺：模型未配置、调用超时、
返回非 JSON、甚至只是把原问题复读一遍，都必须落到本地规则版本上，并且结果
字段能被前端直接渲染（original/optimized/changed/clear_enough/by/notes/questions）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from automind.core.prompt_optimizer import (
    MAX_INPUT_CHARS,
    MAX_LIST_ITEM_CHARS,
    MAX_OUTPUT_CHARS,
    OptimizedPrompt,
    looks_vague,
    optimize_prompt,
    optimize_with_llm,
    rule_based_optimize,
)

#: 一个足够长、且带路径/标点/数字的清晰需求（不应触发模型调用）。
CLEAR_LONG = (
    "请把 web/src/components/chat/ChatPanel.tsx 里的发送按钮改成蓝色，"
    "并在点击后 200ms 内显示加载动画，最后用 tsc --noEmit 验证类型通过。"
)

#: 一个很短、但给出了具体文件路径与数字的需求（不该被判成纯泛化短语）。
CLEAR_SHORT = "修复 automind/core/json_utils.py 里第 78 行的截断判定。"


class _FakeLLM:
    """最小 LLM 替身：记录调用，按配置返回文本 / 抛异常 / 故意超时。"""

    def __init__(self, payload="", *, raises: Exception | None = None, delay: float = 0.0):
        self.payload = payload
        self.raises = raises
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def generate(self, messages, tools=None, stop=None):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        # 真实 provider 返回 LLMResponse（带 .text），这里保持同形。
        return SimpleNamespace(text=self.payload)


def _json_payload(**overrides) -> str:
    import json

    data = {
        "optimized": "目标：修复启动报错\n\n请先复述理解，再列出必需信息与产出，最后标注假设。",
        "changes": ["补充了目标", "要求先复述理解"],
        "questions": ["是哪个入口启动时报错？"],
        "clear_enough": False,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class TestLooksVague:
    def test_empty_and_blank_are_vague(self):
        assert looks_vague("") is True
        assert looks_vague("   \n\t ") is True

    def test_too_short_is_vague(self):
        assert looks_vague("帮我看看") is True
        assert looks_vague("继续") is True
        assert looks_vague("hi") is True

    def test_generic_phrases_are_vague(self):
        assert looks_vague("优化一下") is True
        assert looks_vague("报错了") is True
        assert looks_vague("怎么办") is True
        assert looks_vague("帮我弄一下这个") is True

    def test_short_without_structure_is_vague(self):
        # 13 个字以上、没有标点、也没给路径/数字/扩展名 → 仍然说不清"对什么做"。
        assert looks_vague("把我那个脚本再整理整理") is True

    def test_short_with_path_is_not_vague(self):
        assert looks_vague(CLEAR_SHORT) is False

    def test_long_specific_is_not_vague(self):
        assert len(CLEAR_LONG) >= 80
        assert looks_vague(CLEAR_LONG) is False

    def test_url_is_structure(self):
        assert looks_vague("打开 https://example.com/docs 看看接口定义") is False

    def test_numbers_count_as_structure(self):
        # 短、无标点，但给了数字（版本号）→ 不套模板。
        assert looks_vague("把 python 版本升到 312") is False


class TestRuleBased:
    def test_vague_gets_four_step_structure(self):
        r = rule_based_optimize("帮我看看这个报错")
        assert isinstance(r, OptimizedPrompt)
        assert r.changed is True
        assert r.clear_enough is False
        assert r.by == "rule"
        # 原文必须原样保留在「目标：」之后，不能丢用户信息。
        assert r.optimized.startswith("目标：帮我看看这个报错")
        for keyword in ("复述", "验收", "假设", "最多 3 个问题"):
            assert keyword in r.optimized
        assert r.notes
        assert r.questions == []

    def test_clear_is_returned_unchanged(self):
        r = rule_based_optimize(CLEAR_LONG)
        assert r.optimized == CLEAR_LONG
        assert r.changed is False
        assert r.clear_enough is True
        assert r.notes == ["问题已经比较清楚，可直接发送"]

    def test_to_dict_shape(self):
        data = rule_based_optimize(CLEAR_SHORT).to_dict()
        assert set(data) == {
            "original", "optimized", "changed", "clear_enough", "by", "notes", "questions",
        }
        assert data["original"] == CLEAR_SHORT
        assert isinstance(data["notes"], list)
        assert isinstance(data["questions"], list)


class TestOptimizeWithLLM:
    async def test_parses_plain_json(self):
        llm = _FakeLLM(_json_payload())
        r = await optimize_with_llm(llm, "帮我看看这个报错")
        assert r is not None
        assert r.by == "llm"
        assert r.changed is True
        assert r.clear_enough is False
        assert r.notes == ["补充了目标", "要求先复述理解"]
        assert r.questions == ["是哪个入口启动时报错？"]
        # 提示词里带上原问题，模型才知道要改什么。
        assert "帮我看看这个报错" in llm.calls[0][0]["content"]

    async def test_parses_json_in_code_fence(self):
        payload = "好的，结果如下：\n```json\n" + _json_payload() + "\n```"
        r = await optimize_with_llm(_FakeLLM(payload), "帮我看看这个报错")
        assert r is not None and r.by == "llm"

    async def test_accepts_plain_string_response(self):
        class _RawStringLLM:
            async def generate(self, messages, tools=None, stop=None):
                return _json_payload()

        r = await optimize_with_llm(_RawStringLLM(), "帮我看看这个报错")
        assert r is not None and r.changed is True

    async def test_clear_enough_requires_no_change(self):
        # 输入本身不模糊，模型也认为清晰 → 原样返回并标记 clear_enough。
        payload = _json_payload(optimized=CLEAR_SHORT, changes=[], clear_enough=True)
        r = await optimize_with_llm(_FakeLLM(payload), CLEAR_SHORT)
        assert r is not None
        assert r.changed is False
        assert r.clear_enough is True
        assert r.notes == ["问题已经比较清楚，可直接发送"]

    async def test_bad_json_returns_none(self):
        assert await optimize_with_llm(_FakeLLM("我觉得这句话还行"), "帮我看看") is None

    async def test_empty_response_returns_none(self):
        assert await optimize_with_llm(_FakeLLM("   "), "帮我看看") is None
        assert await optimize_with_llm(_FakeLLM(None), "帮我看看") is None

    async def test_exception_returns_none(self):
        llm = _FakeLLM(raises=RuntimeError("provider down"))
        assert await optimize_with_llm(llm, "帮我看看") is None

    async def test_timeout_returns_none(self):
        # 30 秒默认超时太长，用例里压到 0.01s；模型故意睡 0.2s。
        llm = _FakeLLM(_json_payload(), delay=0.2)
        assert await optimize_with_llm(llm, "帮我看看", timeout=0.01) is None

    async def test_unchanged_vague_result_is_invalid(self):
        # 模型对模糊问题原样返回 = 没有改写，应交给规则模板而不是照抄。
        payload = _json_payload(optimized="帮我看看这个报错")
        assert await optimize_with_llm(_FakeLLM(payload), "帮我看看这个报错") is None

    async def test_missing_optimized_is_invalid(self):
        payload = _json_payload()
        payload = payload.replace('"optimized"', '"whatever"')
        assert await optimize_with_llm(_FakeLLM(payload), "帮我看看这个报错") is None

    async def test_too_long_result_is_invalid(self):
        payload = _json_payload(optimized="改" * (MAX_OUTPUT_CHARS + 1))
        assert await optimize_with_llm(_FakeLLM(payload), "帮我看看这个报错") is None

    async def test_notes_and_questions_are_capped(self):
        payload = _json_payload(
            changes=["改" * (MAX_LIST_ITEM_CHARS + 50)] * 8,
            questions=[f"问题{i}" for i in range(6)],
        )
        r = await optimize_with_llm(_FakeLLM(payload), "帮我看看这个报错")
        assert r is not None
        assert len(r.notes) == 5
        assert all(len(n) <= MAX_LIST_ITEM_CHARS + 1 for n in r.notes)
        assert all(n.endswith("…") for n in r.notes)
        assert len(r.questions) == 3

    async def test_ignores_non_list_fields(self):
        payload = _json_payload(changes="不是列表", questions=None)
        r = await optimize_with_llm(_FakeLLM(payload), "帮我看看这个报错")
        assert r is not None
        # changed=True 时不会补"已经很清楚"的默认提示，非列表字段直接忽略。
        assert r.notes == []
        assert r.questions == []


class TestOptimizePrompt:
    async def test_strips_whitespace(self):
        data = await optimize_prompt(None, "  帮我看看这个报错  ")
        assert data["original"] == "帮我看看这个报错"

    async def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            await optimize_prompt(None, "   ")

    async def test_too_long_raises_value_error(self):
        with pytest.raises(ValueError):
            await optimize_prompt(None, "改" * (MAX_INPUT_CHARS + 1))

    async def test_clear_long_skips_llm(self):
        llm = _FakeLLM(_json_payload())
        data = await optimize_prompt(llm, CLEAR_LONG)
        assert data["by"] == "rule"
        assert data["changed"] is False
        assert data["clear_enough"] is True
        assert llm.calls == []          # 清晰问题不花一次模型调用

    async def test_clear_short_without_llm_returns_rule(self):
        # 不足 80 字的清晰问题仍会尝试模型；没有模型时规则版原样返回。
        data = await optimize_prompt(None, CLEAR_SHORT)
        assert data["by"] == "rule"
        assert data["clear_enough"] is True
        assert data["optimized"] == CLEAR_SHORT

    async def test_llm_success_is_used(self):
        llm = _FakeLLM(_json_payload())
        data = await optimize_prompt(llm, "帮我看看这个报错", "coding")
        assert data["by"] == "llm"
        assert data["changed"] is True
        assert len(llm.calls) == 1
        # interaction 只作为改写上下文传给模型，不改变返回结构。
        assert "coding" in llm.calls[0][0]["content"]

    async def test_llm_bad_json_falls_back_to_rule(self):
        llm = _FakeLLM("这不是 JSON")
        data = await optimize_prompt(llm, "帮我看看这个报错")
        assert data["by"] == "rule"
        assert data["changed"] is True
        assert data["optimized"].startswith("目标：帮我看看这个报错")

    async def test_llm_exception_falls_back_to_rule(self):
        llm = _FakeLLM(raises=RuntimeError("boom"))
        data = await optimize_prompt(llm, "帮我看看这个报错")
        assert data["by"] == "rule"
        assert data["changed"] is True

    async def test_no_llm_falls_back_to_rule(self):
        data = await optimize_prompt(None, "帮我看看这个报错")
        assert data["by"] == "rule"
        assert data["changed"] is True
        assert "四步" in data["optimized"] or "请先按下面四步处理" in data["optimized"]
