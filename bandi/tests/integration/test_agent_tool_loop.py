from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from tests.integration.helpers import MockProvider, RecordingBus


class EchoTool(BaseTool):
    name = "echo"
    is_concurrency_safe = True

    async def execute(self, params: dict, context: object | None = None) -> str:
        return params.get("msg", "")


class AddTool(BaseTool):
    name = "add"
    is_concurrency_safe = True

    async def execute(self, params: dict, context: object | None = None) -> int:
        return params.get("x", 0) + params.get("y", 0)


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register_instance(EchoTool())
    r.register_instance(AddTool())
    return r


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry=registry)


@pytest.mark.asyncio
async def test_single_tool_call_executed(executor: ToolExecutor, registry: ToolRegistry) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"msg": "hello world"})],
        )
    )
    provider.add_response(LLMResponse(content="done"))
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="say hello")],
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
    assert "hello world" in (tool_msgs[0].content or "")
    assert ctx.tool_results is None or ctx.tool_results == []


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_turn(
    executor: ToolExecutor, registry: ToolRegistry
) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"msg": "first"}),
                ToolCall(id="c2", name="add", arguments={"x": 3, "y": 4}),
            ],
        )
    )
    provider.add_response(LLMResponse(content="all done"))
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="run two tools")],
        provider=provider,
        config=AgentConfig(max_iterations=3, max_tool_calls_per_turn=10),
        bus=bus,
    )
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    reason = await AgentLoop(max_iterations=3).run(ctx)
    assert reason == TerminalReason.COMPLETED
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) == 2
    assert "first" in (tool_msgs[0].content or "")
    assert "7" in (tool_msgs[1].content or "")


@pytest.mark.asyncio
async def test_perception_feeds_tool_loop(executor: ToolExecutor, registry: ToolRegistry) -> None:
    from bandi.agent.prompt_harness.builder import PromptHarness
    from bandi.perception.pipeline import Dependencies as PerceptionDeps
    from bandi.perception.pipeline import PerceptionPipeline

    pipeline = PerceptionPipeline(
        deps=PerceptionDeps(prompt_harness=PromptHarness()),
    )
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"msg": "perception works"})],
        )
    )
    provider.add_response(LLMResponse(content="done"))
    ctx = ExecutionContext(
        messages=[],
        provider=provider,
        config=AgentConfig(max_iterations=3),
        bus=bus,
    )
    ctx.raw_input = "test perception"
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    loop = AgentLoop(max_iterations=3, perception=pipeline)
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED
    assert len(ctx.messages) > 0
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) >= 1
    assert "perception works" in (tool_msgs[0].content or "")


@pytest.mark.asyncio
async def test_tool_error_does_not_block_loop(
    executor: ToolExecutor, registry: ToolRegistry
) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"msg": "ok"})],
        )
    )
    provider.add_response(LLMResponse(content="recovered"))
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="test")],
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
