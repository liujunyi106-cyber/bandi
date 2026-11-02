# 功能：SessionStore 单元测试 — CRUD / 消息追加 / 分支 / JSONL / Checkpoint

import json
import os
import tempfile
from pathlib import Path

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


@pytest.fixture
def store() -> SessionStore:
    tmp = tempfile.mktemp(suffix=".db")
    s = SessionStore(tmp)
    yield s
    s.close()
    if os.path.exists(tmp):
        os.unlink(tmp)


class TestSessionStore:
    def test_create_and_get_session(self, store: SessionStore) -> None:
        sid = store.create_session(title="测试会话")
        s = store.get_session(sid)
        assert s is not None
        assert s["title"] == "测试会话"
        assert s["status"] == "active"

    def test_get_nonexistent_session(self, store: SessionStore) -> None:
        assert store.get_session("nonexistent") is None

    def test_update_session(self, store: SessionStore) -> None:
        sid = store.create_session(title="原标题")
        assert store.update_session(sid, title="新标题")
        s = store.get_session(sid)
        assert s["title"] == "新标题"

    def test_list_sessions(self, store: SessionStore) -> None:
        store.create_session(title="A")
        store.create_session(title="B")
        sessions = store.list_sessions()
        assert len(sessions) == 2

    def test_delete_session(self, store: SessionStore) -> None:
        sid = store.create_session(title="待删除")
        assert store.delete_session(sid) is True
        assert store.get_session(sid) is None

    def test_append_and_get_messages(self, store: SessionStore) -> None:
        sid = store.create_session()
        msg = Message(role=MessageRole.USER, content="你好")
        store.append_message(sid, msg)
        msg2 = Message(role=MessageRole.ASSISTANT, content="你好！")
        store.append_message(sid, msg2)
        branch = store.get_branch(sid)
        assert len(branch) == 2
        assert branch[0]["content"] == "你好"
        assert branch[1]["role"] == "assistant"

    def test_export_jsonl(self, store: SessionStore) -> None:
        sid = store.create_session()
        msg = Message(role=MessageRole.USER, content="你好")
        store.append_message(sid, msg)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            count = store.export_jsonl(sid, path)
            assert count == 1
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["content"] == "你好"
        finally:
            os.unlink(path)

    def test_append_message_to_nonexistent_session(self, store: SessionStore) -> None:
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            msg = Message(role=MessageRole.USER, content="hi")
            store.append_message("nonexistent", msg)

    def test_terminated_metadata_written_via_on_terminated(self, store: SessionStore) -> None:
        import asyncio

        sid = store.create_session(title="终止会话")
        evt = type(
            "Evt",
            (),
            {"data": {"session_id": sid, "terminal_reason": "COMPLETED"}},
        )()
        asyncio.run(store.on_terminated(evt))
        session = store.get_session(sid)
        metadata = json.loads(session.get("metadata") or "{}")
        assert metadata.get("last_terminal_reason") == "COMPLETED"

    def test_rotate_archives_old_sessions(self, store: SessionStore, tmp_path: Path) -> None:
        for i in range(5):
            sid = store.create_session(title=f"session_{i}")
            msg = Message(role=MessageRole.USER, content=f"hello {i}")
            store.append_message(sid, msg)
        assert store.count_sessions() == 5
        result = store.rotate(str(tmp_path), max_sessions=3, archive_after_days=0)
        assert result["archived_count"] == 2
        assert store.count_sessions() == 3
        for sid in result["archived_ids"]:
            assert store.get_session(sid) is None
        archive_files = list(tmp_path.glob("*.jsonl.gz"))
        assert len(archive_files) == 2

    def test_rotate_archives_old_sessions_by_age(self, store: SessionStore, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta

        for i in range(3):
            sid = store.create_session(title=f"fresh_{i}")
            msg = Message(role=MessageRole.USER, content=f"fresh {i}")
            store.append_message(sid, msg)
        old_sid = store.create_session(title="old_one")
        store.update_session(
            old_sid, updated_at=(datetime.now(UTC) - timedelta(days=60)).isoformat()
        )
        assert store.count_sessions() == 4
        result = store.rotate(str(tmp_path), max_sessions=100, archive_after_days=30)
        assert result["archived_count"] == 1
        assert result["archived_ids"] == [old_sid]
        assert store.count_sessions() == 3

    def test_rotate_no_op_when_under_limit(self, store: SessionStore, tmp_path: Path) -> None:
        store.create_session(title="only_one")
        result = store.rotate(str(tmp_path), max_sessions=10, archive_after_days=30)
        assert result["archived_count"] == 0

    def test_count_branch(self, store: SessionStore) -> None:
        sid = store.create_session(title="计数")
        assert store.count_branch(sid) == 0
        store.append_message(sid, Message(role=MessageRole.USER, content="a"))
        store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="b"))
        assert store.count_branch(sid) == 2

    def test_rotate_archive_names_do_not_collide_on_id_prefix(
        self, store: SessionStore, tmp_path: Path
    ) -> None:
        """
        前缀相同的两个 session 归档文件互相覆盖（后者内容覆盖前者）。"""
        sid_a = store.create_session(session_id="aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title="A")
        sid_b = store.create_session(session_id="aaaa1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb", title="B")
        msg = Message(role=MessageRole.USER, content="hi")
        store.append_message(sid_a, msg)
        store.append_message(sid_b, msg)
        result = store.rotate(str(tmp_path), max_sessions=0, archive_after_days=0)
        assert result["archived_count"] == 2
        archive_files = list(tmp_path.glob("*.jsonl.gz"))
        assert len(archive_files) == 2, "前 8 位相同的两个 session 归档不得互相覆盖"
        assert any(sid_a in f.name for f in archive_files)
        assert any(sid_b in f.name for f in archive_files)
        assert store.count_branch("missing") == 0

    def test_truncate_removes_entries_after_revert_point(self, store: SessionStore) -> None:
        sid = store.create_session(title="截断")
        for i in range(4):
            store.append_message(sid, Message(role=MessageRole.USER, content=f"m{i}"))
        entries = store.get_branch(sid)
        kept = store.truncate_branch(sid, entries[1]["id"])
        assert kept == 2
        branch = store.get_branch(sid)
        assert [e["content"] for e in branch] == ["m0", "m1"]

    def test_truncate_keeps_revert_point_itself(self, store: SessionStore) -> None:
        sid = store.create_session(title="截断")
        store.append_message(sid, Message(role=MessageRole.USER, content="第一轮"))
        last = store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="回复"))
        kept = store.truncate_branch(sid, last)
        assert kept == 0
        branch = store.get_branch(sid)
        assert [e["content"] for e in branch] == ["第一轮", "回复"]

    def test_truncate_unknown_entry_raises_and_preserves_data(self, store: SessionStore) -> None:
        sid = store.create_session(title="截断")
        store.append_message(sid, Message(role=MessageRole.USER, content="m0"))
        with pytest.raises(ValueError, match="not found"):
            store.truncate_branch(sid, "nonexistent")
        assert store.count_branch(sid) == 1
