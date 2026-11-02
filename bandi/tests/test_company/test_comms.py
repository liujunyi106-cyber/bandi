from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.comms import (
    CommsMessage,
    append_transcript,
    create_meeting,
    list_inbox,
    mark_read,
    read_team_memory,
    send_message,
    write_team_memory,
)


class TestComms:
    def test_send_and_list(self, tmp_workspace):
        msg_id = send_message(
            workspace=tmp_workspace,
            project_id="p",
            session_id="s",
            sender_role="manager",
            recipient_role="worker",
            subject="hello",
            body="work on task X",
        )
        assert msg_id
        inbox = list_inbox(tmp_workspace, "p", "s", "worker")
        assert len(inbox) == 1
        assert inbox[0].subject == "hello"
        assert inbox[0].body == "work on task X"

    def test_mark_read(self, tmp_workspace):
        msg_id = send_message(tmp_workspace, "p", "s", "manager", "worker", "test", "body")
        assert mark_read(tmp_workspace, "p", "s", "worker", msg_id)
        inbox = list_inbox(tmp_workspace, "p", "s", "worker")
        assert len(inbox) == 0
        inbox_seen = list_inbox(tmp_workspace, "p", "s", "worker", include_seen=True)
        assert len(inbox_seen) == 1

    def test_sender_outbox(self, tmp_workspace):
        send_message(tmp_workspace, "p", "s", "manager", "worker", "test", "body")
        inbox = list_inbox(tmp_workspace, "p", "s", "manager", include_seen=True)
        assert len(inbox) == 0

    def test_team_memory_read_write(self, tmp_workspace):
        write_team_memory(tmp_workspace, "p", "s", "# Updated Memory")
        content = read_team_memory(tmp_workspace, "p", "s")
        assert "Updated Memory" in content

    def test_team_memory_default(self, tmp_workspace):
        content = read_team_memory(tmp_workspace, "p2", "s2")
        assert "Team Memory" in content

    def test_create_meeting(self, tmp_workspace):
        meeting_dir = create_meeting(
            tmp_workspace, "p", "s", "m1", "Sprint Planning", ["manager", "worker"]
        )
        assert meeting_dir.exists()
        manifest = meeting_dir / "manifest.yaml"
        assert manifest.exists()
        transcript = meeting_dir / "transcript.md"
        assert transcript.exists()

    def test_append_transcript(self, tmp_workspace, tmp_path):
        create_meeting(tmp_workspace, "p", "s", "m1", "Meeting", ["manager", "worker"])
        append_transcript(tmp_workspace, "p", "s", "m1", "manager", "Let's start")
        root = tmp_path / ".bandi-comms" / "p" / "s" / "meetings" / "m1" / "transcript.md"
        content = root.read_text(encoding="utf-8")
        assert "Let's start" in content

    @pytest.mark.parametrize(
        "traversal",
        ["../evil", "..", "/etc/evil", "a/b", "a\\b", "C:evil"],
    )
    def test_reject_path_traversal_role(self, tmp_workspace, traversal):
        with pytest.raises(ValueError):
            send_message(
                workspace=tmp_workspace,
                project_id="p",
                session_id="s",
                sender_role=traversal,
                recipient_role="worker",
                subject="t",
                body="b",
            )

    def test_reject_path_traversal_project_id(self, tmp_workspace):
        with pytest.raises(ValueError):
            send_message(tmp_workspace, "../p", "s", "manager", "worker", "t", "b")

    def test_reject_path_traversal_session_id(self, tmp_workspace):
        with pytest.raises(ValueError):
            send_message(tmp_workspace, "p", "../../s", "manager", "worker", "t", "b")

    def test_reject_path_traversal_meeting_id(self, tmp_workspace):
        with pytest.raises(ValueError):
            create_meeting(tmp_workspace, "p", "s", "../evil", "Meeting", ["manager"])

    def test_reject_path_traversal_inbox_lookup(self, tmp_workspace):
        with pytest.raises(ValueError):
            list_inbox(tmp_workspace, "p", "s", "../../evil")

    def test_is_read_false_for_new_message(self, tmp_workspace):
        send_message(tmp_workspace, "p", "s", "manager", "worker", "t", "b")
        inbox = list_inbox(tmp_workspace, "p", "s", "worker")
        assert inbox[0].is_read is False

    def test_is_read_true_after_mark_read(self, tmp_workspace):
        msg_id = send_message(tmp_workspace, "p", "s", "manager", "worker", "t", "b")
        assert mark_read(tmp_workspace, "p", "s", "worker", msg_id)
        seen = list_inbox(tmp_workspace, "p", "s", "worker", include_seen=True)
        assert len(seen) == 1
        assert seen[0].is_read is True

    def test_from_file_invalid_created_at_falls_back(self, tmp_workspace):
        send_message(tmp_workspace, "p", "s", "manager", "worker", "t", "b")
        new_dir = Path(tmp_workspace) / ".bandi-comms" / "p" / "s" / "inbox" / "worker" / "new"
        msg_file = next(new_dir.glob("*.md"))
        content = msg_file.read_text(encoding="utf-8").replace("created_at: ", "created_at: nope")
        msg_file.write_text(content, encoding="utf-8")
        msgs = list_inbox(tmp_workspace, "p", "s", "worker")
        assert len(msgs) == 1
        assert isinstance(msgs[0], CommsMessage)
        assert msgs[0].created_at is not None

    def test_no_tmp_leftover_after_writes(self, tmp_workspace):
        write_team_memory(tmp_workspace, "p", "s", "# v1")
        write_team_memory(tmp_workspace, "p", "s", "# v2")
        create_meeting(tmp_workspace, "p", "s", "m1", "M", ["manager"])
        append_transcript(tmp_workspace, "p", "s", "m1", "manager", "hi")
        root = Path(tmp_workspace) / ".bandi-comms" / "p" / "s"
        assert list(root.rglob("*.tmp")) == []


@pytest.fixture
def tmp_workspace(tmp_path):
    return str(tmp_path)
