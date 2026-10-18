"""v1.11.10 提示缓存用量采集 —— 命中率必须"算得出、算得对、不造假"。

背景
----
``LLMResponse`` 早就有 ``cached_tokens`` 字段，但**没有任何 provider 给它赋值**：
DeepSeek 的 ``prompt_cache_hit_tokens``、OpenAI/GLM 的
``prompt_tokens_details.cached_tokens``、Anthropic 的
``cache_read_input_tokens`` 全被丢掉了。界面于是既看不到命中率，也无从判断
"优化前缀顺序到底有没有用"。

这个文件盯三件事：
1. 各家口径归一化正确（尤其 Anthropic 的 input 不含缓存读取、DeepSeek 的
   hit+miss 才是总输入）；
2. 非流式与流式都能把缓存用量带到 ``usage_sink``；
3. "没上报缓存"必须返回 None（显示"—"），不能拿 0 冒充满分失败的假数字。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from automind.core.config import LLMProviderConfig
from automind.core.llm import LLMBackend, OpenAIProvider, extract_cache_usage
from automind.core.types import LLMResponse, TokenUsage


class TestExtractCacheUsage:
    def test_deepseek_hit_and_miss(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                total_tokens=120,
                                prompt_cache_hit_tokens=80,
                                prompt_cache_miss_tokens=20)
        assert extract_cache_usage(usage) == (80, 0, 100, True)

    def test_deepseek_uses_hit_plus_miss_as_total(self):
        """个别中转的 prompt_tokens 口径会漂，hit+miss 更可信。"""
        usage = {"prompt_tokens": 999, "prompt_cache_hit_tokens": 80,
                 "prompt_cache_miss_tokens": 20}
        assert extract_cache_usage(usage)[2] == 100

    def test_openai_style_details(self):
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=5,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=900))
        assert extract_cache_usage(usage) == (900, 0, 1000, True)

    def test_glm_style_dict_details(self):
        """智谱 GLM 走 OpenAI 兼容协议，details 以 dict 形态出现也要认。"""
        usage = {"prompt_tokens": 1000,
                 "prompt_tokens_details": {"cached_tokens": 640}}
        assert extract_cache_usage(usage) == (640, 0, 1000, True)

    def test_kimi_top_level_cached_tokens(self):
        usage = {"prompt_tokens": 50, "cached_tokens": 30}
        assert extract_cache_usage(usage) == (30, 0, 50, True)

    def test_anthropic_cache_read_is_not_part_of_input_tokens(self):
        """Anthropic 的 input_tokens 只算未命中部分，总输入要补 read+creation。"""
        usage = SimpleNamespace(input_tokens=20, output_tokens=5,
                                cache_read_input_tokens=80,
                                cache_creation_input_tokens=10)
        assert extract_cache_usage(usage) == (80, 10, 110, True)

    def test_google_cached_content(self):
        usage = {"prompt_token_count": 100, "cached_content_token_count": 40}
        assert extract_cache_usage(usage) == (40, 0, 100, True)

    def test_no_cache_fields_is_not_reported(self):
        """Ollama / 不报缓存的中转：全 0 且 reported=False（显示"—"）。"""
        assert extract_cache_usage({"prompt_tokens": 100}) == (0, 0, 100, False)

    def test_none_usage_is_safe(self):
        assert extract_cache_usage(None) == (0, 0, 0, False)

    def test_reported_zero_is_distinguishable(self):
        """明确返回 cached_tokens=0 的提供商算"上报了"，界面显示 0% 而不是"—"。"""
        usage = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
        assert extract_cache_usage(usage) == (0, 0, 100, True)


class TestResponseAndUsageTotals:
    def test_cache_savings_uses_total_input(self):
        """OpenAI 口径的 prompt_tokens 已经含缓存命中，分母不能再加一次。"""
        r = LLMResponse(text="", prompt_tokens=100, cached_tokens=80,
                        input_tokens=100)
        assert r.cache_savings_pct == 80.0   # 旧口径 80/(100+80)=44.4

    def test_cache_savings_without_input_falls_back_to_prompt(self):
        r = LLMResponse(text="", prompt_tokens=100, cached_tokens=20)
        assert r.cache_savings_pct == 20.0

    def test_no_data_is_zero(self):
        assert LLMResponse(text="").cache_savings_pct == 0.0

    def test_token_usage_accumulates_cache(self):
        u = TokenUsage()
        u.add(LLMResponse(text="", prompt_tokens=100, completion_tokens=10,
                          cached_tokens=80, cache_reported=True))
        u.add(LLMResponse(text="", prompt_tokens=200, completion_tokens=10,
                          cached_tokens=0, cache_reported=True))
        assert u.calls == 2
        assert u.cache_hit_calls == 1
        assert u.cache_reported_calls == 2
        assert u.input_tokens == 300
        assert u.peak_prompt_tokens == 200
        assert u.cache_hit_rate == 26.7      # 80 / 300

    def test_token_usage_without_cache_reporting_has_no_rate(self):
        """提供商不报缓存 → 命中率是 None，不是 0（0 会让人以为真的没命中）。"""
        u = TokenUsage()
        u.add(LLMResponse(text="", prompt_tokens=100, completion_tokens=10))
        assert u.cache_reported_calls == 0
        assert u.cache_hit_rate is None
        assert u.total == 110                 # 预算口径不受新字段影响


class _SinkBackend(LLMBackend):
    """最小后端：只用于直接验证 ``_report_usage`` 的 payload。"""

    def __init__(self) -> None:
        super().__init__(LLMProviderConfig(provider="deepseek", model="m",
                                           api_key="sk-test"))

    async def generate(self, messages, tools=None, stop=None):
        return LLMResponse(text="")

    async def generate_stream(self, messages, tools=None):
        if False:                              # pragma: no cover - 只为满足抽象方法
            yield ""


async def test_report_usage_carries_cache_fields():
    p = _SinkBackend()
    seen: list[dict] = []

    async def sink(usage: dict) -> None:
        seen.append(usage)

    p.usage_sink = sink
    await p._report_usage(100, 20, cached=80, cache_creation=0,
                          input_total=100, cache_reported=True)
    assert seen[0]["cached_tokens"] == 80
    assert seen[0]["input_tokens"] == 100
    assert seen[0]["cache_reported"] is True
    # 原有字段与语义不变
    assert seen[0]["prompt_tokens"] == 100 and seen[0]["total_tokens"] == 120


def _provider() -> OpenAIProvider:
    cfg = LLMProviderConfig(provider="deepseek", model="deepseek-chat",
                            api_key="sk-test",
                            api_base="https://api.deepseek.com/v1")
    return OpenAIProvider(cfg)


def _completion(usage, content: str = "hi"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")

    class _Resp:
        def __init__(self) -> None:
            self.choices = [choice]
            self.usage = usage

        def model_dump(self) -> dict:
            return {"id": "fake"}

    return _Resp()


async def test_openai_generate_fills_cache_fields():
    p = _provider()
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                            total_tokens=120,
                            prompt_cache_hit_tokens=80,
                            prompt_cache_miss_tokens=20)

    async def fake(_kwargs):
        return _completion(usage)

    p._create_with_retry = fake          # type: ignore[assignment]
    r = await p.generate([{"role": "user", "content": "hi"}])
    assert r.cached_tokens == 80
    assert r.input_tokens == 100
    assert r.cache_reported is True
    assert r.cache_savings_pct == 80.0


async def test_openai_stream_marker_and_sink_carry_cache():
    """流式也要带缓存口径：对话模式的命中率完全依赖这个标记。"""
    p = _provider()
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=30,
                            total_tokens=1030,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=900))

    class _Chunk:
        def __init__(self, content=None, usage=None) -> None:
            self.choices = ([SimpleNamespace(delta=SimpleNamespace(content=content))]
                            if content else [])
            self.usage = usage

    async def fake(_kwargs):
        async def gen():
            yield _Chunk("部分")
            yield _Chunk("回答")
            yield _Chunk(usage=usage)
        return gen()

    p._create_with_retry = fake          # type: ignore[assignment]
    seen: list[dict] = []

    async def sink(u: dict) -> None:
        seen.append(u)

    p.usage_sink = sink
    chunks = [c async for c in p.generate_stream([{"role": "user", "content": "x"}])]
    assert chunks[:2] == ["部分", "回答"], "流内容被改动了"
    assert "STREAM_USAGE:" in chunks[-1]
    assert '"cached_tokens": 900' in chunks[-1]
    assert '"cache_reported": true' in chunks[-1]
    assert seen and seen[0]["cached_tokens"] == 900
    assert seen[0]["cache_reported"] is True
    assert seen[0]["streamed"] is True


class TestNonStreamingProvidersWithoutSdk:
    """Anthropic / Google 的 SDK 在测试环境可能没装，直接验归一化函数即可。

    这也是把 ``extract_cache_usage`` 做成纯函数的原因：口径逻辑不该被
    "某个可选依赖没装"挡住测试。
    """

    @pytest.mark.parametrize("usage,expected", [
        ({"input_tokens": 10, "cache_read_input_tokens": 90},
         (90, 0, 100, True)),
        ({"prompt_token_count": 100, "cached_content_token_count": 0},
         (0, 0, 100, True)),
    ])
    def test_shapes(self, usage, expected):
        assert extract_cache_usage(usage) == expected


# ═══════════════════════════════════════════════════════════
# 事件链路：agent 的 usage_sink → usage_update → 右栏
# ═══════════════════════════════════════════════════════════


class _SinkLLM:
    """只提供 `_attach_usage_sink` 需要挂的那几个属性。"""

    def __init__(self) -> None:
        self.usage_sink = None
        self.pre_call_hook = None
        self.heartbeat_hook = None
        self.call_timeout = 0.0


def _usage_agent(ctx_max: int = 1000):
    """造一个**真 AutoMindAgent 实例**，只补用量回调相关的属性。"""
    from automind.agent import AutoMindAgent

    agent = object.__new__(AutoMindAgent)
    agent.llm = _SinkLLM()
    agent.resources = None
    agent.config = SimpleNamespace(
        memory=SimpleNamespace(short_term_max_tokens=ctx_max),
        execution=SimpleNamespace(llm_call_timeout_seconds=5.0))
    events: list[dict] = []

    async def sink(ev: dict) -> None:
        events.append(ev)

    agent.event_sink = sink
    agent._attach_usage_sink()
    return agent, events


async def _feed(agent, *, prompt: int, cached: int, reported: bool = True) -> None:
    await agent.llm.usage_sink({
        "prompt_tokens": prompt, "completion_tokens": 10,
        "total_tokens": prompt + 10, "cached_tokens": cached,
        "input_tokens": prompt, "cache_reported": reported,
    })


async def test_usage_event_carries_cache_rate_and_context():
    agent, events = _usage_agent(ctx_max=1000)
    await _feed(agent, prompt=100, cached=80)
    await _feed(agent, prompt=200, cached=0)

    ev = events[-1]
    assert ev["type"] == "usage_update"
    cum = ev["cumulative"]
    assert cum["calls"] == 2
    assert cum["cache_hit_calls"] == 1
    assert cum["cache_reported_calls"] == 2
    assert cum["input_tokens"] == 300
    assert cum["cache_rate"] == 26.7          # 80 / 300，按总输入算
    # 上下文看**单请求峰值**，不是累计输入：累计 300 会被误读成 30% 窗口占用
    assert cum["peak_prompt_tokens"] == 200
    assert ev["context"] == {"used": 200, "max": 1000, "pct": 20.0}
    # 原有字段语义不变
    assert cum["prompt_tokens"] == 300 and cum["completion_tokens"] == 20


async def test_usage_event_rate_is_none_when_provider_does_not_report():
    """不返回缓存字段的提供商：命中率是 None（界面显示「—」），不是 0%。"""
    agent, events = _usage_agent()
    await _feed(agent, prompt=100, cached=0, reported=False)
    ev = events[-1]
    assert ev["cumulative"]["cache_rate"] is None
    assert ev["cumulative"]["cache_reported_calls"] == 0
    assert ev["context"]["used"] == 100


def test_reset_usage_total_zeroes_live_counters():
    agent, _ = _usage_agent()
    agent._usage_total["calls"] = 3
    agent._usage_total["cached_tokens"] = 99
    agent._reset_usage_total()
    assert agent._usage_total["calls"] == 0
    assert agent._usage_total["cached_tokens"] == 0
    assert agent._usage_total["cache_reported_calls"] == 0
    assert agent._usage_total["peak_prompt_tokens"] == 0
