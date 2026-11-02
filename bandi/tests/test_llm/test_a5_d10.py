# 功能：流式双缓冲区 + 统一鉴权测试
import httpx
import pytest

from bandi.core.config import LLMConfig
from bandi.core.message import LLMResponse, TokenUsage
from bandi.llm.providers._http_provider import HttpAdapterProvider
from bandi.llm.providers.openai import OpenAICompatProvider
from bandi.llm.streaming import (
    StreamEvent,
    StreamEventType,
    StreamingBuffer,
    merge_stream_events,
)


class TestStreamingBuffer:
    def test_text_buffer_accumulation(self) -> None:
        buf = StreamingBuffer()
        buf.append_text("Hello")
        buf.append_text(" world")
        assert buf.text == "Hello world"

    def test_tool_call_buffer(self) -> None:
        buf = StreamingBuffer()
        buf.start_tool_call("call_1", "bash")
        buf.append_tool_call("call_1", '{"cmd"')
        buf.append_tool_call("call_1", ': "ls"}')
        completed = buf.get_completed_tool_calls()
        assert len(completed) == 1
        assert completed[0]["id"] == "call_1"
        assert completed[0]["name"] == "bash"
        assert completed[0]["arguments"] == '{"cmd": "ls"}'

    def test_clear(self) -> None:
        buf = StreamingBuffer()
        buf.append_text("test")
        buf.start_tool_call("call_1", "bash")
        buf.clear()
        assert buf.text == ""
        assert buf.get_completed_tool_calls() == []


@pytest.mark.asyncio
async def test_merge_stream_events_with_buffer():

    async def stream():
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="Hello")
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=" world")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START,
            tool_call_id="call_1",
            tool_name="bash",
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA,
            tool_call_id="call_1",
            arguments_delta='{"cmd":"ls"}',
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    result = await merge_stream_events(stream())
    assert result.content == "Hello world"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments == {"cmd": "ls"}


class TestBaseProviderAuth:
    def test_auth_token_default_from_config(self) -> None:
        from bandi.llm.base import BaseProvider

        config = LLMConfig(api_key="sk-test-key")

        # 不能直接实例化 ABC，但可以通过子类测试
        class TestProvider(BaseProvider):
            async def complete(self, messages, tools=None, **kwargs):
                pass

            async def stream(self, messages, tools=None, **kwargs):
                pass

        provider = TestProvider(config)
        assert provider.auth_token == "sk-test-key"

    def test_auth_token_setter(self) -> None:
        from bandi.llm.base import BaseProvider

        config = LLMConfig(api_key="sk-original")

        class TestProvider(BaseProvider):
            async def complete(self, messages, tools=None, **kwargs):
                pass

            async def stream(self, messages, tools=None, **kwargs):
                pass

        provider = TestProvider(config)
        provider.auth_token = "sk-new-key"
        assert provider.auth_token == "sk-new-key"

    def test_set_auth_token_alias(self) -> None:
        from bandi.llm.base import BaseProvider

        config = LLMConfig()

        class TestProvider(BaseProvider):
            async def complete(self, messages, tools=None, **kwargs):
                pass

            async def stream(self, messages, tools=None, **kwargs):
                pass

        provider = TestProvider(config)
        provider.set_auth_token("sk-oauth-token")
        assert provider.auth_token == "sk-oauth-token"

    def test_auth_token_empty_string(self) -> None:
        from bandi.llm.base import BaseProvider

        config = LLMConfig(api_key="sk-key")

        class TestProvider(BaseProvider):
            async def complete(self, messages, tools=None, **kwargs):
                pass

            async def stream(self, messages, tools=None, **kwargs):
                pass

        provider = TestProvider(config)
        provider.auth_token = ""
        assert provider.auth_token == ""


