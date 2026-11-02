from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, Message, MessageRole
from tests.integration.helpers import MockProvider, RecordingBus


@pytest.mark.asyncio
async def test_turn_started_emitted():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hi"))
    bus = RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hello")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED
    types = [e.type for e in bus.events]
    assert EventType.TURN_STARTED in types


@pytest.mark.asyncio
async def test_state_changed_emitted():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hi"))
    bus = RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hello")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    await loop.run(ctx)
    state_events = [e for e in bus.events if e.type == EventType.STATE_CHANGED]
    assert len(state_events) >= 1
    for ev in state_events:
        assert ev.data is not None
        assert "from" in ev.data
        assert "to" in ev.data


@pytest.mark.asyncio
async def test_terminated_emitted_with_reason():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="done"))
    bus = RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    reason = await loop.run(ctx)
    term_events = [e for e in bus.events if e.type == EventType.TERMINATED]
    assert len(term_events) == 1
    data = term_events[0].data
    assert data is not None
    assert data["terminal_reason"] == reason.name


@pytest.mark.asyncio
async def test_model_error_emitted_on_provider_failure():
    provider = MockProvider()
    bus = RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    reason = await loop.run(ctx)
    term_events = [e for e in bus.events if e.type == EventType.TERMINATED]
    assert len(term_events) == 1
    assert reason == TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_state_transition_order():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hello"))
    bus = RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    await loop.run(ctx)
    state_events = [e for e in bus.events if e.type == EventType.STATE_CHANGED]
    assert len(state_events) >= 2
    first = state_events[0].data
    assert first is not None
    assert first["from"] == "init"
    assert first["to"] == "thinking"
