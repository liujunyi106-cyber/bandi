# 功能：Day122t collab CLI 测试——send/read/reply/meeting/colleagues + env 契约 + JSON 输出

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bandi.cli.app import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BANDI_COMMS_FROM", "alice")
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("BANDI_COMMS_PROJECT", "p")
    monkeypatch.setenv("BANDI_COMMS_SESSION", "s")
    return tmp_path


def _invoke(args: list[str]) -> dict:
    result = runner.invoke(app, ["collab", *args])
    assert result.exit_code == 0, (
        f"exit={result.exit_code} out={result.output!r} err={result.exception}"
    )
    return json.loads(result.output.strip().splitlines()[-1])


class TestSend:
    def test_send_dm(self, env: Path) -> None:
        data = _invoke(["send", "bob", "确认", "方案 A 可以吗"])
        assert data["delivery_mode"] == "async_mailbox"
        assert data["message_id"]

    def test_send_requires_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BANDI_COMMS_FROM", raising=False)
        result = runner.invoke(app, ["collab", "send", "bob", "s", "b"])
        assert result.exit_code == 1
        assert "BANDI_COMMS_FROM" in result.output

    def test_send_invalid_urgency(self, env: Path) -> None:
        result = runner.invoke(app, ["collab", "send", "bob", "s", "b", "--urgency", "urgenttt"])
        assert result.exit_code == 1
        assert "invalid urgency" in result.output

    def test_send_high_urgency(self, env: Path) -> None:
        data = _invoke(["send", "bob", "紧急", "快点", "--urgency", "high"])
        assert data["message_id"]


class TestRead:
    def test_read_empty_inbox(self, env: Path) -> None:
        data = _invoke(["read"])
        assert data["count"] == 0
        assert data["unread_count"] == 0

    def test_read_after_send(self, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _invoke(["send", "bob", "主题", "正文"])
        monkeypatch.setenv("BANDI_COMMS_FROM", "bob")
        data = _invoke(["read"])
        assert data["count"] == 1
        assert data["messages"][0]["from_agent"] == "alice"
        assert data["messages"][0]["subject"] == "主题"

    def test_read_ack_archives(self, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _invoke(["send", "bob", "主题", "正文"])
        monkeypatch.setenv("BANDI_COMMS_FROM", "bob")
        data = _invoke(["read", "--ack"])
        assert data["count"] == 1
        data2 = _invoke(["read"])
        assert data2["count"] == 0, "ack 后未读应清零"

    def test_read_limit(self, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for i in range(3):
            _invoke(["send", "bob", f"s{i}", f"b{i}"])
        monkeypatch.setenv("BANDI_COMMS_FROM", "bob")
        data = _invoke(["read", "--limit", "2"])
        assert data["count"] == 2


class TestReply:
    def test_reply_to_message(self, env: Path) -> None:
        sent = _invoke(["send", "bob", "问题", "确认？"])
        data = _invoke(["reply", sent["message_id"], "同意"])
        assert data["reply_to"] == sent["message_id"]

    def test_reply_unknown_message(self, env: Path) -> None:
        result = runner.invoke(app, ["collab", "reply", "nonexistent", "hi"])
        assert result.exit_code == 1
        assert "error" in result.output

    def test_reply_with_subject(self, env: Path) -> None:
        sent = _invoke(["send", "bob", "问题", "确认？"])
        data = _invoke(["reply", sent["message_id"], "ok", "--subject", "re: 问题"])
        assert data["message_id"]


class TestMeeting:
    def test_meeting_start(self, env: Path) -> None:
        data = _invoke(["meeting", "start", "选技术栈", "alice,bob", "决定语言"])
        assert data["requires_peer_wait"] is True
        assert data["meeting_room_id"]

    def test_meeting_start_empty_participants(self, env: Path) -> None:
        result = runner.invoke(app, ["collab", "meeting", "start", "t", "", "a"])
        assert result.exit_code == 1
        assert "participants" in result.output

    def test_meeting_respond_and_finalize(self, env: Path) -> None:
        room_id = _invoke(
            ["meeting", "start", "选技术栈", "alice,bob", "决定语言", "--decision-owner", "alice"]
        )["meeting_room_id"]
        data = _invoke(["meeting", "respond", room_id, "用 Python", "--finalize"])
        assert data["status"] == "decided"

    def test_meeting_respond_non_owner_finalize_denied(self, env: Path) -> None:
        room_id = _invoke(
            ["meeting", "start", "选技术栈", "alice,bob", "决定语言", "--decision-owner", "alice"]
        )["meeting_room_id"]
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("BANDI_COMMS_FROM", "bob")
        result = runner.invoke(
            app, ["collab", "meeting", "respond", room_id, "我不同意", "--finalize"]
        )
        monkeypatch.undo()
        assert result.exit_code == 1
        assert "decision owner" in result.output

    def test_meeting_read(self, env: Path) -> None:
        room_id = _invoke(["meeting", "start", "选技术栈", "alice,bob", "决定语言"])[
            "meeting_room_id"
        ]
        _invoke(["meeting", "respond", room_id, "同意"])
        data = _invoke(["meeting", "read", room_id])
        assert data["status"] == "in_progress"
        assert len(data["messages"]) == 1
        assert data["messages"][0]["sender"] == "alice"

    def test_meeting_read_unknown(self, env: Path) -> None:
        result = runner.invoke(app, ["collab", "meeting", "read", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestColleagues:
    def test_colleagues_lists_roles(self, env: Path) -> None:
        data = _invoke(["colleagues"])
        assert "self" in data
        assert data["self"] == "alice"