class TestD10AuthTokenWiring:
    """接线契约：set_auth_token 后实际 HTTP 请求必须携带新 token（运行时鉴权轮换）。
    族级覆盖全部 4 个生产请求构建路径：OpenAICompat complete/stream + HttpAdapter complete/stream。"""

    @staticmethod
    def _openai_response_body() -> dict:
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    @pytest.mark.asyncio
    async def test_openai_compat_complete_uses_rotated_token(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=self._openai_response_body())

        config = LLMConfig(api_key="sk-original", default_model="test-model")
        provider = OpenAICompatProvider(config, "https://api.test/v1")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider.set_auth_token("sk-rotated")
        await provider.complete([{"role": "user", "content": "hi"}])
        await provider.close()
        assert captured["auth"] == "Bearer sk-rotated", (
            f"complete 请求头仍用旧凭据: {captured['auth']!r}"
        )

    @pytest.mark.asyncio
    async def test_openai_compat_stream_uses_rotated_token(self) -> None:
        captured: dict = []
        sse_body = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization"))
            return httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"})

        config = LLMConfig(api_key="sk-original", default_model="test-model")
        provider = OpenAICompatProvider(config, "https://api.test/v1")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider.set_auth_token("sk-rotated")
        async for _event in provider.stream([{"role": "user", "content": "hi"}]):
            pass
        await provider.close()
        assert captured and captured[0] == "Bearer sk-rotated", (
            f"stream 请求头仍用旧凭据: {captured!r}"
        )

    @pytest.mark.asyncio
    async def test_http_adapter_complete_uses_rotated_token(self) -> None:
        captured: dict = {}

        async def fake_complete(**kwargs):
            captured["api_key"] = kwargs.get("api_key")
            return LLMResponse(content="ok")

        config = LLMConfig(api_key="sk-original", default_model="test-model")
        provider = HttpAdapterProvider(config)
        provider._adapter_complete = fake_complete
        provider.set_auth_token("sk-rotated")
        await provider.complete([{"role": "user", "content": "hi"}])
        assert captured["api_key"] == "sk-rotated", (
            f"adapter complete 收到的仍是旧凭据: {captured['api_key']!r}"
        )

    @pytest.mark.asyncio
    async def test_http_adapter_stream_uses_rotated_token(self) -> None:
        captured: dict = {}

        async def fake_stream(*args, **kwargs):
            captured["api_key"] = args[1] if len(args) > 1 else kwargs.get("api_key")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        config = LLMConfig(api_key="sk-original", default_model="test-model")
        provider = HttpAdapterProvider(config)
        provider._adapter_stream = fake_stream
        provider.set_auth_token("sk-rotated")
        async for _event in provider.stream([{"role": "user", "content": "hi"}]):
            pass
        assert captured["api_key"] == "sk-rotated", (
            f"adapter stream 收到的仍是旧凭据: {captured['api_key']!r}"
        )


@pytest.mark.asyncio
async def test_merge_stream_events_unparseable_json_returns_actionable_error() -> None:
    """接线契约：工具参数 JSON 无法修复时必须返回可行动的 _parse_error 文本
    不得静默降级 args={}（空参数执行写类工具是危险行为——对齐 states 主链路语义）。"""

    async def stream():
        yield StreamEvent(type=StreamEventType.TOOL_CALL_START, tool_call_id="c1", tool_name="bash")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA, tool_call_id="c1", arguments_delta="not-json"
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    resp = await merge_stream_events(stream())
    assert len(resp.tool_calls) == 1
    assert "_parse_error" in resp.tool_calls[0].arguments, (
        f"JSON 损坏被静默吞掉: {resp.tool_calls[0].arguments!r}"
    )


@pytest.mark.asyncio
async def test_merge_second_done_without_usage_keeps_first() -> None:
    """接线契约：DONE 不带 usage 时不得覆盖先前已累积的 usage（防御性一致）。"""

    async def stream():
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="x")
        yield StreamEvent(
            type=StreamEventType.DONE,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    resp = await merge_stream_events(stream())
    assert resp.usage is not None and resp.usage.prompt_tokens == 10, (
        f"usage 被无 usage 的 DONE 覆盖: {resp.usage!r}"
    )
