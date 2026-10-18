"""v1.11.10 服务端：提示缓存 / 上下文占用的聚合口径。

右栏的「缓存命中率」和「上下文使用率」直接决定用户对"省没省钱、
离上下文上限还有多远"的判断，所以这些数字必须在服务端有**唯一口径**：

    · 命中率的分母是**总输入** token（含命中部分），不是 prompt_tokens；
    · 命中率只在**提供商确实上报过缓存口径**时才有值：
      上报了且 0 命中 → 0.0%（这次确实没命中）；
      压根不报（如 Ollama）→ None（界面显示"—"，不能拿 0% 冒充）；
    · 上下文占用取最近一次任务的**单请求输入峰值**，不是累加花销
      （累加值随步数线性增长，永远超过窗口，看着像随时会溢出）。

这里同时盯住旧数据兼容：历史记录里没有新字段时必须按 0 聚合、
input 回退 prompt，否则重启后老记录的命中率分母会凭空少一块。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from automind.core.types import LLMResponse, TokenUsage

srv = pytest.importorskip("automind.server")


@pytest.fixture
def server(tmp_path, monkeypatch):
    """隔离进程级累计状态，并用轻量 Agent 替身提供上下文窗口配置。

    不替换 get_agent 的话，/api/tokens 会为了读一个数字去重建整个 Agent
    （扫项目、建索引），单元测试既慢又会碰用户的真实配置。
    """
    srv._store.config_file = tmp_path / "config.json"
    srv._store.chat_file = tmp_path / "chat.json"
    srv._task_history.clear()
    for key in srv._token_totals:
        srv._token_totals[key] = 0
    fake_agent = SimpleNamespace(
        config=SimpleNamespace(memory=SimpleNamespace(short_term_max_tokens=128000)))
    monkeypatch.setattr(srv, "get_agent", lambda: fake_agent)
    return srv


def _record(mode: str, prompt: int, completion: int, **extra) -> dict:
    """一条任务历史记录（``extra`` 用来补缓存/上下文字段）。"""
    return {
        "session_id": f"s{prompt}", "task": "t", "success": True, "output": "",
        "steps": 1, "backtracks": 0, "errors_corrected": 0,
        "tokens": prompt + completion, "prompt_tokens": prompt,
        "completion_tokens": completion, "duration_ms": 100.0,
        "plan": None, "interaction": mode, **extra,
    }


# ── 用量字段摊平 ──────────────────────────────────────────


def test_usage_record_fields_from_token_usage():
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20, cached_tokens=40,
                       input_tokens=100, calls=3, cache_hit_calls=2,
                       cache_reported_calls=3, peak_prompt_tokens=90)

    assert srv._usage_record_fields(usage) == {
        "tokens": 120, "prompt_tokens": 100, "completion_tokens": 20,
        "cached_tokens": 40, "input_tokens": 100, "llm_calls": 3,
        "cache_hit_calls": 2, "cache_reported_calls": 3,
        "peak_prompt_tokens": 90,
    }


def test_usage_record_fields_none_is_zeroed_schema():
    """语义缓存秒回 / 调用方没有用量 → 一整套 0，记录结构仍然完整。"""
    assert srv._usage_record_fields(None) == {
        "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "cached_tokens": 0, "input_tokens": 0, "llm_calls": 0,
        "cache_hit_calls": 0, "cache_reported_calls": 0,
        "peak_prompt_tokens": 0,
    }


def test_usage_record_fields_tolerates_legacy_namespace():
    """旧调用方传 SimpleNamespace（没有新字段）也不能炸。"""
    legacy = SimpleNamespace(prompt_tokens=10, completion_tokens=5)

    f = srv._usage_record_fields(legacy)

    assert f["tokens"] == 15
    assert f["input_tokens"] == 10, "没有 input_tokens → 回退 prompt_tokens"
    assert f["llm_calls"] == 1, "一个响应对象就代表一次调用"
    assert f["peak_prompt_tokens"] == 10
    assert f["cache_hit_calls"] == 0
    assert f["cache_reported_calls"] == 0, "没声明上报缓存口径 → 命中率应为 null"


def test_usage_record_fields_accepts_llm_response():
    """OpenAI 兼容端点拿到的是 LLMResponse：命中数要一起记进去。"""
    resp = LLMResponse(text="hi", prompt_tokens=200, completion_tokens=30,
                       cached_tokens=80, input_tokens=200, cache_reported=True)

    f = srv._usage_record_fields(resp)

    assert f["tokens"] == 230
    assert f["input_tokens"] == 200
    assert f["cached_tokens"] == 80
    assert f["llm_calls"] == 1 and f["cache_hit_calls"] == 1
    assert f["cache_reported_calls"] == 1
    assert f["peak_prompt_tokens"] == 200


def test_usage_record_fields_llm_response_without_cache_report():
    """提供商不报缓存口径（cache_reported=False）→ 不能记成"已上报"。"""
    resp = LLMResponse(text="hi", prompt_tokens=200, completion_tokens=30,
                       input_tokens=200)

    f = srv._usage_record_fields(resp)

    assert f["cached_tokens"] == 0
    assert f["cache_reported_calls"] == 0


# ── 会话计数器累加 ────────────────────────────────────────


def test_accumulate_tokens_accepts_legacy_record(server):
    """既有测试/旧调用方只给四个旧键：新指标按 0，input 回退 prompt。"""
    server._accumulate_tokens({"prompt_tokens": 100, "completion_tokens": 40,
                               "tokens": 140})

    t = server._token_totals
    assert (t["prompt"], t["completion"], t["total"], t["tasks"]) == (100, 40, 140, 1)
    assert t["input"] == 100
    assert (t["cached"], t["calls"], t["cache_hit_calls"]) == (0, 0, 0)
    assert t["cache_reported_calls"] == 0
    assert t["peak_prompt_tokens"] == 100


def test_accumulate_tokens_sums_cache_and_tracks_latest_peak(server):
    server._accumulate_tokens(_record(
        "coding", 100, 20, cached_tokens=60, input_tokens=100, llm_calls=3,
        cache_hit_calls=2, cache_reported_calls=3, peak_prompt_tokens=90))
    server._accumulate_tokens(_record(
        "chat", 500, 50, cached_tokens=0, input_tokens=500, llm_calls=1,
        cache_hit_calls=0, cache_reported_calls=1, peak_prompt_tokens=70))

    t = server._token_totals
    assert (t["prompt"], t["completion"], t["total"], t["tasks"]) == (600, 70, 670, 2)
    assert (t["cached"], t["input"], t["calls"], t["cache_hit_calls"]) == (60, 600, 4, 2)
    assert t["cache_reported_calls"] == 4
    assert t["peak_prompt_tokens"] == 70, "上下文占用取最近一次，不是累加/历史最大值"


# ── GET /api/tokens ───────────────────────────────────────


async def test_api_tokens_reports_cache_rate_and_context(server):
    server._token_totals.update({"cached": 300, "input": 1200, "calls": 5,
                                 "cache_hit_calls": 3, "cache_reported_calls": 5,
                                 "peak_prompt_tokens": 70000})

    view = await server.api_tokens()

    # 原有四个字段保留（旧集成脚本还在读），新增字段按固定契约
    assert set(view) == {
        "prompt", "completion", "total", "tasks", "cached", "input", "calls",
        "cache_hit_calls", "cache_rate", "context",
    }
    assert view["cached"] == 300 and view["input"] == 1200
    assert view["calls"] == 5 and view["cache_hit_calls"] == 3
    assert view["cache_rate"] == 25.0
    assert view["context"] == {"used": 70000, "max": 128000, "pct": 54.7}


async def test_api_tokens_reported_zero_hits_is_zero_percent(server):
    """上报了缓存口径、只是确实没命中 → 0.0%，不是 None。"""
    server._token_totals.update({"cached": 0, "input": 1000,
                                 "cache_reported_calls": 1})

    view = await server.api_tokens()

    assert view["cache_rate"] == 0.0


async def test_api_tokens_unreported_cache_is_null(server):
    """有输入但提供商压根不报缓存（如 Ollama）→ None，界面显示"—"。"""
    server._token_totals.update({"cached": 0, "input": 1000,
                                 "cache_reported_calls": 0})

    view = await server.api_tokens()

    assert view["cache_rate"] is None


async def test_context_max_comes_from_agent_memory_config(server, monkeypatch):
    server._token_totals["peak_prompt_tokens"] = 50000
    monkeypatch.setattr(srv, "get_agent", lambda: SimpleNamespace(
        config=SimpleNamespace(memory=SimpleNamespace(
            short_term_max_tokens=200000))))

    view = await server.api_tokens()

    assert view["context"]["max"] == 200000
    assert view["context"]["pct"] == 25.0


def test_context_max_falls_back_when_agent_is_unavailable(server, monkeypatch):
    def _boom():
        raise RuntimeError("Agent 还没建好")

    monkeypatch.setattr(srv, "get_agent", _boom)

    assert srv._context_max_tokens() == 128000
    assert srv._tokens_view()["context"]["max"] == 128000


async def test_api_tokens_without_any_data_has_null_cache_rate(server):
    view = await server.api_tokens()

    assert view["cached"] == 0 and view["input"] == 0
    assert view["cache_rate"] is None, "没有可计算的输入时是 null，不是 0%"
    assert view["context"] == {"used": 0, "max": 128000, "pct": 0.0}


async def test_api_tokens_reset_clears_cache_and_context(server):
    server._token_totals.update({"prompt": 9, "cached": 8, "input": 10, "calls": 2,
                                 "cache_hit_calls": 2, "cache_reported_calls": 2,
                                 "peak_prompt_tokens": 5000})

    await server.api_tokens_reset()

    assert server._token_totals == {k: 0 for k in server._token_totals}
    view = await server.api_tokens()
    assert view["cache_rate"] is None
    assert view["context"] == {"used": 0, "max": 128000, "pct": 0.0}


# ── GET /api/stats ────────────────────────────────────────


async def test_api_stats_session_uses_the_same_view(server):
    """右栏的实时值与 /api/tokens 必须同源，不能各算一套。"""
    server._token_totals.update({"cached": 40, "input": 100, "calls": 2,
                                 "cache_hit_calls": 1, "cache_reported_calls": 2,
                                 "peak_prompt_tokens": 80000})

    stats = await server.api_stats()

    assert stats["tokens_session"] == server._tokens_view()
    assert stats["tokens_session"]["cache_rate"] == 40.0
    assert stats["tokens_session"]["context"]["pct"] == 62.5


async def test_api_stats_aggregates_history_cache_metrics(server):
    server._task_history.extend([
        _record("coding", 1000, 400, cached_tokens=600, input_tokens=1000,
                llm_calls=4, cache_hit_calls=3, cache_reported_calls=4,
                peak_prompt_tokens=900),
        _record("chat", 100, 50),   # 旧记录：无新字段 → input 回退 prompt
    ])

    t = (await server.api_stats())["tokens"]

    assert (t["cached"], t["input"], t["calls"], t["cache_hit_calls"]) == (600, 1100, 4, 3)
    assert t["cache_rate"] == 54.5
    assert t["prompt"] == 1100 and t["completion"] == 450 and t["tasks"] == 2


async def test_api_stats_history_without_cache_has_null_rate(server):
    """旧记录（含没有 cache_reported_calls 的记录）不能被算成 0% 命中。"""
    server._task_history.append(_record("chat", 100, 50))

    t = (await server.api_stats())["tokens"]

    assert t["cached"] == 0
    assert t["cache_rate"] is None


async def test_api_stats_history_reported_zero_hits_is_zero_percent(server):
    """历史里上报过缓存口径、只是没命中 → 0.0%（与 None 区分开）。"""
    server._task_history.append(_record("chat", 100, 50, input_tokens=100,
                                        cache_reported_calls=1))

    t = (await server.api_stats())["tokens"]

    assert t["cache_rate"] == 0.0


async def test_record_fields_flow_into_session_and_history(server):
    """一条真实记录：字段摊平 → 历史聚合 → 会话累计，三处数字要一致。"""
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=600,
                       input_tokens=1000, calls=4, cache_hit_calls=3,
                       cache_reported_calls=4, peak_prompt_tokens=800)
    record = _record("coding", 1000, 200, **srv._usage_record_fields(usage))
    server._push_history(record)
    server._accumulate_tokens(record)

    view = await server.api_tokens()
    assert (view["cached"], view["input"], view["calls"], view["cache_hit_calls"]) == (
        600, 1000, 4, 3)
    assert view["cache_rate"] == 60.0
    assert view["context"]["used"] == 800

    t = (await server.api_stats())["tokens"]
    assert t["cached"] == 600 and t["input"] == 1000 and t["calls"] == 4
    assert t["cache_rate"] == 60.0


# ── 知识库去重窗口与 Agent 同源 ───────────────────────────


def test_kb_visible_uses_shared_history_window():
    """窗口判定必须与 Agent 真正发出去的历史窗口同源。

    以前这里硬编码 ``hist[-20:]``；一旦 Agent 侧的分批裁剪改为别的长度，
    服务端就会"以为模型还看得见"而跳过注入 —— 用户看到的是知识库明明
    有这段、模型却答不出来，且没有任何报错。
    """
    from automind.core.chat_window import CHAT_HISTORY_MAX

    old = {"role": "system", "content": "报销流程说明" + "x" * 100}
    hist = [old] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"闲聊 {i}"}
        for i in range(CHAT_HISTORY_MAX * 2)
    ]
    assert srv._kb_already_visible(hist, old["content"]) is False, (
        "片段早被挤出可见窗口，却仍被判定为在场 → 漏注入")

    recent = {"role": "system", "content": "调休规则说明" + "y" * 100}
    hist_in_window = [{"role": "user", "content": "调休怎么算？"}, recent]
    assert srv._kb_already_visible(hist_in_window, recent["content"]) is True
