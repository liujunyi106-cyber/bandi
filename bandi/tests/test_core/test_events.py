from bandi.core.events import EventType


class TestEventTypes:
    def test_core_event_types_exist(self) -> None:
        core = {
            EventType.STATE_CHANGED,
            EventType.TURN_STARTED,
            EventType.TERMINATED,
            EventType.LLM_REQUEST,
            EventType.LLM_RESPONSE,
            EventType.LLM_ERROR,
            EventType.TOOL_INVOKED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_ERROR,
            EventType.ERROR_OCCURRED,
            EventType.DEGRADATION,
            EventType.SAFETY_GUARD_TRIGGERED,
            EventType.PII_DETECTED,
            EventType.LLM_STREAM_EVENT,
        }
        for et in core:
            assert et in EventType.__members__.values()

    def test_dead_event_types_removed(self) -> None:
        """B5b 8.5：零发射死类型已删除（删除优先；再引入须带生产发射点）。"""
        gone = {
            "TURN_COMPLETED",
            "SESSION_SAVED",
            "ONTOLOGY_UPDATED",
            "AGENT_MESSAGE",
            "AGENT_HANDOFF",
        }
        assert not (gone & set(EventType.__members__))

    def test_cross_agent_event_types_exist(self) -> None:
        cross = {EventType.AGENT_REQUEST}
        for et in cross:
            assert et in EventType.__members__.values()

    def test_cross_agent_event_names(self) -> None:
        assert EventType.AGENT_REQUEST.name == "AGENT_REQUEST"

    def test_b5b_events_present(self) -> None:
        assert EventType.COMPRESSION_COMPLETED.name == "COMPRESSION_COMPLETED"
        assert EventType.MEMORY_WRITTEN.name == "MEMORY_WRITTEN"

    def test_event_type_count(self) -> None:
        assert len(EventType) >= 20
