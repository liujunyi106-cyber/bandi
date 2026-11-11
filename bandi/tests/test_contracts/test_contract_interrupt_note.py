# 功能：契约——中断续跑注记的持久化标记（供前端隐藏、LLM 仍可见）
from __future__ import annotations

import json
from pathlib import Path

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


def test_interrupt_resume_note_persists_internal_marker(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions.db"))
    try:
        session_id = store.create_session(title="interrupt-note")
        store.append_message(
            session_id,
            Message(
                role=MessageRole.SYSTEM,
                content="【中断】上一次执行被用户终止。该轮目标尚未完成；",
                meta={"note_kind": "interrupt_resume", "internal_note": True},
            ),
        )
        entries = [entry for entry in store.get_branch(session_id) if entry.get("role") == "system"]
        assert len(entries) == 1
        metadata = entries[0].get("metadata")
        parsed = json.loads(metadata) if isinstance(metadata, str) else metadata
        assert parsed["note_kind"] == "interrupt_resume"
        assert parsed["internal_note"] is True
    finally:
        store.close()
