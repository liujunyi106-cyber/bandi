from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from bandi.agent.base import ExecutionContext
from bandi.core.message import Message, MessageRole
from bandi.eval.badcase_tracker import BadCaseTracker
from bandi.llm.mock import MockProvider


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def ctx() -> ExecutionContext:
    return ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hello")],
        provider=MockProvider(),
        config=MagicMock(),
        bus=MagicMock(),
        meta={"session_id": "sess-test"},
    )


class TestBadCaseTracker:
    def test_record_and_list(self, conn: sqlite3.Connection, ctx: ExecutionContext) -> None:
        tracker = BadCaseTracker(conn)
        bc_id = tracker.record(ctx, "safety_violation")
        items = tracker.list()
        assert len(items) == 1
        assert items[0].id == bc_id
        assert items[0].failure_type == "safety_violation"
        assert items[0].severity == "critical"

    def test_record_raw(self, conn: sqlite3.Connection) -> None:
        tracker = BadCaseTracker(conn)
        tracker.record_raw(
            session_id="sess-001",
            user_input="do something",
            agent_output="I did a bad thing",
            failure_type="tool_error",
            detail="timeout",
            severity="high",
        )
        items = tracker.list()
        assert len(items) == 1
        assert items[0].session_id == "sess-001"
        assert items[0].severity == "high"

    def test_record_masks_sensitive_input(
        self, conn: sqlite3.Connection, ctx: ExecutionContext
    ) -> None:
        ctx.raw_input = "my password is hunter2secret"
        ctx.current_content = "ok done"
        tracker = BadCaseTracker(conn, masker=lambda m: m.replace("hunter2secret", "****"))
        tracker.record(ctx, "safety_violation")
        items = tracker.list()
        assert "hunter2secret" not in items[0].user_input, "敏感输入明文落库"
        assert "****" in items[0].user_input

    def test_list_filter(self, conn: sqlite3.Connection, ctx: ExecutionContext) -> None:
        tracker = BadCaseTracker(conn)
        tracker.record(ctx, "safety_violation")
        tracker.record_raw(
            session_id="s2", user_input="x", agent_output="y", failure_type="tool_error"
        )
        safety_items = tracker.list(failure_type="safety_violation")
        assert len(safety_items) == 1
        tool_items = tracker.list(failure_type="tool_error")
        assert len(tool_items) == 1

    def test_stats(self, conn: sqlite3.Connection, ctx: ExecutionContext) -> None:
        tracker = BadCaseTracker(conn)
        tracker.record(ctx, "safety_violation")
        tracker.record_raw(
            session_id="s2", user_input="x", agent_output="y", failure_type="tool_error"
        )
        tracker.record_raw(
            session_id="s3", user_input="z", agent_output="w", failure_type="tool_error"
        )
        s = tracker.stats()
        assert s["total"] == 3
        assert s["by_type"]["tool_error"] == 2
        assert s["by_type"]["safety_violation"] == 1
        assert s["by_severity"]["critical"] == 1
        assert s["by_severity"]["medium"] == 2

    def test_idempotent_table(self, conn: sqlite3.Connection, ctx: ExecutionContext) -> None:
        tracker1 = BadCaseTracker(conn)
        tracker1.record(ctx, "safety_violation")
        tracker2 = BadCaseTracker(conn)
        items = tracker2.list()
        assert len(items) == 1
