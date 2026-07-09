"""统一 LLM 后端 — 支持 10+ 模型提供商，单一异步接口。

支持的提供商:
    - openai (OpenAI, 及兼容 API 如 DeepSeek/Kimi/百炼/智谱/豆包)
    - anthropic (Claude)
    - google (Gemini)
    - grok (Grok)
    - ollama (本地模型)

使用示例::

    backend = LLMBackendFactory.create("openai", config)
    response = await backend.generate([{"role": "user", "content": "Hello"}])
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from automind.core.config import LLMProviderConfig
from automind.core.exceptions import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMProviderNotFoundError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from automind.core.types import LLMResponse, ToolCall

# ═══════════════════════════════════════════════════════════════
# 抽象基类
# ═══════════════════════════════════════════════════════════════


class LLMBackend(ABC):
    """LLM 后端抽象基类。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        self.config = config
        self._tools: list[dict[str, Any]] = []

    @abstractmethod
    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """生成回复 (非流式)。"""

    @abstractmethod
    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """生成回复 (流式)。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本嵌入向量。"""
        raise NotImplementedError(f"{self.__class__.__name__} does not support embeddings")

    async def close(self) -> None:
        """释放后端持有的网络资源（默认无操作，子类按需覆盖）。"""
        return None

    def token_count(self, text: str) -> int:
        """估算文本的 token 数量。"""
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return len(text) // 4

    def supports_tools(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False

    def register_tools(self, tools: list[dict[str, Any]]) -> None:
        self._tools = tools


# ═══════════════════════════════════════════════════════════════
# OpenAI Provider (含所有 OpenAI 兼容 API)
# ═══════════════════════════════════════════════════════════════


class OpenAIProvider(LLMBackend):
    """OpenAI 及所有兼容 API 提供商。

    覆盖: OpenAI, DeepSeek, Kimi (Moonshot), 阿里百炼 (DashScope),
           智谱 (GLM), 豆包 (ByteDance), 及其他 OpenAI 兼容服务。
    """

    PROVIDER_API_BASES: dict[str, str] = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "bailian": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "doubao": "https://ark.cn-beijing.volces.com/api/v3",
        "custom": "",
    }

    PROVIDER_DEFAULT_MODELS: dict[str, str] = {
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "kimi": "moonshot-v1-128k",
        "moonshot": "moonshot-v1-128k",
        "bailian": "qwen-max",
        "dashscope": "qwen-max",
        "qwen": "qwen-max",
        "zhipu": "glm-4-plus",
        "glm": "glm-4-plus",
        "doubao": "doubao-pro-128k",
    }

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        from openai import AsyncOpenAI

        api_key = config.api_key
        if not api_key:
            # 自定义/中转代理常常使用占位 Key；仅在确实为空时报错
            raise LLMAuthenticationError(
                f"未配置 API Key（提供商 '{config.provider}'）。"
                f"请在「API Keys」面板填写，或设置对应的环境变量。"
            )

        # 优先使用用户自定义 api_base（中转/代理），否则用内置默认值
        # 仅当自定义 api_base 是有效 URL 时才使用
        raw_base = config.api_base or ""
        if raw_base and (raw_base.startswith("http://") or raw_base.startswith("https://")):
            api_base = raw_base
        else:
            api_base = self.PROVIDER_API_BASES.get(config.provider, "")
        if not api_base:
            raise LLMProviderNotFoundError(
                f"提供商 '{config.provider}' 缺少 API 地址（api_base）。"
                f"使用自定义/中转代理时，请在模型配置中填写 API 地址。"
            )

        model = (
            config.model
            or self.PROVIDER_DEFAULT_MODELS.get(config.provider, "")
            or "gpt-4o"
        )

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=config.timeout,
            max_retries=2,
            default_headers=config.extra_headers or None,
        )
        self._model = model
        self.api_base = api_base

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        tools = tools or self._tools
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]
        if stop:
            kwargs["stop"] = stop
        if self.config.extra_body:
            kwargs["extra_body"] = self.config.extra_body

        resp = await self._create_with_retry(kwargs)

        choice = resp.choices[0]
        tool_calls = None
        if choice.message.tool_calls:
            from automind.core.json_utils import parse_tool_arguments
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=parse_tool_arguments(tc.function.arguments),
                )
                for tc in choice.message.tool_calls
            ]

        return LLMResponse(
            text=choice.message.content or "",
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            finish_reason=choice.finish_reason or "stop",
            provider=self.config.provider,
            model=self._model,
            raw=resp.model_dump(),
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """流式生成文本。最后一个 chunk 前附加 usage 元数据（以 STREAM_USAGE: 前缀）。"""
        tools = tools or self._tools
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]

        import json as _json
        last_usage = None
        try:
            stream = await self._create_with_retry(kwargs)
        except Exception as e:
            self._handle_error(e)
            return
        try:
            async for chunk in stream:
                try:
                    # 提取 usage（DeepSeek / OpenAI 在最后 chunk 中返回）
                    if hasattr(chunk, "usage") and chunk.usage:
                        last_usage = chunk.usage
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                except (_json.JSONDecodeError, AttributeError, IndexError):
                    continue
            # 流结束后将 usage 编码为特殊标记
            if last_usage:
                usage_json = _json.dumps({
                    "prompt_tokens": getattr(last_usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(last_usage, "completion_tokens", 0),
                })
                yield f"\n<!--STREAM_USAGE:{usage_json}-->"
        except _json.JSONDecodeError:
            return
        except Exception as e:
            self._handle_error(e)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(
            model=self.config.extra_body.get("embedding_model", "text-embedding-3-small"),
            input=texts,
        )
        return [d.embedding for d in resp.data]

    async def _create_with_retry(self, kwargs: dict[str, Any], attempts: int = 3):
        """调用 chat.completions.create，对"响应体损坏/截断"等瞬时错误自动重试。

        某些 OpenAI 兼容服务（如中转/部分国产 API）偶发返回不完整的响应体，
        SDK 解析时抛出 json.JSONDecodeError（如 'Unterminated string ...'）。
        这类错误对单次请求是瞬时的，重试通常即可成功，不应让整个任务失败。
        """
        import asyncio as _asyncio
        import json as _json

        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except _json.JSONDecodeError as e:
                last_exc = e  # 响应体损坏/截断 → 重试
            except Exception as e:
                low = str(e).lower()
                # 透传明确的鉴权/限流/超时/上下文错误（含可重试判断）
                if any(k in low for k in ("unterminated", "expecting value",
                                          "json", "incomplete", "peer closed",
                                          "connection reset", "remote")):
                    last_exc = e  # 可重试的瞬时/网络/解析错误
                else:
                    self._handle_error(e)  # 不可重试 → 立即抛出
            if i < attempts - 1:
                await _asyncio.sleep(0.8 * (i + 1))
        # 多次重试仍失败
        raise LLMProviderNotFoundError(
            f"模型响应异常（已重试 {attempts} 次）：{str(last_exc)[:160]}。"
            f"通常是服务端返回了不完整的响应，请稍后重试或更换模型/中转地址。"
        ) from last_exc

    def _handle_error(self, error: Exception) -> None:
        msg = str(error).lower()
        if "401" in msg or "403" in msg or "unauthorized" in msg or "invalid api key" in msg:
            raise LLMAuthenticationError(str(error), code="AUTH_ERROR") from error
        if "429" in msg or "rate limit" in msg:
            raise LLMRateLimitError(str(error), code="RATE_LIMIT") from error
        if "timeout" in msg:
            raise LLMTimeoutError(str(error), code="TIMEOUT") from error
        if "context" in msg and ("too long" in msg or "exceed" in msg):
            raise LLMContextTooLargeError(str(error), code="CONTEXT_TOO_LARGE") from error
        raise


# ═══════════════════════════════════════════════════════════════
# Anthropic Provider
# ═══════════════════════════════════════════════════════════════


class AnthropicProvider(LLMBackend):
    """Anthropic Claude API 提供商。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        from anthropic import AsyncAnthropic

        api_key = config.api_key
        if not api_key:
            raise LLMAuthenticationError(
                "Anthropic API key not configured. Set ANTHROPIC_API_KEY env var."
            )

        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=config.timeout,
        )
        self._model = config.model or "claude-sonnet-4-20250514"

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        system_prompt, user_messages = self._split_messages(messages)
        tools = tools or self._tools

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": user_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._convert_tools_anthropic(tools)
        if stop:
            kwargs["stop_sequences"] = stop

        try:
            resp = await self._client.messages.create(**kwargs)
        except Exception as e:
            self._handle_error(e)

        text = ""
        tool_calls = None
        for block in resp.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or {},
                ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.input_tokens if resp.usage else 0,
            completion_tokens=resp.usage.output_tokens if resp.usage else 0,
            finish_reason=resp.stop_reason or "stop",
            provider="anthropic",
            model=self._model,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else {},
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        system_prompt, user_messages = self._split_messages(messages)
        tools = tools or self._tools

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": user_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            self._handle_error(e)

    def _split_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """分离 system prompt 和对话消息。"""
        system_parts = []
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                user_messages.append(m)
        return "\n".join(system_parts), user_messages

    def _convert_tools_anthropic(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 OpenAI 格式 tools 转为 Anthropic 格式。"""
        converted = []
        for tool in tools:
            converted.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": {
                    "type": "object",
                    "properties": tool.get("parameters", {}).get("properties", {}),
                    "required": tool.get("parameters", {}).get("required", []),
                },
            })
        return converted

    def _handle_error(self, error: Exception) -> None:
        msg = str(error).lower()
        if "401" in msg or "403" in msg or "invalid" in msg and "key" in msg:
            raise LLMAuthenticationError(str(error), code="AUTH_ERROR") from error
        if "429" in msg or "rate" in msg:
            raise LLMRateLimitError(str(error), code="RATE_LIMIT") from error
        if "timeout" in msg:
            raise LLMTimeoutError(str(error), code="TIMEOUT") from error
        raise


