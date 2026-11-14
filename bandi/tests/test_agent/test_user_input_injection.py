# 功能：实时转向测试（USER_INPUT_INJECTED + inject_input + inject_user_input）
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.loop import AgentLoop
from bandi.core.agent_runner import inject_user_input
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import MessageRole
from bandi.llm.mock import MockProvider


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def ctx(mock_provider: MockProvider, bus: EventBus) -> ExecutionContext:
    return ExecutionContext(
        messages=[],
        provider=mock_provider,
        config=AgentConfig(),
        bus=bus,
    )


class TestInjectUserInput:
    def test_inject_no_active_context_returns_false(self) -> None:
        assert inject_user_input("nonexistent-session", "hello") is False

    def test_inject_with_active_context_sets_flag(self, ctx: ExecutionContext) -> None:
        from bandi.core.agent_runner import _register_active_context, _unregister_active_context

        session_id = "test-session"
        _register_active_context(session_id, ctx)
        try:
            result = inject_user_input(session_id, "stop that!")
            assert result is True
            assert ctx.injected_input == "stop that!"
            assert ctx._injected_consumed is False
        finally:
            _unregister_active_context(session_id)

    def test_inject_clears_previous_injection(self, ctx: ExecutionContext) -> None:
        from bandi.core.agent_runner import _register_active_context, _unregister_active_context

        session_id = "test-session"
        _register_active_context(session_id, ctx)
        try:
            inject_user_input(session_id, "first input")
            inject_user_input(session_id, "second input")
            assert "first input" in ctx.injected_input
            assert "second input" in ctx.injected_input
            assert "first input\nsecond input" == ctx.injected_input
        finally:
            _unregister_active_context(session_id)


class TestAgentLoopInjection:
    """Test that the loop detects injection and re-starts thinking."""

    @pytest.mark.asyncio
    async def test_loop_detects_injection_and_restarts_thinking(
        self, ctx: ExecutionContext, mock_provider: MockProvider
    ) -> None:
        mock_response = MagicMock()
        mock_response.content = "Alright, doing something new."
        mock_response.tool_calls = []
        mock_response.finish_reason = "stop"
        mock_provider.complete = AsyncMock(return_value=mock_response)
        mock_provider.count_tokens = MagicMock(return_value=5)

        loop = AgentLoop(max_iterations=5)
        ctx.current_state = "observing"
        ctx.injected_input = "switch task"

        await loop.run(ctx)

        # Should have processed injection: state went through thinking
        assert ctx.current_state == "terminated" or ctx.current_state == "reflecting"
        # (we verify by checking the loop didn't crash and state progressed)
        assert ctx.step_index >= 1

    @pytest.mark.asyncio
    async def test_injection_emits_user_input_injected_event(
        self, ctx: ExecutionContext, mock_provider: MockProvider
    ) -> None:
        captured_events: list[Event] = []

        async def capture(event: Event) -> None:
            captured_events.append(event)

        await ctx.bus.subscribe(EventType.USER_INPUT_INJECTED, capture)

        mock_response = MagicMock()
        mock_response.content = "OK"
        mock_response.tool_calls = []
        mock_response.finish_reason = "stop"
        mock_provider.complete = AsyncMock(return_value=mock_response)
        mock_provider.count_tokens = MagicMock(return_value=3)

        loop = AgentLoop(max_iterations=5)
        ctx.current_state = "observing"
        ctx.injected_input = "new instruction"

        await loop.run(ctx)

        injected_events = [e for e in captured_events if e.type == EventType.USER_INPUT_INJECTED]
        assert len(injected_events) >= 1
        assert injected_events[0].data["turn_index"] >= 1

    @pytest.mark.asyncio
    async def test_injection_appends_user_message(
        self, ctx: ExecutionContext, mock_provider: MockProvider
    ) -> None:
        mock_response = MagicMock()
        mock_response.content = "Understood."
        mock_response.tool_calls = []
        mock_response.finish_reason = "stop"
        mock_provider.complete = AsyncMock(return_value=mock_response)
        mock_provider.count_tokens = MagicMock(return_value=3)

        loop = AgentLoop(max_iterations=5)
        ctx.messages = [
            MagicMock(role=MessageRole.USER, content="original"),
            MagicMock(role=MessageRole.ASSISTANT, content="reply"),
        ]
        ctx.current_state = "observing"
        ctx.injected_input = "follow up question"

        await loop.run(ctx)

        user_messages = [m for m in ctx.messages if m.role == MessageRole.USER]
        assert len(user_messages) >= 2  # original + injected
        assert "follow up question" in user_messages[-1].content

    def test_clear_injection(self, ctx: ExecutionContext) -> None:
        ctx.inject_input("pending input")
        assert ctx.injected_input == "pending input"
        assert ctx._injected_consumed is False
        ctx.clear_injection()
        assert ctx.injected_input is None
        assert ctx._injected_consumed is False

    def test_inject_input_does_not_clobber_state(self, ctx: ExecutionContext) -> None:
        """修订：外部注入不抢写主循环 state（工具执行期外部改写
        会让状态机提前终止）；状态由 loop 消费点 `_consume_injected_input` 置
        thinking（见 test_contract_p1p2_b5a_state 同族契约）。"""
        ctx.current_state = "executing"
        ctx.inject_input("urgent change")
        assert ctx.injected_input == "urgent change"
        assert ctx.current_state == "executing", "外部注入不得抢写主循环状态"

    def test_inject_does_not_overwrite_raw_input(self, ctx: ExecutionContext) -> None:
        ctx.raw_input = "original user request"
        ctx.inject_input("urgent change")
        assert ctx.raw_input == "original user request", (
            "raw_input must not be overwritten by injection"
        )
        assert ctx.injected_text == "urgent change"

    @pytest.mark.asyncio
    async def test_injection_preserves_raw_input_after_processing(
        self, ctx: ExecutionContext, mock_provider: MockProvider
    ) -> None:
        mock_response = MagicMock()
        mock_response.content = "OK"
        mock_response.tool_calls = []
        mock_response.finish_reason = "stop"
        mock_provider.complete = AsyncMock(return_value=mock_response)
        mock_provider.count_tokens = MagicMock(return_value=3)

        loop = AgentLoop(max_iterations=5)
        ctx.raw_input = "original task"
        ctx.current_state = "observing"
        ctx.injected_input = "intermediate instruction"

        await loop.run(ctx)

        assert ctx.raw_input == "original task", "injection must not overwrite original raw_input"
        # injected_text 在消费后清空（与 injected_input 同语义）
        assert ctx.injected_input is None, "injection flag must be consumed"
