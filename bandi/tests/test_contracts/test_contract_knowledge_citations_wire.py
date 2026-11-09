from __future__ import annotations

import asyncio
import json
from typing import Any

from bandi.core.process_recorder import ProcessRecorder
from bandi.memory.session.store import SessionStore

CITATIONS = [
    {
        "id": "kref_1",
        "label": "[1]",
        "kind": "evidence",
        "title": "产品手册",
        "source_path": "docs/manual.md",
        "chunk_id": "chunk-1",
        "summary": "保修期两年",
    }
]


class _Inner:
    async def on_tool_call_start(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_tool_result(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_tool_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_skill_injected(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_note(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_memory_written(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_ledger(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_done(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_error(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_tool_result_citations_reach_process_entry(tmp_path) -> None:
    store = SessionStore(str(tmp_path / "s.db"))
    session_id = store.create_session(session_id="s1", mode="execute")
    recorder = ProcessRecorder(_Inner(), store, session_id)

    async def run() -> None:
        await recorder.on_tool_call_start(
            session_id, "knowledge_search", {"query": "保修"}, "call-1"
        )
        await recorder.on_tool_result(
            session_id,
            "knowledge_search",
            True,
            "命中 1 条",
            12,
            "call-1",
            citations=CITATIONS,
        )
        await recorder.persist_partial(session_id, 1)

    asyncio.run(run())

    entries = store.get_full_branch(session_id)
    process_entry = next(entry for entry in entries if entry.get("entry_type") == "process")
    nodes = json.loads(process_entry["metadata"])["process"]
    tool_node = next(node for node in nodes if node["kind"] == "tool")
    assert tool_node["citations"] == CITATIONS
    assert tool_node["detail"] == "命中 1 条"
    store.close()
