from __future__ import annotations

from bandi.core.dependencies import Dependencies
from bandi.core.event_bus import EventBus


class TestDependencies:
    def test_create_with_minimal_fields(self) -> None:
        bus = EventBus()
        deps = Dependencies(bus=bus, config="dummy")
        assert deps.bus is bus
        assert deps.cost_tracker is None
        assert deps.session_store is None

    def test_create_with_all_fields(self) -> None:
        bus = EventBus()
        deps = Dependencies(
            bus=bus,
            config="test",
            cost_tracker="ct",
            session_store="ss",
        )
        assert deps.cost_tracker == "ct"

    def test_defaults_are_none(self) -> None:
        bus = EventBus()
        deps = Dependencies(bus=bus, config="")
        assert deps.tool_registry is None
        assert deps.recovery_manager is None
