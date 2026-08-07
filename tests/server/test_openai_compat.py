"""OpenAI 兼容 API（IDE 集成：Continue.dev 等）测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402


class _FakeResp:
    text = "你好，我是 AutoMind"
    prompt_tokens = 12
    completion_tokens = 7
    total_tokens = 19
    finish_reason = "stop"


class _FakeLLM:
    async def generate(self, messages, tools=None, stop=None):
        assert isinstance(messages, list) and messages
        return _FakeResp()

    async def generate_stream(self, messages, tools=None):
        for piece in ("你好", "，", "世界"):
            yield piece

    def token_count(self, text: str) -> int:
        return max(1, len(text) // 2)


@pytest.fixture()
def client(monkeypatch):
    from automind import server
    # 防御性清零（既有约定）：避免前序测试文件的令牌赋值泄漏进来
    monkeypatch.setattr(server, "_AUTH_TOKEN", "")
    # 显式用回环地址作为来源：TestClient 默认的 client.host 是 "testclient"，
    # 而 v1.4.5 起 /api/integrations/continue 这类"会吐出明文令牌"的端点
    # 在未配置令牌时只接受本机请求。用真实的 127.0.0.1 才反映真实调用场景。
    return TestClient(server.app, client=("127.0.0.1", 50000))


@pytest.fixture()
def stub_agent(monkeypatch):
    """把全局 agent 替换为带假 LLM 的桩（测完还原）。"""
    from automind import server
    agent = SimpleNamespace(
        llm=_FakeLLM(),
        config=SimpleNamespace(llm=SimpleNamespace(model="fake-model")),
    )
    monkeypatch.setattr(server, "_agent", agent)
    return agent


class TestModels:
    def test_v1_models_shape(self, client, stub_agent):
        r = client.get("/v1/models")
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        assert body["data"][0]["id"] == "fake-model"


class TestChatCompletions:
    def test_missing_messages_400(self, client, stub_agent):
        r = client.post("/v1/chat/completions", json={})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"

    def test_llm_not_ready_503(self, client, monkeypatch):
        from automind import server
        monkeypatch.setattr(server, "_agent", SimpleNamespace(
            llm=None, config=SimpleNamespace(llm=SimpleNamespace(model="m"))))
        r = client.post("/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 503

    def test_non_stream_completion(self, client, stub_agent):
        r = client.post("/v1/chat/completions", json={
            "model": "whatever",
            "messages": [{"role": "user", "content": "你好"}],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "你好，我是 AutoMind"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] == 19
        assert body["model"] == "fake-model"

    def test_multimodal_content_flattened(self, client, stub_agent):
        r = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "解释这段代码"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ]}],
        })
        assert r.status_code == 200

    def test_stream_sse(self, client, stub_agent):
        with client.stream("POST", "/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        }) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            raw = "".join(r.iter_text())
        assert '"role": "assistant"' in raw or '"role":"assistant"' in raw
        assert "你好" in raw and "世界" in raw
        assert '"finish_reason": "stop"' in raw or '"finish_reason":"stop"' in raw
        assert raw.rstrip().endswith("data: [DONE]")


class TestAuthGuard:
    def test_v1_requires_token_when_configured(self, client, stub_agent, monkeypatch):
        from automind import server
        monkeypatch.setattr(server, "_AUTH_TOKEN", "sek")
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models",
                          headers={"Authorization": "Bearer sek"}).status_code == 200


class TestContinueConfig:
    def test_config_yaml(self, client, stub_agent):
        r = client.get("/api/integrations/continue")
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == "fake-model"
        assert body["base_url"].endswith("/v1")
        assert "provider: openai" in body["yaml"]
        assert "apiBase:" in body["yaml"] and "fake-model" in body["yaml"]


class TestContinueConfigBoundary:
    """该端点会返回明文访问令牌，按"泄密端点"对待（v1.4.5）。"""

    def test_remote_without_token_denied(self, monkeypatch, stub_agent):
        from automind import server
        monkeypatch.setattr(server, "_AUTH_TOKEN", "")
        c = TestClient(server.app, client=("192.168.1.50", 50000))
        assert c.get("/api/integrations/continue").status_code == 403

    def test_response_is_not_cacheable(self, client, stub_agent):
        r = client.get("/api/integrations/continue")
        assert r.status_code == 200
        assert "no-store" in r.headers.get("cache-control", "")
