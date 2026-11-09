"""契约：记忆写入注记落库必须带通道明细。

缺陷实证：process 条目只存 `count`，不存 `channels` ⇒ 会话重载后前端拿不到通道名，
只能回落成「计入记忆 xN」，按通道分档（长期/情景/草稿）在刷新后全部失效。
同一轮多次写入还要合并成一行并累加通道，不能只留第一次的条数。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from bandi.memory.session.store import SessionStore


class _StubInner:
    """功能：记录下游收到的写入调用——本契约只看落库，不看下游。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        self.calls.append(list(channels))


def _persist(db_path: Path, rounds: list[list[str]]) -> list[dict[str, Any]]:
    """跑若干轮写入并落库，返回 process 条目里的全部注记节点。"""
    from bandi.core.process_recorder import ProcessRecorder

    store = SessionStore(str(db_path))
    sid = store.create_session(mode="execute")
    recorder = ProcessRecorder(_StubInner(), store, sid)
    for channels in rounds:
        asyncio.run(recorder.on_memory_written(sid, channels))
    asyncio.run(recorder.persist_partial(sid, 1))
    store.close()

    conn = sqlite3.connect(str(db_path))
    try:
        metas = [
            json.loads(m)
            for (m,) in conn.execute(
                "select metadata from session_entries where session_id=? and entry_type='process'",
                (sid,),
            )
        ]
    finally:
        conn.close()
    return [n for m in metas for n in m.get("process", []) if n.get("noteType") == "memory_written"]


def test_persisted_entry_carries_channels(tmp_path: Path) -> None:
    """端到端落库：重载会话库拿回的注记仍带 channels，前端才能分档。"""
    notes = _persist(tmp_path / "sess.db", [["episodic", "working"]])
    assert len(notes) == 1, f"落库须有一条 memory_written 注记，实际 {len(notes)}"
    assert notes[0].get("channels") == ["episodic", "working"], (
        f"落库条目须带 channels（重载后前端才能分档），实际 {notes[0].get('channels')}"
    )
    assert notes[0].get("count") == 2, f"条数须与通道明细一致，实际 {notes[0].get('count')}"


def test_repeated_writes_merge_into_one_row(tmp_path: Path) -> None:
    """同一轮多次写入合并成一行，通道明细累加而非只留第一次。"""
    notes = _persist(tmp_path / "sess2.db", [["episodic"], ["long_term"], ["working"]])
    assert len(notes) == 1, f"同一轮多次写入须合并成一行，实际 {len(notes)}"
    assert notes[0].get("channels") == ["episodic", "long_term", "working"], (
        f"通道明细须累加，实际 {notes[0].get('channels')}"
    )
    assert notes[0].get("count") == 3, f"条数须为累加值，实际 {notes[0].get('count')}"


def test_single_write_keeps_exact_channels(tmp_path: Path) -> None:
    """破坏者视角：只有 long_term 时不得凭空多出别的通道。"""
    notes = _persist(tmp_path / "sess3.db", [["long_term"]])
    assert notes[0].get("channels") == ["long_term"], f"实际 {notes[0].get('channels')}"
    assert notes[0].get("count") == 1
