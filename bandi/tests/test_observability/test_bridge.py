from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.observability.bridge import MetricsBridge

pytestmark = pytest.mark.asyncio


class TestMetricsBridge:
    async def test_wire_subscribes_all_events(self) -> None:
        bus = EventBus()
        bridge = MetricsBridge()
        await bridge.wire(bus)
        for et in (
            EventType.LLM_REQUEST,
            EventType.LLM_RESPONSE,
            EventType.LLM_ERROR,
            EventType.TOOL_INVOKED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_ERROR,
            EventType.TERMINATED,
        ):
            assert et in bus._handlers and len(bus._handlers[et]) > 0

    async def test_llm_events_record_call(self) -> None:
        bridge = MetricsBridge()
        bridge._metrics = MagicMock()
        await bridge.on_llm_request(
            Event(type=EventType.LLM_REQUEST, data={"provider": "p", "model": "m"})
        )
        await bridge.on_llm_response(
            Event(
                type=EventType.LLM_RESPONSE,
                data={
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        )
        bridge._metrics.record_llm_call.assert_called_once()
        args = bridge._metrics.record_llm_call.call_args
        assert args.kwargs["provider"] == "deepseek"
        assert args.kwargs["status"] == "success"
        assert args.kwargs["prompt_tokens"] == 10
        assert args.kwargs["completion_tokens"] == 5
        assert args.kwargs["duration"] >= 0.0

    async def test_llm_error_records_error_status(self) -> None:
        bridge = MetricsBridge()
        bridge._metrics = MagicMock()
        await bridge.on_llm_request(Event(type=EventType.LLM_REQUEST, data={}))
        await bridge.on_llm_error(
            Event(type=EventType.LLM_ERROR, data={"provider": "p", "model": "m", "error": "boom"})
        )
        assert bridge._metrics.record_llm_call.call_args.kwargs["status"] == "error"

    async def test_tool_events_record_call(self) -> None:
        bridge = MetricsBridge()
        bridge._metrics = MagicMock()
        await bridge.on_tool_invoked(Event(type=EventType.TOOL_INVOKED, data={"tool_name": "bash"}))
        await bridge.on_tool_completed(
            Event(type=EventType.TOOL_COMPLETED, data={"tool_name": "bash", "duration_ms": 1200})
        )
        bridge._metrics.record_tool_call.assert_called_once_with("bash", 1.2, success=True)

    async def test_tool_error_records_failure(self) -> None:
        bridge = MetricsBridge()
        bridge._metrics = MagicMock()
        await bridge.on_tool_invoked(Event(type=EventType.TOOL_INVOKED, data={"tool_name": "read"}))
        await bridge.on_tool_error(
            Event(type=EventType.TOOL_ERROR, data={"tool_name": "read", "error_message": "boom"})
        )
        bridge._metrics.record_tool_call.assert_called_once_with(
            "read", pytest.approx(0.0, abs=60), success=False
        )

    async def test_terminated_records_agent_turn(self) -> None:
        bridge = MetricsBridge()
        bridge._metrics = MagicMock()
        await bridge.on_terminated(
            Event(type=EventType.TERMINATED, data={"terminal_reason": "COMPLETED"})
        )
        bridge._metrics.record_agent_turn.assert_called_once_with("COMPLETED")

    async def test_llm_duration_resets_after_call(self) -> None:
        bridge = MetricsBridge()
        bridge._metrics = MagicMock()
        await bridge.on_llm_request(Event(type=EventType.LLM_REQUEST, data={}))
        await bridge.on_llm_response(Event(type=EventType.LLM_RESPONSE, data={"usage": {}}))
        await bridge.on_llm_response(Event(type=EventType.LLM_RESPONSE, data={"usage": {}}))
        second = bridge._metrics.record_llm_call.call_args.kwargs["duration"]
        assert second == 0.0
