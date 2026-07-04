"""OpenAIProvider._create_with_retry 测试 — 容忍服务端返回损坏/截断响应体。"""

import json

import pytest

from automind.core.config import LLMProviderConfig
from automind.core.llm import OpenAIProvider


def _provider():
    cfg = LLMProviderConfig(provider="deepseek", model="deepseek-chat",
                            api_key="sk-test", api_base="https://api.deepseek.com/v1")
    return OpenAIProvider(cfg)


class _FakeCreate:
    """模拟 chat.completions.create：前 N 次抛 JSONDecodeError，之后成功。"""
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise json.JSONDecodeError("Unterminated string starting at", "x" * 90, 81)
        return {"ok": True}


@pytest.mark.asyncio
async def test_retry_recovers_from_jsondecode():
    p = _provider()
    fake = _FakeCreate(fail_times=2)
    p._client.chat.completions.create = fake
    resp = await p._create_with_retry({}, attempts=3)
    assert resp == {"ok": True}
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_retry_exhausted_raises_clean_error():
    p = _provider()
    fake = _FakeCreate(fail_times=99)
    p._client.chat.completions.create = fake
    with pytest.raises(Exception) as exc:
        await p._create_with_retry({}, attempts=2)
    # 不应是原始 JSONDecodeError，而是带中文说明的清晰错误
    assert not isinstance(exc.value, json.JSONDecodeError)
    assert "响应异常" in str(exc.value)
