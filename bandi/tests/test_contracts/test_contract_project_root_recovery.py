"""功能：契约 — project_root 会话回填+ TOOL name 落库/恢复

钉的行为：
- SessionStore.session_project_root：从会话 metadata 解析 project_root（后续轮
  工具层基准回填的单一来源）；无 metadata/未知会话返回 None；
- append_message → message_from_entry 往返保留 TOOL 消息的 name（工具归因与折叠
  落盘命名依赖——session_entries 无 name 列，随 metadata 承载）。
"""

from __future__ import annotations

from pathlib import Path

from bandi.core.message import Message, MessageRole, message_from_entry
from bandi.memory.session.store import SessionStore


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "test.db")


class TestSessionProjectRootRecovery:
    def test_returns_metadata_project_root(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        project = str(tmp_path / "proj")
        sid = store.create_session(mode="execute", project_root=project)
        assert store.session_project_root(sid) == project

    def test_none_when_missing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sid = store.create_session(mode="execute")
        assert store.session_project_root(sid) is None

    def test_none_for_unknown_session(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.session_project_root("no-such-sid") is None


class TestToolNameRoundTrip:
    def test_append_and_restore_preserves_name(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sid = store.create_session(mode="execute")
        store.append_message(
            sid,
            Message(role=MessageRole.TOOL, content="out", tool_call_id="t1", name="bash"),
        )
        entries = store.get_branch(sid)
        restored = [message_from_entry(e) for e in entries if e.get("entry_type") == "message"]
        tool_msgs = [m for m in restored if m.role == MessageRole.TOOL]
        assert tool_msgs, "TOOL 消息必须存在于读回"
        assert tool_msgs[0].name == "bash", "工具名必须往返保留"

    def test_message_without_name_restores_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sid = store.create_session(mode="execute")
        store.append_message(sid, Message(role=MessageRole.USER, content="hi"))
        entries = store.get_branch(sid)
        restored = [message_from_entry(e) for e in entries if e.get("entry_type") == "message"]
        assert restored and restored[0].name is None
