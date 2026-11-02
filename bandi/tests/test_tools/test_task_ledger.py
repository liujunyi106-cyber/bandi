from __future__ import annotations

import json
from typing import Any

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import EventType
from bandi.memory.session.store import SessionStore
from bandi.tools.task_ledger import (
    TaskLedger,
    TaskLedgerConfig,
    normalize_todo_status,
    normalize_todos,
    render_todos,
)


class TestNormalizeTodoStatus:
    def test_completed_aliases(self) -> None:
        for value in ("done", "completed", "complete", "finished", "DONE"):
            assert normalize_todo_status(value) == "completed"

    def test_in_progress_aliases(self) -> None:
        for value in ("in_progress", "in-progress", "active", "current"):
            assert normalize_todo_status(value) == "in_progress"

    def test_unknown_and_missing_to_pending(self) -> None:
        assert normalize_todo_status("blocked") == "pending"
        assert normalize_todo_status(None) == "pending"
        assert normalize_todo_status("") == "pending"


class TestNormalizeTodos:
    def test_non_list_returns_empty(self) -> None:
        assert normalize_todos(None) == []
        assert normalize_todos("str") == []
        assert normalize_todos({"a": 1}) == []

    def test_empty_list(self) -> None:
        assert normalize_todos([]) == []

    def test_skips_non_dict_items(self) -> None:
        result = normalize_todos([1, "x", None, {"title": "ok"}])
        assert len(result) == 1
        assert result[0]["title"] == "ok"

    def test_full_fields_kept(self) -> None:
        result = normalize_todos(
            [{"id": "a1", "title": "t", "content": "c", "active_form": "af", "status": "pending"}]
        )
        assert result[0] == {
            "id": "a1",
            "title": "t",
            "content": "c",
            "active_form": "af",
            "status": "pending",
        }

    def test_single_in_progress_constraint(self) -> None:
        result = normalize_todos(
            [
                {"id": "1", "title": "a", "status": "in_progress"},
                {"id": "2", "title": "b", "status": "in_progress"},
            ]
        )
        assert result[0]["status"] == "in_progress"
        assert result[1]["status"] == "pending"

    def test_title_fallback_chain(self) -> None:
        result = normalize_todos(
            [
                {"content": "has-content"},
                {"active_form": "has-active"},
                {},
            ]
        )
        assert result[0]["title"] == "has-content"
        assert result[1]["title"] == "has-active"
        assert result[2]["title"] == "todo-3"

    def test_active_form_camel_compat(self) -> None:
        result = normalize_todos([{"title": "t", "activeForm": "AF"}])
        assert result[0]["active_form"] == "AF"

    def test_id_fallback_index(self) -> None:
        result = normalize_todos([{"title": "t"}, {"title": "u"}])
        assert result[0]["id"] == "1"
        assert result[1]["id"] == "2"


class TestRenderTodos:
    def test_empty(self) -> None:
        assert render_todos([]) == "(no TODO items)"

    def test_status_icons(self) -> None:
        state = normalize_todos(
            [
                {"title": "p", "status": "pending"},
                {"title": "i", "status": "in_progress"},
                {"title": "c", "status": "completed"},
                {"title": "d", "status": "done"},
            ]
        )
        rendered = render_todos(state)
        assert "[ ] p" in rendered
        assert "[>] i" in rendered
        assert "[x] c" in rendered
        assert "[x] d" in rendered

    def test_title_fallback(self) -> None:
        state = [{"id": "id1"}, {"title": "tt"}]
        rendered = render_todos(state)
        assert "id1" in rendered
        assert "tt" in rendered


class FakeWorkItem:
    def __init__(self) -> None:
        self.runtime_metadata: dict[str, Any] = {}


