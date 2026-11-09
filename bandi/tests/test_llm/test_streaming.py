import pytest

from bandi.core.message import TokenUsage
from bandi.llm.streaming import StreamEvent, StreamEventType, merge_stream_events


class _FakeResp:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aiter_lines(self):
        for c in self._chunks:
            for line in c.decode("utf-8").splitlines():
                yield line


class _FakeClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def stream(self, *args: object, **kwargs: object) -> _FakeResp:
        return _FakeResp(self._chunks)


@pytest.mark.asyncio
async def test_openai_compat_stream_tool_call_args_merged():
    from bandi.llm.adapters.openai_compat import sse_stream

    chunks = [
        b'data: {"choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"bash","arguments":""}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\":\\"ls\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    client = _FakeClient(chunks)
    events = [
        (ev.type, ev.tool_call_id, ev.tool_name, ev.arguments_delta)
        async for ev in sse_stream(client, "http://x", {}, {"model": "m", "messages": []})
    ]
    start_ids = [e[1] for e in events if e[0] == StreamEventType.TOOL_CALL_START]
    delta_targets = [e[1] for e in events if e[0] == StreamEventType.TOOL_CALL_DELTA]
    assert start_ids == ["call_abc"]
    assert delta_targets == ["call_abc"]
    result = await merge_stream_events(_stream_from_events(events))
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_abc"
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments == {"cmd": "ls"}


@pytest.mark.asyncio
async def test_openai_compat_stream_tool_call_start_without_id():
    from bandi.llm.adapters.openai_compat import sse_stream

    chunks = [
        b'data: {"choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"function":{"name":"bash","arguments":""}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\":\\"ls\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    client = _FakeClient(chunks)
    events = [
        (ev.type, ev.tool_call_id, ev.tool_name, ev.arguments_delta)
        async for ev in sse_stream(client, "http://x", {}, {"model": "m", "messages": []})
    ]
    starts = [e for e in events if e[0] == StreamEventType.TOOL_CALL_START]
    deltas = [e for e in events if e[0] == StreamEventType.TOOL_CALL_DELTA]
    ends = [e for e in events if e[0] == StreamEventType.TOOL_CALL_END]
    assert starts, "首 chunk 无 id 也必须发 TOOL_CALL_START（契约完整）"
    assert starts[0][2] == "bash", "START 必须携带工具名"
    assert len(deltas) == 1, "arguments delta 必须照发"
    assert len(ends) == 1, "TOOL_CALL_END 必须照发"
    assert starts[0][1] == deltas[0][1] == ends[0][1], (
        "START/DELTA/END 必须共享同一 tool_call_id（占位 id 一致）"
    )


@pytest.mark.asyncio
async def test_anthropic_stream_message_delta_with_usage():
    from bandi.llm.adapters.anthropic import anthropic_stream

    chunks = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":10,"output_tokens":5}}\n\n',
    ]
    client = _FakeClient(chunks)
    events = [
        (ev.type, ev.finish_reason, ev.usage)
        async for ev in anthropic_stream(
            client,
            "key",
            "claude-3-5",
            [{"role": "user", "content": "hi"}],
            max_tokens=100,
        )
    ]
    assert events[-1][0] == StreamEventType.DONE
    assert events[-1][1] == "stop"
    assert events[-1][2].prompt_tokens == 10
    assert events[-1][2].completion_tokens == 5


def _stream_from_events(events):
    async def gen():
        for ev_type, tcid, name, args in events:
            yield StreamEvent(
                type=ev_type,
                tool_call_id=tcid,
                tool_name=name,
                arguments_delta=args,
            )

    return gen()


@pytest.mark.asyncio
async def test_merge_stream_events_content():
    async def stream():
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="Hello")
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=" world")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    result = await merge_stream_events(stream())
    assert result.content == "Hello world"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_merge_stream_events_tool_calls():
    async def stream():
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START, tool_call_id="call_1", tool_name="bash"
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA, tool_call_id="call_1", arguments_delta='{"cmd"'
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA, tool_call_id="call_1", arguments_delta=': "ls"}'
        )
        yield StreamEvent(type=StreamEventType.TOOL_CALL_END, tool_call_id="call_1")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")

    result = await merge_stream_events(stream())
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments == {"cmd": "ls"}


@pytest.mark.asyncio
async def test_merge_stream_events_usage():
    async def stream():
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="test")
        yield StreamEvent(
            type=StreamEventType.DONE,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20),
        )

    result = await merge_stream_events(stream())
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 20


@pytest.mark.asyncio
async def test_merge_stream_events_empty():
    async def stream():
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
        if False:
            yield

    result = await merge_stream_events(stream())
    assert result.content == ""
    assert len(result.tool_calls) == 0


def test_stream_event_type_enum():
    assert StreamEventType.CONTENT_DELTA.name == "CONTENT_DELTA"
    assert StreamEventType.TOOL_CALL_START.name == "TOOL_CALL_START"
    assert StreamEventType.TOOL_CALL_END.name == "TOOL_CALL_END"
    assert StreamEventType.DONE.name == "DONE"
    assert StreamEventType.ERROR.name == "ERROR"


@pytest.mark.asyncio
async def test_tool_call_end_replaces_placeholder_id() -> None:
    async def _stream():
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START,
            tool_call_id="idx_0",
            tool_name="read",
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA,
            tool_call_id="idx_0",
            arguments_delta='{"path": "x"}',
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_END,
            tool_call_id="idx_0",
            final_tool_call_id="call_real",
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    response = await merge_stream_events(_stream())

    assert response.tool_calls[0].id == "call_real"
