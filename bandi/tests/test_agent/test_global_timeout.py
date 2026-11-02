import asyncio

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, Message, MessageRole


class _SlowProvider:
    supports_streaming = False

    @property
    def model_name(self) -> str:
        return "slow"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(self, messages, tools=None, **kwargs):
        await asyncio.sleep(10)
        return LLMResponse(content="never")

    async def stream(self, messages, tools=None, **kwargs):
        yield


class TestAgentLoopTimeout:
    @pytest.mark.asyncio
    async def test_session_timeout_sec_terminates(self) -> None:
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=_SlowProvider(),
            config=AgentConfig(max_iterations=5),
            bus=EventBus(),
        )
        reason = await AgentLoop(max_iterations=5).run(ctx, session_timeout_sec=0.1)
        assert reason == TerminalReason.TIMEOUT

    @pytest.mark.asyncio
    async def test_no_timeout_completes(self) -> None:
        class FastProvider:
            supports_streaming = False

            @property
            def model_name(self) -> str:
                return "fast"

            @property
            def protocol(self) -> str:
                return "openai"

            async def complete(self, messages, tools=None, **kwargs):
                return LLMResponse(content="done")

            async def stream(self, messages, tools=None, **kwargs):
                yield

        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=FastProvider(),
            config=AgentConfig(max_iterations=3),
            bus=EventBus(),
        )
        reason = await AgentLoop(max_iterations=3).run(ctx)
        assert reason == TerminalReason.COMPLETED