class TestTaskLedger:
    @pytest.mark.asyncio
    async def test_apply_todos_replaces_state(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        result = ledger.apply_todos([{"title": "a", "status": "in_progress"}])
        assert result["success"]
        assert result["result"]["todos"][0]["title"] == "a"
        assert result["result"]["task_ledger"] == result["result"]["todos"]
        assert ledger.items[0]["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_apply_todos_json_string(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        result = ledger.apply_todos('[{"title": "j", "status": "done"}]')
        assert result["success"]
        assert ledger.items[0]["title"] == "j"
        assert ledger.items[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_apply_todos_invalid_json(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        result = ledger.apply_todos("{not json")
        assert not result["success"]
        assert "Invalid todo JSON" in result["error"]
        assert ledger.items == []

    @pytest.mark.asyncio
    async def test_snapshot_returns_current(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        ledger.apply_todos([{"title": "s1"}])
        snapshot = ledger.snapshot()
        assert snapshot["success"]
        assert snapshot["result"]["todos"][0]["title"] == "s1"

    @pytest.mark.asyncio
    async def test_apply_todos_max_items_truncate(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(max_items=2, emit_runtime_events=False))
        ledger.apply_todos([{"title": f"t{i}"} for i in range(5)])
        assert len(ledger.items) == 2

    @pytest.mark.asyncio
    async def test_restore_from_session(self, tmp_path) -> None:
        store = SessionStore(str(tmp_path / "s.db"))
        sid = store.create_session(title="t")
        store.update_session(
            sid, metadata=json.dumps({"task_ledger": [{"title": "old", "status": "done"}]})
        )
        ledger = TaskLedger(
            config=TaskLedgerConfig(emit_runtime_events=False),
            session_id=sid,
            session_store=store,
        )
        ledger.restore()
        assert ledger.items[0]["title"] == "old"
        assert ledger.items[0]["status"] == "completed"
        store.close()

    @pytest.mark.asyncio
    async def test_persist_to_session_roundtrip(self, tmp_path) -> None:
        store = SessionStore(str(tmp_path / "s.db"))
        sid = store.create_session(title="t")
        ledger = TaskLedger(
            config=TaskLedgerConfig(emit_runtime_events=False),
            session_id=sid,
            session_store=store,
        )
        ledger.apply_todos([{"title": "new-item", "status": "pending"}])
        await ledger.persist()
        session = store.get_session(sid)
        assert session is not None
        meta = json.loads(session["metadata"])
        assert meta["task_ledger"][0]["title"] == "new-item"
        ledger2 = TaskLedger(
            config=TaskLedgerConfig(emit_runtime_events=False),
            session_id=sid,
            session_store=store,
        )
        ledger2.restore()
        assert ledger2.items[0]["title"] == "new-item"
        store.close()

    @pytest.mark.asyncio
    async def test_persist_no_session_store_noop(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig())
        ledger.apply_todos([{"title": "x"}])
        await ledger.persist()

    @pytest.mark.asyncio
    async def test_restore_from_work_item(self) -> None:
        wi = FakeWorkItem()
        wi.runtime_metadata["task_ledger"] = [{"title": "wi-task", "status": "in_progress"}]
        ledger = TaskLedger(
            config=TaskLedgerConfig(emit_runtime_events=False),
            work_item_provider=lambda: wi,
        )
        ledger.restore()
        assert ledger.items[0]["title"] == "wi-task"
        assert ledger.items[0]["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_persist_to_work_item(self) -> None:
        wi = FakeWorkItem()
        ledger = TaskLedger(
            config=TaskLedgerConfig(emit_runtime_events=False),
            work_item_provider=lambda: wi,
        )
        ledger.apply_todos([{"title": "wi-write", "status": "completed"}])
        await ledger.persist()
        assert wi.runtime_metadata["task_ledger"][0]["title"] == "wi-write"

    @pytest.mark.asyncio
    async def test_persist_emits_event(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await bus.subscribe(EventType.TASK_LEDGER_UPDATED, handler)
        ledger = TaskLedger(config=TaskLedgerConfig(), event_bus=bus)
        ledger.apply_todos([{"title": "e1"}])
        await ledger.persist()
        assert len(received) == 1
        assert received[0].data["item_count"] == 1

    @pytest.mark.asyncio
    async def test_persist_no_event_when_disabled(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await bus.subscribe(EventType.TASK_LEDGER_UPDATED, handler)
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False), event_bus=bus)
        ledger.apply_todos([{"title": "e2"}])
        await ledger.persist()
        assert received == []

    @pytest.mark.asyncio
    async def test_disabled_config_noop(self, tmp_path) -> None:
        store = SessionStore(str(tmp_path / "s.db"))
        sid = store.create_session(title="t")
        ledger = TaskLedger(
            config=TaskLedgerConfig(enabled=False),
            session_id=sid,
            session_store=store,
        )
        ledger.apply_todos([{"title": "never"}])
        await ledger.persist()
        session = store.get_session(sid)
        assert session is not None
        assert "task_ledger" not in json.loads(session["metadata"])
        store.close()
