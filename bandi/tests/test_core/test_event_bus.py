from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from bandi.core.event_bus import EventBus, bootstrap_event_bus
from bandi.core.events import Event, EventType


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_emit(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.TURN_STARTED, handler)
        event = Event(type=EventType.TURN_STARTED, data={"turn": 1})
        await bus.emit(event)
        assert len(received) == 1
        assert received[0].type == EventType.TURN_STARTED
        assert received[0].data == {"turn": 1}

    @pytest.mark.asyncio
    async def test_multiple_handlers_for_same_event(self) -> None:
        bus = EventBus()
        results: list[str] = []

        async def h1(event: Event) -> None:
            results.append("h1")

        async def h2(event: Event) -> None:
            results.append("h2")

        await bus.subscribe(EventType.TERMINATED, h1)
        await bus.subscribe(EventType.TERMINATED, h2)
        await bus.emit(Event(type=EventType.TERMINATED))
        assert sorted(results) == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.TURN_STARTED, handler)
        await bus.emit(Event(type=EventType.TURN_STARTED))
        assert len(received) == 1

        await bus.unsubscribe(EventType.TURN_STARTED, handler)
        await bus.emit(Event(type=EventType.TURN_STARTED))
        assert len(received) == 1  # unchanged

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent(self) -> None:
        bus = EventBus()

        async def handler(event: Event) -> None:
            pass

        # should not raise
        await bus.unsubscribe(EventType.TERMINATED, handler)

    @pytest.mark.asyncio
    async def test_emit_with_no_handlers(self) -> None:
        bus = EventBus()
        # should not raise
        await bus.emit(Event(type=EventType.TERMINATED))

    @pytest.mark.asyncio
    async def test_emit_only_triggers_matching_type(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.TURN_STARTED, handler)
        await bus.emit(Event(type=EventType.TERMINATED))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_exception_in_handler_does_not_crash(self) -> None:
        bus = EventBus()

        async def failing_handler(event: Event) -> None:
            raise ValueError("handler failed")

        async def good_handler(event: Event) -> None:
            good_handler.called = True

        good_handler.called = False

        await bus.subscribe(EventType.TERMINATED, failing_handler)
        await bus.subscribe(EventType.TERMINATED, good_handler)
        await bus.emit(Event(type=EventType.TERMINATED))
        assert good_handler.called is True

    @pytest.mark.asyncio
    async def test_subscribe_same_handler_twice_called_twice(self) -> None:
        bus = EventBus()
        count = 0

        async def handler(event: Event) -> None:
            nonlocal count
            count += 1

        await bus.subscribe(EventType.TURN_STARTED, handler)
        await bus.subscribe(EventType.TURN_STARTED, handler)
        await bus.emit(Event(type=EventType.TURN_STARTED))
        assert count == 2

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_only_exact_handler(self) -> None:
        bus = EventBus()
        results: list[str] = []

        async def h1(event: Event) -> None:
            results.append("h1")

        async def h2(event: Event) -> None:
            results.append("h2")

        await bus.subscribe(EventType.TERMINATED, h1)
        await bus.subscribe(EventType.TERMINATED, h2)
        await bus.unsubscribe(EventType.TERMINATED, h1)
        await bus.emit(Event(type=EventType.TERMINATED))
        assert results == ["h2"]

    @pytest.mark.asyncio
    async def test_emit_with_data(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.LLM_RESPONSE, handler)
        data = {"provider": "deepseek", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        await bus.emit(Event(type=EventType.LLM_RESPONSE, data=data))
        assert received[0].data["provider"] == "deepseek"
        assert received[0].data["usage"]["prompt_tokens"] == 100

    @pytest.mark.asyncio
    async def test_concurrent_emit_safety(self) -> None:
        bus = EventBus()
        results: list[int] = []

        async def handler(event: Event) -> None:
            results.append(1)
            await asyncio.sleep(0)

        await bus.subscribe(EventType.TERMINATED, handler)
        await asyncio.gather(
            bus.emit(Event(type=EventType.TERMINATED)),
            bus.emit(Event(type=EventType.TERMINATED)),
            bus.emit(Event(type=EventType.TERMINATED)),
        )
        assert len(results) == 3


@dataclass
class StubDeps:
    cost_tracker: Any = None
    session_store: Any = None
    logger: Any = None
    trace_explainer: Any = None


class TestBootstrapEventBus:
    @pytest.mark.asyncio
    async def test_no_deps_does_not_crash(self) -> None:
        bus = EventBus()
        deps = StubDeps()
        await bootstrap_event_bus(bus, deps)
        # should not raise

    @pytest.mark.asyncio
    async def test_subscribes_cost_tracker(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        class FakeCostTracker:
            async def on_llm_response(self, event: Event) -> None:
                calls.append("llm")

            async def on_tool_done(self, event: Event) -> None:
                calls.append("tool")

        deps = StubDeps(cost_tracker=FakeCostTracker())
        await bootstrap_event_bus(bus, deps)
        await bus.emit(Event(type=EventType.LLM_RESPONSE))
        await bus.emit(Event(type=EventType.TOOL_COMPLETED))
        assert "llm" in calls
        assert "tool" in calls

    @pytest.mark.asyncio
    async def test_subscribes_session_store(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        class FakeSessionStore:
            async def on_state_change(self, event: Event) -> None:
                calls.append("state")

            async def on_terminated(self, event: Event) -> None:
                calls.append("term")

        deps = StubDeps(session_store=FakeSessionStore())
        await bootstrap_event_bus(bus, deps)
        await bus.emit(Event(type=EventType.STATE_CHANGED))
        await bus.emit(Event(type=EventType.TERMINATED))
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_subscribes_logger(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        class FakeLogger:
            async def on_error(self, event: Event) -> None:
                calls.append("error")

            async def on_degradation(self, event: Event) -> None:
                calls.append("degradation")

        deps = StubDeps(logger=FakeLogger())
        await bootstrap_event_bus(bus, deps)
        await bus.emit(Event(type=EventType.ERROR_OCCURRED))
        await bus.emit(Event(type=EventType.DEGRADATION))
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_skips_none_objects(self) -> None:
        bus = EventBus()
        deps = StubDeps(cost_tracker=None, session_store=None, logger=None, trace_explainer=None)
        await bootstrap_event_bus(bus, deps)
        await bus.emit(Event(type=EventType.LLM_RESPONSE))
        await bus.emit(Event(type=EventType.TERMINATED))

    @pytest.mark.asyncio
    async def test_starts_trace_explainer(self) -> None:
        bus = EventBus()
        started = False

        class FakeTraceExplainer:
            async def start(self) -> None:
                nonlocal started
                started = True

        deps = StubDeps(trace_explainer=FakeTraceExplainer())
        await bootstrap_event_bus(bus, deps)
        assert started is True

    @pytest.mark.asyncio
    async def test_skips_missing_method(self) -> None:
        bus = EventBus()

        class IncompleteTracker:
            pass

        deps = StubDeps(cost_tracker=IncompleteTracker())
        await bootstrap_event_bus(bus, deps)
        await bus.emit(Event(type=EventType.LLM_RESPONSE))

    @pytest.mark.asyncio
    async def test_persist_flush_is_batched(self, tmp_path) -> None:
        bus = EventBus(persist_path=tmp_path / "events.jsonl")
        bus.enable_persistence()
        flushes = {"n": 0}
        orig_flush = bus._persist_file.flush

        def counting_flush() -> None:
            flushes["n"] += 1
            orig_flush()

        bus._persist_file.flush = counting_flush  # type: ignore[method-assign]
        for i in range(25):
            await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))
        assert flushes["n"] < 25, f"flush 必须批量，实际 {flushes['n']} 次"
        await bus.close()
        lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 25, f"close 后全部事件必须落盘，实际 {len(lines)}"

    @pytest.mark.asyncio
    async def test_persist_masks_sensitive_data(self, tmp_path) -> None:
        bus = EventBus(persist_path=tmp_path / "events.jsonl")
        bus.enable_persistence()
        await bus.emit(
            Event(
                type=EventType.LLM_RESPONSE,
                data={"api_key": "sk-secret12345", "content": "hello", "ok": True},
            )
        )
        await bus.close()
        raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        assert "sk-secret12345" not in raw, "api_key 明文泄漏进事件落盘"
        assert "hello" in raw, "非敏感内容必须保留"

    @pytest.mark.asyncio
    async def test_unserializable_data_does_not_break_dispatch(self, tmp_path) -> None:
        bus = EventBus(persist_path=tmp_path / "events.jsonl")
        bus.enable_persistence()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe(EventType.LLM_RESPONSE, handler)

        class _Unserializable:
            pass

        await bus.emit(Event(type=EventType.LLM_RESPONSE, data={"obj": _Unserializable(), "n": 1}))
        assert len(received) == 1, "持久化失败不得阻断 handler 分发"
        assert received[0].data["n"] == 1
        await bus.close()
