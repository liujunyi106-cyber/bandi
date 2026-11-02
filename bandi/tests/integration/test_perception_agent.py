from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.perception.pipeline import Dependencies as PerceptionDeps
from bandi.perception.pipeline import PerceptionPipeline
from tests.integration.helpers import MockProvider, RecordingBus


@pytest.mark.asyncio
async def test_perception_feeds_messages_into_agent():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hello from agent"))
    bus = RecordingBus()
    pipeline = PerceptionPipeline(
        deps=PerceptionDeps(prompt_harness=PromptHarness()),
    )
    ctx = ExecutionContext(
        messages=[],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    ctx.raw_input = "say hi"
    loop = AgentLoop(perception=pipeline)
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED
    assert len(ctx.messages) >= 2
    user_msgs = [m for m in ctx.messages if m.role == MessageRole.USER]
    assert len(user_msgs) >= 1


@pytest.mark.asyncio
async def test_perception_skipped_when_no_raw_input():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="ok"))
    bus = RecordingBus()
    pipeline = PerceptionPipeline(
        deps=PerceptionDeps(prompt_harness=PromptHarness()),
    )
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    ctx.raw_input = ""
    loop = AgentLoop(perception=pipeline)
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_perception_with_context_registry():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="ok"))
    bus = RecordingBus()
    pipeline = PerceptionPipeline(
        deps=PerceptionDeps(prompt_harness=PromptHarness()),
    )
    ctx = ExecutionContext(
        messages=[],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    ctx.raw_input = "what is memo"
    loop = AgentLoop(perception=pipeline)
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED
