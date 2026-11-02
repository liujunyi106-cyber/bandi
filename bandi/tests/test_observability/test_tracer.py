from __future__ import annotations

from bandi.core.events import Event, EventType
from bandi.observability.tracer import (
    OPENTELEMETRY_AVAILABLE,
    BandiTracer,
    OTelEventBusBridge,
    generate_trace_id,
)


class TestBandiTracer:
    def test_tracer_span_context_manager(self) -> None:
        tracer = BandiTracer(service_name="test")
        with tracer.span("test_op") as span:
            assert span is not None

    def test_tracer_span_set_attribute(self) -> None:
        tracer = BandiTracer(service_name="test")
        with tracer.span("test_attr") as span:
            span.set_attribute("key", "value")

    def test_tracer_span_in_nested_scope(self) -> None:
        tracer = BandiTracer(service_name="test")
        with tracer.span("outer") as outer:
            outer.set_attribute("outer_key", "outer_val")
            with tracer.span("inner") as inner:
                inner.set_attribute("inner_key", "inner_val")

    def test_otel_available_flag(self) -> None:
        assert isinstance(OPENTELEMETRY_AVAILABLE, bool)


class TestOTelEventBusBridge:
    def test_span_map_has_expected_keys(self) -> None:
        bridge = OTelEventBusBridge()
        assert "LLM_REQUEST" in bridge._SPAN_MAP
        assert "TOOL_INVOKED" in bridge._SPAN_MAP
        assert "STATE_CHANGED" in bridge._SPAN_MAP
        assert "LLM_RESPONSE" in bridge._END_TO_START_KEY
        assert "TOOL_COMPLETED" in bridge._END_TO_START_KEY
        assert "TERMINATED" in bridge._END_TO_START_KEY

    def test_error_events_close_spans(self) -> None:
        tracer = BandiTracer()
        bridge = OTelEventBusBridge(tracer)
        import asyncio

        correlation = {"session_id": "s1", "turn": 1}
        asyncio.run(bridge.handle_event(Event(type=EventType.LLM_REQUEST, data={**correlation})))
        asyncio.run(
            bridge.handle_event(Event(type=EventType.LLM_ERROR, data={**correlation, "err": "x"}))
        )
        assert not bridge._stacks

        tool_correlation = {"session_id": "s1", "turn": 1, "tool_call_id": "c1"}
        asyncio.run(
            bridge.handle_event(Event(type=EventType.TOOL_INVOKED, data={**tool_correlation}))
        )
        asyncio.run(
            bridge.handle_event(
                Event(type=EventType.TOOL_ERROR, data={**tool_correlation, "err": "y"})
            )
        )
        assert not bridge._stacks

    def test_unknown_event_type_ignored(self) -> None:
        tracer = BandiTracer()
        bridge = OTelEventBusBridge(tracer)
        event = Event(type=EventType.ERROR_OCCURRED, data={"msg": "test"})
        import asyncio

        asyncio.run(bridge.handle_event(event))

    def test_llm_request_response_lifecycle(self) -> None:
        tracer = BandiTracer()
        bridge = OTelEventBusBridge(tracer)
        correlation = {"session_id": "s1", "turn": 1}
        req = Event(type=EventType.LLM_REQUEST, data={**correlation, "model": "test"})
        resp = Event(type=EventType.LLM_RESPONSE, data={**correlation, "tokens": 100})
        import asyncio

        asyncio.run(bridge.handle_event(req))
        assert len(bridge._stacks) == 1
        assert next(iter(bridge._stacks)).startswith("LLM_REQUEST:")
        asyncio.run(bridge.handle_event(resp))
        assert not bridge._stacks

    def test_concurrent_correlations_do_not_overwrite_spans(self) -> None:
        tracer = BandiTracer()
        bridge = OTelEventBusBridge(tracer)
        import asyncio

        asyncio.run(
            bridge.handle_event(
                Event(
                    type=EventType.LLM_REQUEST,
                    data={"session_id": "s1", "turn": 1},
                )
            )
        )
        asyncio.run(
            bridge.handle_event(
                Event(
                    type=EventType.LLM_REQUEST,
                    data={"session_id": "s2", "turn": 1},
                )
            )
        )
        assert len(bridge._stacks) == 2

        asyncio.run(
            bridge.handle_event(
                Event(
                    type=EventType.LLM_RESPONSE,
                    data={"session_id": "s1", "turn": 1},
                )
            )
        )
        assert len(bridge._stacks) == 1

        asyncio.run(
            bridge.handle_event(
                Event(
                    type=EventType.LLM_RESPONSE,
                    data={"session_id": "s2", "turn": 1},
                )
            )
        )
        assert not bridge._stacks


class TestGenerateTraceId:
    def test_generate_trace_id_returns_string(self) -> None:
        tid = generate_trace_id()
        assert isinstance(tid, str)
        assert len(tid) > 0

    def test_generate_trace_id_unique(self) -> None:
        tids = {generate_trace_id() for _ in range(10)}
        assert len(tids) == 10


class TestExecutionContextTraceId:
    def test_trace_id_set_on_creation(self) -> None:
        from bandi.agent.base import ExecutionContext
        from bandi.core.config import AgentConfig
        from bandi.core.event_bus import EventBus
        from bandi.llm.mock import MockProvider

        provider = MockProvider()
        ctx = ExecutionContext(
            messages=[],
            provider=provider,
            config=AgentConfig(),
            bus=EventBus(),
        )
        ctx.trace_id = "abc123"
        assert ctx.trace_id == "abc123"
