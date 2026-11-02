from __future__ import annotations

import pytest

from bandi.agent.base import ExecutionContext, QueryState, TerminalReason
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.llm.mock import MockProvider


class TestQueryState:
    def test_frozen_immutable(self):
        qs = QueryState()
        with pytest.raises(AttributeError):
            qs.current_state = "thinking"

    def test_with_state_returns_new_instance(self):
        qs = QueryState(current_state="init")
        qs2 = qs.with_state(current_state="thinking")
        assert qs.current_state == "init"
        assert qs2.current_state == "thinking"
        assert qs is not qs2

    def test_with_state_unknown_field_raises(self):
        qs = QueryState()
        with pytest.raises(ValueError, match="unknown QueryState field"):
            qs.with_state(nonexistent=True)

    def test_default_values(self):
        qs = QueryState()
        assert qs.current_state == "init"
        assert qs.terminal_reason is None
        assert qs.final_message is None
        assert qs.total_tokens == 0
        assert qs.turn_index == 0
        assert qs.tool_sequence_hashes == ()

    def test_tuple_immutability(self):
        qs = QueryState(tool_sequence_hashes=("a", "b"))
        with pytest.raises(AttributeError):
            qs.tool_sequence_hashes.append("c")  # type: ignore[attr-defined]

    def test_multiple_with_state_chain(self):
        qs = QueryState()
        qs2 = qs.with_state(current_state="parsing", total_tokens=100)
        qs3 = qs2.with_state(current_state="executing", total_tokens=150)
        assert qs3.current_state == "executing"
        assert qs3.total_tokens == 150
        assert qs2.total_tokens == 100


class TestExecutionContextQueryState:
    def make_ctx(self) -> ExecutionContext:
        return ExecutionContext(
            messages=[],
            provider=MockProvider(),
            config=AgentConfig(),
            bus=EventBus(),
        )

    def test_to_query_state_roundtrip(self):
        ctx = self.make_ctx()
        ctx.current_state = "thinking"
        ctx.total_tokens = 500
        ctx.turn_index = 3
        ctx.clarification_injected = True
        ctx.tool_sequence_hashes = ["hash1", "hash2"]
        ctx.compress.compress_ok = True

        qs = ctx.to_query_state()
        assert qs.current_state == "thinking"
        assert qs.total_tokens == 500
        assert qs.turn_index == 3
        assert qs.clarification_injected is True
        assert qs.tool_sequence_hashes == ("hash1", "hash2")
        assert qs.compress_ok is True

    def test_apply_query_state_restores(self):
        ctx = self.make_ctx()
        qs = QueryState(
            current_state="executing",
            total_tokens=1000,
            turn_index=5,
            terminal_reason="COMPLETED",
            final_message="done",
            clarification_injected=True,
            tool_sequence_hashes=("a", "b"),
            compress_ok=True,
            compress_failed_count=2,
            rework_count=1,
            rework_feedback="try again",
        )
        ctx.apply_query_state(qs)
        assert ctx.current_state == "executing"
        assert ctx.total_tokens == 1000
        assert ctx.turn_index == 5
        assert ctx.terminal_reason == TerminalReason.COMPLETED
        assert ctx.final_message == "done"
        assert ctx.clarification_injected is True
        assert ctx.tool_sequence_hashes == ["a", "b"]
        assert ctx.compress.compress_ok is True
        assert ctx.compress.compress_failed_count == 2
        assert ctx.verifier_state.rework_count == 1
        assert ctx.verifier_state.rework_feedback == "try again"

    def test_apply_query_state_clears_terminal_reason(self):
        ctx = self.make_ctx()
        ctx.terminal_reason = TerminalReason.COMPLETED

        qs = QueryState(terminal_reason=None)
        ctx.apply_query_state(qs)
        assert ctx.terminal_reason is None

    def test_to_query_state_includes_terminal_reason(self):
        ctx = self.make_ctx()
        ctx.terminal_reason = TerminalReason.COMPLETED
        qs = ctx.to_query_state()
        assert qs.terminal_reason == "COMPLETED"

    def test_to_query_state_none_terminal_reason(self):
        ctx = self.make_ctx()
        qs = ctx.to_query_state()
        assert qs.terminal_reason is None

    def test_from_state_dict_roundtrip(self):
        import dataclasses
        import json

        ctx = self.make_ctx()
        ctx.current_state = "observing"
        ctx.total_tokens = 250
        ctx.turn_index = 7

        qs = ctx.to_query_state()
        raw = json.loads(json.dumps(dataclasses.asdict(qs)))
        restored = QueryState(**raw)
        ctx2 = self.make_ctx()
        ctx2.apply_query_state(restored)

        assert ctx2.current_state == "observing"
        assert ctx2.total_tokens == 250
        assert ctx2.turn_index == 7
        assert ctx2.terminal_reason is None

    def test_apply_unknown_reason_ignored(self):
        ctx = self.make_ctx()
        qs = QueryState(terminal_reason="UNKNOWN_REASON")
        ctx.apply_query_state(qs)
        assert ctx.terminal_reason is None
