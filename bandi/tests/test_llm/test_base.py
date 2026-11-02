import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, message_to_dict
from bandi.llm.mock import MockProvider
from bandi.llm.streaming import StreamEventType


@pytest.mark.asyncio
async def test_mock_provider_returns_default_response():
    provider = MockProvider(config=None)
    msgs = [message_to_dict(Message(role=MessageRole.USER, content="hi"))]
    response = await provider.complete(msgs)
    assert response.content == "Mock response"
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_mock_provider_returns_predefined_response():
    provider = MockProvider(config=None)
    expected = LLMResponse(content="Hello!", usage=None)
    provider.add_response(expected)
    msgs = [message_to_dict(Message(role=MessageRole.USER, content="hi"))]
    response = await provider.complete(msgs)
    assert response.content == "Hello!"


@pytest.mark.asyncio
async def test_mock_provider_raises_error():
    provider = MockProvider(config=None)
    provider.add_response(RuntimeError)
    msgs = [message_to_dict(Message(role=MessageRole.USER, content="hi"))]
    with pytest.raises(RuntimeError):
        await provider.complete(msgs)


@pytest.mark.asyncio
async def test_mock_provider_stream_content():
    provider = MockProvider(config=None)
    provider.add_response(LLMResponse(content="streaming reply"))
    msgs = [message_to_dict(Message(role=MessageRole.USER, content="hi"))]
    events = [e async for e in provider.stream(msgs)]
    deltas = [e.content_delta for e in events if e.type == StreamEventType.CONTENT_DELTA]
    assert "".join(delta for delta in deltas if delta) == "streaming reply"
    assert any(e.type == StreamEventType.DONE for e in events)


@pytest.mark.asyncio
async def test_mock_provider_stream_tool_calls():
    provider = MockProvider(config=None)
    provider.add_response(
        LLMResponse(
            content="", tool_calls=[ToolCall(id="call_1", name="bash", arguments={"cmd": "ls"})]
        )
    )
    msgs = [message_to_dict(Message(role=MessageRole.USER, content="run ls"))]
    events = [e async for e in provider.stream(msgs)]
    starts = [e for e in events if e.type == StreamEventType.TOOL_CALL_START]
    assert len(starts) == 1
    assert starts[0].tool_name == "bash"


def test_base_provider_count_tokens():
    provider = MockProvider(config=None)
    assert provider.count_tokens("hello world") == 2
    assert provider.count_tokens("a" * 100) == 13
