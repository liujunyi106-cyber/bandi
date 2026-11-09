import json

import httpx
import pytest

from bandi.core.config import LLMConfig
from bandi.llm.adapters.anthropic import anthropic_stream
from bandi.llm.adapters.google import google_stream
from bandi.llm.providers.openai import OpenAICompatProvider
from bandi.llm.streaming import StreamEvent, StreamEventType


def _sse_response(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _collect(stream):
    out = []
    for ev in stream:
        out.append(ev)
    return out


class TestAnthropicUsageMerge:
    @pytest.mark.asyncio
    async def test_message_delta_preserves_input_tokens(self):

        def handler(request: httpx.Request) -> httpx.Response:
            body = (
                b"event: message_start\n"
                b'data: {"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":1}}}\n\n'
                b"event: content_block_delta\n"
                b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
                b"event: message_delta\n"
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":50}}\n\n'
                b"event: message_stop\n"
                b'data: {"type":"message_stop"}\n\n'
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        done_usage = None
        async for ev in anthropic_stream(client, "https://api.test", "sk", [], [], timeout=30):
            if ev.type == StreamEventType.DONE:
                done_usage = ev.usage
        await client.aclose()
        assert done_usage is not None
        assert done_usage.prompt_tokens == 100, (
            f"input_tokens 被覆盖为 {done_usage.prompt_tokens}（应为 100）"
        )
        assert done_usage.completion_tokens == 50


class TestOpenAILateIdMerge:
    @pytest.mark.asyncio
    async def test_late_tool_call_id_merges_not_splits(self):
        """
        deepseek 单 Map），不得分裂为两个幽灵调用。"""
        body = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"bash"}}]}}]}\n\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_real123","function":{"arguments":"{\\"cmd\\":"}}]}}]}\n\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            "data: [DONE]\n\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return _sse_response(body)

        provider = OpenAICompatProvider(
            LLMConfig(api_key="sk", default_model="m"), "https://api.test/v1"
        )
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = []
        async for ev in provider.stream([{"role": "user", "content": "hi"}]):
            events.append(ev)
        await provider.close()

        starts = [e for e in events if e.type == StreamEventType.TOOL_CALL_START]
        deltas = [e for e in events if e.type == StreamEventType.TOOL_CALL_DELTA]
        assert len(starts) == 1, f"工具调用分裂为 {len(starts)} 个 START"
        # 同一 stream_key 的 DELTA 参数必须拼接完整
        args_by_id: dict[str, str] = {}
        for d in deltas:
            args_by_id[d.tool_call_id] = args_by_id.get(d.tool_call_id, "") + (
                d.arguments_delta or ""
            )
        assert len(args_by_id) == 1, f"参数分散在多个 id 下: {list(args_by_id)}"
        full_args = next(iter(args_by_id.values()))
        assert json.loads(full_args) == {"cmd": "ls"}, f"参数重组失败: {full_args!r}"


class TestStreamOptionsIncludeUsage:
    @pytest.mark.asyncio
    async def test_stream_body_includes_stream_options(self):
        """
        stream_options={"include_usage": true}（否则官方 API 流式无 usage）。"""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return _sse_response(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            )

        provider = OpenAICompatProvider(
            LLMConfig(api_key="sk", default_model="m"), "https://api.test/v1"
        )
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async for _ev in provider.stream([{"role": "user", "content": "hi"}]):
            pass
        await provider.close()
        opts = captured["body"].get("stream_options")
        assert opts == {"include_usage": True}, f"stream_options 缺失: {opts}"


class TestThinkingModeStreamNotBareSent:
    @pytest.mark.asyncio
    async def test_openai_compat_stream_does_not_send_unknown_thinking_field(self):
        """thinking_mode 按 provider 官方语义翻译后发送（见
        test_contract_thinking_and_token_field.py）；通用兼容端点不得裸传自定义
        thinking_mode 字段（未知参数 400 风险）。"""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return _sse_response(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            )

        config = LLMConfig(api_key="sk", default_model="m", thinking_mode="high")
        provider = OpenAICompatProvider(config, "https://api.test/v1")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async for _ev in provider.stream([{"role": "user", "content": "hi"}]):
            pass
        await provider.close()
        assert "thinking_mode" not in captured["body"], (
            f"通用兼容端点不得裸传 thinking_mode: {captured['body']}"
        )


class TestGeminiStreamUsage:
    @pytest.mark.asyncio
    async def test_usage_metadata_extracted_from_stream(self):
        """
        末帧全量 usage——恒 0 会让成本记账失效。"""

        def handler(request: httpx.Request) -> httpx.Response:
            body = (
                b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}],'
                b'"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5,"totalTokenCount":15}}\n\n'
                b'data: {"candidates":[{"content":{"parts":[{"text":"!"}]},"finishReason":"STOP"}],'
                b'"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":9,"totalTokenCount":19}}\n\n'
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        done_usage = None
        async for ev in google_stream(
            client, "k", "gemini-test", [{"role": "user", "content": "hi"}]
        ):
            if ev.type == StreamEventType.DONE:
                done_usage = ev.usage
        await client.aclose()
        assert done_usage is not None, "DONE 未携带 usage"
        assert done_usage.prompt_tokens == 10 and done_usage.completion_tokens == 9


class TestHttpAdapterThinkingModeStream:
    @pytest.mark.asyncio
    async def test_http_adapter_stream_passes_thinking_mode(self):
        """
        透传给 adapter stream 函数（complete 已透传，stream 缺失属同族不一致）。"""
        captured: dict = {}

        async def fake_stream(*args, **kwargs):
            captured["thinking_mode"] = kwargs.get("thinking_mode")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        from bandi.llm.providers._http_provider import HttpAdapterProvider

        provider = HttpAdapterProvider(
            LLMConfig(api_key="sk", default_model="m", thinking_mode="enabled")
        )
        provider._adapter_stream = fake_stream
        async for _ev in provider.stream([{"role": "user", "content": "hi"}]):
            pass
        assert captured.get("thinking_mode") == "enabled"
