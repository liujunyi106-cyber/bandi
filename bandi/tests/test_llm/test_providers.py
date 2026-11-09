from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bandi.core.config import LLMConfig
from bandi.core.message import Message, MessageRole, message_to_dict
from bandi.llm.providers.deepseek import DeepSeekProvider
from bandi.llm.providers.openai import OpenAICompatProvider, OpenAIProvider
from bandi.llm.streaming import StreamEvent, StreamEventType


@pytest.fixture
def llm_config() -> LLMConfig:
    cfg = LLMConfig()
    cfg.api_key = "test-key"
    cfg.default_model = "gpt-4"
    cfg.base_url = "https://api.openai.com/v1"
    cfg.timeout = 10.0
    return cfg


class TestOpenAICompatProvider:
    def test_init_sets_base_url(self, llm_config: LLMConfig) -> None:
        provider = OpenAICompatProvider(llm_config, "https://custom.api.com")
        assert "custom.api.com" in provider._base_url

    def test_init_uses_config_base_url(self, llm_config: LLMConfig) -> None:
        llm_config.base_url = "https://internal.api.com"
        provider = OpenAICompatProvider(llm_config, "")
        assert "internal.api.com" in provider._base_url

    @pytest.mark.asyncio
    async def test_lifecycle(self, llm_config: LLMConfig) -> None:
        provider = OpenAICompatProvider(llm_config, "https://api.test.com")
        async with provider as p:
            assert p is provider
        assert provider._client.is_closed

    @pytest.mark.asyncio
    async def test_complete_success(self, llm_config: LLMConfig) -> None:
        provider = OpenAICompatProvider(llm_config, "https://api.test.com")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(
            return_value={
                "choices": [
                    {"message": {"content": "Hello!", "tool_calls": None}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        provider._client.post = AsyncMock(return_value=mock_response)
        msgs = [message_to_dict(Message(role=MessageRole.USER, content="hi"))]
        result = await provider.complete(msgs)
        assert result.content == "Hello!"
        assert result.usage.prompt_tokens == 10
        await provider.close()

    @pytest.mark.asyncio
    async def test_complete_retry_on_failure(self, llm_config: LLMConfig) -> None:
        provider = OpenAICompatProvider(llm_config, "https://api.test.com")
        provider._client.post = AsyncMock(side_effect=Exception("connection error"))
        msgs = [{"role": "user", "content": "hi"}]
        with pytest.raises(Exception, match="connection error"):
            await provider.complete(msgs)
        await provider.close()

    @pytest.mark.asyncio
    async def test_stream_connection_error(self, llm_config: LLMConfig, monkeypatch) -> None:
        """首事件前连接失败必须 raise LLMError，由上层 recovery 显式处理。"""
        from bandi.core.exceptions import LLMError
        from bandi.llm.providers import openai as openai_mod

        async def _boom(*args, **kwargs):
            raise ConnectionError("connect refused")
            yield  # pragma: no cover

        monkeypatch.setattr(openai_mod, "sse_stream", _boom)
        provider = OpenAICompatProvider(llm_config, "https://api.test.com")
        msgs = [{"role": "user", "content": "hi"}]
        with pytest.raises(LLMError):
            async for _ in provider.stream(msgs):
                pass
        await provider.close()

    @pytest.mark.asyncio
    async def test_stream_yields_error_after_first_event(
        self, llm_config: LLMConfig, monkeypatch
    ) -> None:
        """#16：首事件后中途失败仍 yield ERROR（agent/states.py:433 消费终态）。"""
        from bandi.llm.providers import openai as openai_mod

        async def _partial(*args, **kwargs):
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="你好")
            raise ConnectionError("mid-stream drop")

        monkeypatch.setattr(openai_mod, "sse_stream", _partial)
        provider = OpenAICompatProvider(llm_config, "https://api.test.com")
        msgs = [{"role": "user", "content": "hi"}]
        events = []
        async for event in provider.stream(msgs):
            events.append(event)
        assert events[0].type == StreamEventType.CONTENT_DELTA
        assert events[1].type == StreamEventType.ERROR
        await provider.close()


class TestDeepSeekProvider:
    @pytest.mark.asyncio
    async def test_extends_openai_provider(self, llm_config: LLMConfig) -> None:
        # 无显式 base_url 时回落官方默认域（显式 base_url 覆盖语义见
        # test_contract_provider_resolution.py）
        llm_config.base_url = ""
        provider = DeepSeekProvider(llm_config)
        assert provider._base_url == "https://api.deepseek.com/v1"
        await provider.close()


class TestOpenAIProvider:
    @pytest.mark.asyncio
    async def test_extends_openai_provider(self, llm_config: LLMConfig) -> None:
        # 无显式 base_url 时回落官方默认域（显式 base_url 覆盖语义同上）
        llm_config.base_url = ""
        provider = OpenAIProvider(llm_config)
        assert provider._base_url == "https://api.openai.com/v1"
        await provider.close()
