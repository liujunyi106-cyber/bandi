from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig, LLMConfig
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall
from bandi.llm.mock import MockProvider as RealMockProvider
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from tests.integration.helpers import MockProvider, RecordingBus


class EchoTool(BaseTool):
    name = "echo"
    is_concurrency_safe = True

    async def execute(self, params: dict) -> str:
        return params.get("msg", "")


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register_instance(EchoTool())
    return r


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry=registry)


@pytest.fixture
def llm_config() -> LLMConfig:
    return LLMConfig(api_key="", default_model="mock")


@pytest.mark.asyncio
async def test_non_stream_completes_with_content(
    executor: ToolExecutor, registry: ToolRegistry
) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="hello from non-stream", usage=TokenUsage(prompt_tokens=5, completion_tokens=3)
        )
    )
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(max_iterations=2),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=2).run(ctx)
    assert reason == TerminalReason.COMPLETED
    assert ctx.current_content == "hello from non-stream"
    assert ctx.total_tokens >= 8


@pytest.mark.asyncio
async def test_non_stream_with_tool_calls(executor: ToolExecutor, registry: ToolRegistry) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"msg": "tool works"})],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
    )
    provider.add_response(
        LLMResponse(content="done", usage=TokenUsage(prompt_tokens=15, completion_tokens=3))
    )
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="use tool")],
        provider=provider,
        config=AgentConfig(max_iterations=3),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=3).run(ctx)
    assert reason == TerminalReason.COMPLETED
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) >= 1
    assert ctx.total_tokens > 0


@pytest.mark.asyncio
async def test_stream_emits_content_delta_events(
    executor: ToolExecutor, registry: ToolRegistry, llm_config: LLMConfig
) -> None:
    bus = RecordingBus()
    provider = RealMockProvider(llm_config)
    provider.add_response(LLMResponse(content="streamed reply"))
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="stream test")],
        provider=provider,
        config=AgentConfig(max_iterations=2, stream_to_ui=True),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=2).run(ctx)
    assert reason == TerminalReason.COMPLETED
    stream_events = [e for e in bus.events if e.type == EventType.LLM_STREAM_EVENT]
    assert len(stream_events) > 0
    content_deltas = [e for e in stream_events if e.data and e.data.get("type") == "content_delta"]
    assert len(content_deltas) > 0


@pytest.mark.asyncio
async def test_stream_with_tool_calls(
    executor: ToolExecutor, registry: ToolRegistry, llm_config: LLMConfig
) -> None:
    bus = RecordingBus()
    provider = RealMockProvider(llm_config)
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"msg": "stream tool"})],
        )
    )
    provider.add_response(LLMResponse(content="done"))
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="use tool streaming")],
        provider=provider,
        config=AgentConfig(max_iterations=3, stream_to_ui=True),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=3).run(ctx)
    assert reason == TerminalReason.COMPLETED
    stream_events = [e for e in bus.events if e.type == EventType.LLM_STREAM_EVENT]
    assert len(stream_events) > 0
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) >= 1


@pytest.mark.asyncio
async def test_provider_error_returns_model_error(
    executor: ToolExecutor, registry: ToolRegistry
) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(RuntimeError)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="trigger error")],
        provider=provider,
        config=AgentConfig(max_iterations=2),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=2).run(ctx)
    assert reason == TerminalReason.MODEL_ERROR
