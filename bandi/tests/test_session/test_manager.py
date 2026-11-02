from __future__ import annotations

import json
import os
import tempfile

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore
from bandi.session.manager import SessionManager


@pytest.fixture
def store() -> SessionStore:
    tmp = tempfile.mktemp(suffix=".db")
    s = SessionStore(tmp)
    yield s
    s.close()
    if os.path.exists(tmp):
        os.unlink(tmp)


@pytest.fixture
def manager(store: SessionStore) -> SessionManager:
    m = SessionManager(store)
    sid = store.create_session(title="test")
    m.session_id = sid
    return m


class TestSessionManager:
    def test_fork_creates_independent_session(
        self, manager: SessionManager, store: SessionStore
    ) -> None:
        sid = manager.session_id
        msg1 = Message(role=MessageRole.USER, content="第一轮")
        msg2 = Message(role=MessageRole.ASSISTANT, content="回复1")
        msg3 = Message(role=MessageRole.USER, content="第二轮")
        e1 = store.append_message(sid, msg1)
        store.append_message(sid, msg2)
        store.append_message(sid, msg3)

        new_sid = manager.fork(e1)
        assert new_sid != sid

        new_branch = store.get_branch(new_sid)
        assert len(new_branch) == 1
        assert new_branch[0]["content"] == "第一轮"

        original_branch = store.get_branch(sid)
        assert len(original_branch) == 3


class TestForkRevert:
    def test_stage_commit_truncates_to_point(
        self, manager: SessionManager, store: SessionStore
    ) -> None:
        sid = manager.session_id
        e1 = store.append_message(sid, Message(role=MessageRole.USER, content="u1"))
        store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="a1"))
        store.append_message(sid, Message(role=MessageRole.USER, content="u2"))

        manager.stage(e1)
        removed = manager.commit()
        assert removed == 2
        assert [e["content"] for e in store.get_branch(sid)] == ["u1"]

    def test_clear_then_re_stage(self, manager: SessionManager, store: SessionStore) -> None:
        sid = manager.session_id
        e1 = store.append_message(sid, Message(role=MessageRole.USER, content="u1"))
        e2 = store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="a1"))
        store.append_message(sid, Message(role=MessageRole.USER, content="u2"))

        manager.stage(e1)
        manager.clear()
        assert len(store.get_branch(sid)) == 3

        manager.stage(e2)
        manager.commit()
        assert [e["content"] for e in store.get_branch(sid)] == ["u1", "a1"]

    def test_commit_without_stage_raises(
        self, manager: SessionManager, store: SessionStore
    ) -> None:
        store.append_message(manager.session_id, Message(role=MessageRole.USER, content="u1"))
        with pytest.raises(ValueError, match="no staged revert point"):
            manager.commit()

    def test_fork_records_provenance(self, manager: SessionManager, store: SessionStore) -> None:
        sid = manager.session_id
        e1 = store.append_message(sid, Message(role=MessageRole.USER, content="u1"))
        store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="a1"))

        new_sid = manager.fork(e1)
        raw = store.get_session(new_sid)["metadata"]
        metadata = json.loads(raw)
        assert metadata["parent_session_id"] == sid
        assert metadata["forked_from_entry"] == e1
