# 功能：会话删除必须连带清理产物/用量/归档，避免外键失败或残留原文
from __future__ import annotations

from pathlib import Path

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


def _seed(store: SessionStore, session_id: str) -> str:
    entry_id = store.append_message(session_id, Message(role=MessageRole.USER, content="secret"))
    store.register_artifact(
        session_id,
        tool_name="write",
        path="a.txt",
        artifact_type="file",
        turn_index=1,
    )
    store.truncate_branch_archived(session_id, entry_id)
    return entry_id


def _counts(store: SessionStore, session_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in (
        "session_artifacts",
        "session_context_usage",
        "session_entries_archive",
        "session_entries",
    ):
        row = store._sqlite.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()
        out[table] = int(row[0] if row else 0)
    return out


def test_delete_session_removes_children(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "s.db"))
    session_id = store.create_session()
    _seed(store, session_id)

    assert store.delete_session(session_id) is True
    assert store.get_session(session_id) is None
    assert _counts(store, session_id) == {
        "session_artifacts": 0,
        "session_context_usage": 0,
        "session_entries_archive": 0,
        "session_entries": 0,
    }
    store.close()


def test_delete_expired_removes_children(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "s.db"))
    session_id = store.create_session()
    _seed(store, session_id)

    store.update_session(session_id, updated_at="T00:00:00+00:00")
    assert store.delete_expired(ttl_days=0) == 1
    assert _counts(store, session_id)["session_entries_archive"] == 0
    store.close()
