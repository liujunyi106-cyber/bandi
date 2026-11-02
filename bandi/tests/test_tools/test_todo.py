from __future__ import annotations

import pytest

from bandi.tools.task_ledger import TaskLedger, TaskLedgerConfig
from bandi.tools.todo import TodoReadTool, TodoWriteTool


class TestTodoTools:
    @pytest.mark.asyncio
    async def test_read_delegates_ledger(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        ledger.apply_todos([{"title": "task1", "content": "do something", "status": "pending"}])
        tool = TodoReadTool(ledger=ledger)
        result = await tool.execute({})
        assert result["success"]
        assert len(result["result"]["todos"]) == 1
        assert result["result"]["todos"][0]["title"] == "task1"
        assert result["result"]["todos"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_write_delegates_ledger(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        tool = TodoWriteTool(ledger=ledger)
        result = await tool.execute(
            {"todos": [{"id": "1", "title": "task1", "status": "in_progress"}]}
        )
        assert result["success"]
        assert len(ledger.items) == 1
        assert ledger.items[0]["title"] == "task1"
        assert ledger.items[0]["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_write_json_string(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        tool = TodoWriteTool(ledger=ledger)
        result = await tool.execute({"todos": '[{"title": "j1", "status": "completed"}]'})
        assert result["success"]
        assert ledger.items[0]["title"] == "j1"
        assert ledger.items[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_overwrite_existing_id(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        tool = TodoWriteTool(ledger=ledger)
        await tool.execute({"todos": [{"id": "1", "title": "old", "status": "pending"}]})
        await tool.execute({"todos": [{"id": "1", "title": "new", "status": "in_progress"}]})
        read_tool = TodoReadTool(ledger=ledger)
        result = await read_tool.execute({})
        assert len(result["result"]["todos"]) == 1
        assert result["result"]["todos"][0]["title"] == "new"
        assert result["result"]["todos"][0]["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_default_ledger_when_none(self) -> None:
        tool = TodoReadTool()
        result = await tool.execute({})
        assert result["success"]
        assert result["result"]["todos"] == []

    @pytest.mark.asyncio
    async def test_rendered_output(self) -> None:
        ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=False))
        tool = TodoWriteTool(ledger=ledger)
        result = await tool.execute(
            {"todos": [{"title": "a", "status": "pending"}, {"title": "b", "status": "done"}]}
        )
        rendered = result["result"]["rendered"]
        assert "[ ] a" in rendered
        assert "[x] b" in rendered