# ═══════════════════════════════════════════════════════════════
# Google Gemini Provider
# ═══════════════════════════════════════════════════════════════


class GoogleProvider(LLMBackend):
    """Google Gemini API 提供商。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        import google.generativeai as genai

        api_key = config.api_key
        if not api_key:
            raise LLMAuthenticationError(
                "Google API key not configured. Set GOOGLE_API_KEY env var."
            )

        genai.configure(api_key=api_key)
        self._model_name = config.model or "gemini-2.5-flash"
        self._model = genai.GenerativeModel(self._model_name)

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        contents = self._convert_messages(messages)

        try:
            resp = await self._model.generate_content_async(
                contents,
                generation_config={
                    "temperature": self.config.temperature,
                    "max_output_tokens": self.config.max_tokens,
                },
            )
        except Exception as e:
            self._handle_error(e)

        return LLMResponse(
            text=resp.text or "",
            prompt_tokens=resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0,
            completion_tokens=resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0,
            provider="google",
            model=self._model_name,
            raw={"text": resp.text},
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        contents = self._convert_messages(messages)
        try:
            resp = await self._model.generate_content_async(
                contents,
                generation_config={
                    "temperature": self.config.temperature,
                    "max_output_tokens": self.config.max_tokens,
                },
                stream=True,
            )
            async for chunk in resp:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            self._handle_error(e)

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts = []
        for m in messages:
            role = "user" if m["role"] != "model" else "model"
            parts.append({"role": role, "parts": [m["content"]]})
        return parts

    def _handle_error(self, error: Exception) -> None:
        msg = str(error).lower()
        if "401" in msg or "403" in msg or "api key" in msg:
            raise LLMAuthenticationError(str(error), code="AUTH_ERROR") from error
        if "429" in msg:
            raise LLMRateLimitError(str(error), code="RATE_LIMIT") from error
        raise


# ═══════════════════════════════════════════════════════════════
# Grok Provider
# ═══════════════════════════════════════════════════════════════


class GrokProvider(OpenAIProvider):
    """Grok API (OpenAI 兼容)。"""

    PROVIDER_API_BASES = {"grok": "https://api.x.ai/v1"}
    PROVIDER_DEFAULT_MODELS = {"grok": "grok-3"}


# ═══════════════════════════════════════════════════════════════
# Ollama Provider (本地模型)
# ═══════════════════════════════════════════════════════════════


class OllamaProvider(LLMBackend):
    """Ollama 本地模型提供商。"""

    def __init__(self, config: LLMProviderConfig) -> None:
        super().__init__(config)
        import httpx
        self._client = httpx.AsyncClient(
            base_url=config.api_base or "http://localhost:11434",
            timeout=config.timeout,
        )
        self._model = config.model or "llama3.2"

    async def close(self) -> None:
        """关闭底层 httpx 连接池。"""
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self._handle_error(e)

        return LLMResponse(
            text=data.get("message", {}).get("content", ""),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            provider="ollama",
            model=self._model,
            raw=data,
        )

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        data = json.loads(line)
                        content = data.get("message", {}).get("content", "")
                        if content:
                            yield content
        except Exception as e:
            self._handle_error(e)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for text in texts:
            resp = await self._client.post("/api/embeddings", json={
                "model": self._model,
                "prompt": text,
            })
            resp.raise_for_status()
            data = resp.json()
            embeddings.append(data.get("embedding", []))
        return embeddings

    def supports_tools(self) -> bool:
        return False

    def _handle_error(self, error: Exception) -> None:
        msg = str(error).lower()
        if "connect" in msg or "refused" in msg:
            raise LLMTimeoutError(
                f"Cannot connect to Ollama at {self.config.api_base or 'http://localhost:11434'}. "
                f"Is Ollama running?",
                code="CONNECTION_REFUSED",
            ) from error
        raise


# ═══════════════════════════════════════════════════════════════
# 工厂
# ═══════════════════════════════════════════════════════════════


class LLMBackendFactory:
    """LLM 后端工厂 — 根据提供商名称创建对应的后端实例。"""

    _providers: dict[str, type[LLMBackend]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "google": GoogleProvider,
        "gemini": GoogleProvider,
        "grok": GrokProvider,
        "deepseek": OpenAIProvider,
        "kimi": OpenAIProvider,
        "moonshot": OpenAIProvider,
        "bailian": OpenAIProvider,
        "dashscope": OpenAIProvider,
        "qwen": OpenAIProvider,
        "zhipu": OpenAIProvider,
        "glm": OpenAIProvider,
        "doubao": OpenAIProvider,
        "ollama": OllamaProvider,
        # 自定义 OpenAI 标准接口（支持中转/代理）
        "custom": OpenAIProvider,
        "openai_compatible": OpenAIProvider,
    }

    @classmethod
    def create(cls, provider: str, config: LLMProviderConfig) -> LLMBackend:
        """创建 LLM 后端实例。

        Args:
            provider: 提供商名称 (openai, anthropic, google, deepseek, ollama, etc.)。
            config: LLM 提供商配置。

        Returns:
            对应提供商的 LLMBackend 实例。

        Raises:
            LLMProviderNotFoundError: 未知的提供商。
        """
        provider_lower = provider.lower()
        provider_cls = cls._providers.get(provider_lower)
        if provider_cls is None:
            available = ", ".join(sorted(cls._providers.keys()))
            raise LLMProviderNotFoundError(
                f"Unknown LLM provider '{provider}'. Available: {available}"
            )
        # 确保 provider 名称写入 config
        config = config.model_copy(update={"provider": provider_lower})
        return provider_cls(config)

    @classmethod
    def register(cls, name: str, backend_cls: type[LLMBackend]) -> None:
        """注册自定义 LLM 后端。"""
        cls._providers[name.lower()] = backend_cls

    @classmethod
    def available_providers(cls) -> list[str]:
        """返回所有已注册的提供商名称。"""
        return sorted(cls._providers.keys())
