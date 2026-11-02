from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.comms import CommsMessage, list_inbox
from bandi.company.communication import (
    CommunicationManager,
    MessageStatus,
)


class TestPhaseContract:
    """通信直赋 phase 必须走状态机合法出口（validate_transition 契约）。

    AWAITING_HUMAN（会议转人工评审）在 ALLOWED_TRANSITIONS 无出口——
    一旦接线校验即炸。此处先补业务流契约测试。
    """

    def test_meeting_human_escalation_transition_is_valid(self, tmp_workspace) -> None:
        from bandi.company.communication import MeetingStatus
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        mgr = CommunicationManager(tmp_workspace, "p", "s")
        task = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
        task.phase = Phase.RUNNING
        mgr.open_meeting_wait(task, "topic", ["alice"], "agenda")
        assert task.phase == Phase.WAITING_FOR_PEER, "RUNNING→WAITING_FOR_PEER 挂起会议"
        room_id = task.metadata["peer_wait"]["meeting_room_id"]
        room = mgr._rooms[room_id]
        room.status = MeetingStatus.DECIDED
        room.decision_method = "human_escalation"
        assert mgr.resolve_task_meeting_wait(task) is True
        assert task.phase == Phase.AWAITING_HUMAN, (
            "会议 human 决策升级必须合法转换到 AWAITING_HUMAN（WAITING_FOR_PEER 出口）"
        )

    def test_peer_wait_reply_resolve_stays_valid(self, tmp_workspace) -> None:
        from bandi.company.comms import send_message
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        mgr = CommunicationManager(tmp_workspace, "p", "s")
        task = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
        task.phase = Phase.RUNNING
        wait = {
            "kind": "message",
            "message_id": "wait-msg",
            "timeout_at": 9999999999.0,
            "on_timeout": "continue",
            "waiting_on_agents": ["alice"],
        }
        task.metadata["peer_wait"] = wait
        send_message(
            workspace=tmp_workspace,
            project_id="p",
            session_id="s",
            sender_role="alice",
            recipient_role="manager",
            subject="回复",
            body="已确认",
            extra_metadata={"reply_to_msg_id": "wait-msg"},
        )
        assert mgr.resolve_task_peer_wait(task) is True
        assert task.phase == Phase.READY


class TestCommunicationManager:
    def test_send_dm(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        msg_id = mgr.send_dm("manager", "worker", "hello", "work on X")
        assert msg_id
        inbox = mgr.read_inbox("worker")
        assert len(inbox) == 1
        assert inbox[0].subject == "hello"

    def test_mark_as_read(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        msg_id = mgr.send_dm("manager", "worker", "test", "body")
        assert mgr.mark_as_read("worker", msg_id)
        inbox = mgr.read_inbox("worker")
        assert len(inbox) == 0
        assert mgr.get_message_status(msg_id) == MessageStatus.READ

    def test_broadcast(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        msg_ids = mgr.broadcast("ceo", ["worker1", "worker2"], "all hands", "meeting at 3")
        assert len(msg_ids) == 2
        assert len(mgr.read_inbox("worker1")) == 1
        assert len(mgr.read_inbox("worker2")) == 1

    def test_get_message_status(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        msg_id = mgr.send_dm("manager", "worker", "test", "body")
        assert mgr.get_message_status(msg_id) == MessageStatus.DELIVERED
        assert mgr.get_message_status("nonexistent") is None

    def test_create_and_get_room(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Sprint planning")
        room = mgr.get_room(room_id)
        assert room is not None
        assert room.agenda == "Sprint planning"
        assert "alice" in room.participants

    def test_post_to_room(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Discussion")
        assert mgr.post_to_room(room_id, "alice", "Hello") is True
        room = mgr.get_room(room_id)
        assert len(room.messages) == 1
        assert room.messages[0].content == "Hello"

    def test_post_to_nonexistent_room(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        assert mgr.post_to_room("invalid", "alice", "Hi") is False

    def test_send_dm_v2(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm_v2("alice", "bob", "Hey Bob")
        inbox = mgr.read_inbox_v2("bob")
        assert len(inbox) == 1
        assert inbox[0].content == "Hey Bob"

    def test_read_inbox_v2_clears_queue(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm_v2("alice", "bob", "msg1")
        assert len(mgr.read_inbox_v2("bob")) == 1
        assert len(mgr.read_inbox_v2("bob")) == 0

    @pytest.mark.asyncio
    async def test_reach_consensus(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Decide on stack")
        mgr.post_to_room(room_id, "alice", "Python")
        mgr.post_to_room(room_id, "bob", "Python sounds good")
        consensus, summary = await mgr.reach_consensus(room_id, "stack choice")
        assert consensus is True
        assert "Python" in summary

    @pytest.mark.asyncio
    async def test_reach_consensus_invalid_room(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        with pytest.raises(ValueError, match="not found"):
            await mgr.reach_consensus("invalid", "topic")

    @pytest.mark.asyncio
    async def test_reach_consensus_with_llm_verdict(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Decide on stack")
        mgr.post_to_room(room_id, "alice", "Python")
        mgr.post_to_room(room_id, "bob", "Python sounds good")

        async def fake_llm(prompt: str) -> str:
            return "CONSENSUS: everyone agrees on Python"

        consensus, verdict = await mgr.reach_consensus(room_id, "stack choice", llm=fake_llm)
        assert consensus is True
        assert "CONSENSUS" in verdict

    @pytest.mark.asyncio
    async def test_reach_consensus_llm_no_consensus(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Decide on stack")
        mgr.post_to_room(room_id, "alice", "Python")
        mgr.post_to_room(room_id, "bob", "Rust")

        def fake_llm(prompt: str) -> str:
            return "NO_CONSENSUS: conflicting choices"

        consensus, verdict = await mgr.reach_consensus(room_id, "stack choice", llm=fake_llm)
        assert consensus is False
        assert "NO_CONSENSUS" in verdict

    @pytest.mark.asyncio
    async def test_reach_consensus_llm_failure_closes(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Decide on stack")
        mgr.post_to_room(room_id, "alice", "Python")

        async def broken_llm(prompt: str) -> str:
            raise RuntimeError("llm down")

        consensus, verdict = await mgr.reach_consensus(room_id, "stack choice", llm=broken_llm)
        assert consensus is False, "LLM 异常时必须 fail-closed（未达成共识）"
        assert "NO_CONSENSUS" in verdict.upper()

    def test_hydrate(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm("alice", "bob", "hi", "msg body")
        result = mgr.hydrate()
        assert result == 1  # hydrate 恢复未读消息到内存队列并返回条数


class TestMailboxMetadataKeys:
    def test_mailbox_metadata_keys_length(self):
        mgr = CommunicationManager("w", "p", "s")
        # 21→23：+thread_id（chat/DM 线程绑定——原被白名单过滤丢失的修复）
        # +target_role（单聊定向元数据）
        assert len(mgr._MAILBOX_METADATA_KEYS) == 23
        assert "work_item_id" in mgr._MAILBOX_METADATA_KEYS
        assert "target_seat_id" in mgr._MAILBOX_METADATA_KEYS
        assert "action_hint" in mgr._MAILBOX_METADATA_KEYS
        assert "kind" in mgr._MAILBOX_METADATA_KEYS
        assert "route" in mgr._MAILBOX_METADATA_KEYS
        assert "thread_id" in mgr._MAILBOX_METADATA_KEYS
        assert "target_role" in mgr._MAILBOX_METADATA_KEYS

    def test_envelope_metadata_keys_length(self):
        mgr = CommunicationManager("w", "p", "s")
        assert len(mgr._ENVELOPE_METADATA_KEYS) == 8
        assert "message_class" in mgr._ENVELOPE_METADATA_KEYS
        assert "origin_projection_id" in mgr._ENVELOPE_METADATA_KEYS

    def test_send_dm_persists_extra_metadata(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm(
            "manager",
            "worker",
            "hello",
            "work on X",
            extra_metadata={"work_item_id": "wi-1", "target_seat_id": "seat-9"},
        )
        inbox = mgr.read_inbox("worker")
        assert len(inbox) == 1
        assert inbox[0].metadata.get("work_item_id") == "wi-1"
        assert inbox[0].metadata.get("target_seat_id") == "seat-9"

    def test_send_dm_broadcast_persists_extra_metadata(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.broadcast(
            "ceo",
            ["w1", "w2"],
            "all hands",
            "meeting",
            extra_metadata={"message_class": "broadcast", "notification_kind": "all_hands"},
        )
        for role in ("w1", "w2"):
            msgs = list_inbox(tmp_workspace, "p", "s", role)
            assert msgs[0].metadata.get("message_class") == "broadcast"
            assert msgs[0].metadata.get("notification_kind") == "all_hands"

    def test_send_dm_metadata_roundtrip_via_file(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        msg_id = mgr.send_dm(
            "manager",
            "worker",
            "hi",
            "body",
            extra_metadata={"parent_work_item_id": "pwi", "source_seat_id": "s1"},
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "worker")[0]
        assert raw.id == msg_id
        assert raw.metadata.get("parent_work_item_id") == "pwi"
        assert raw.metadata.get("source_seat_id") == "s1"
        assert raw.is_read is False

    def test_send_dm_filters_non_whitelisted_metadata(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm(
            "manager",
            "worker",
            "hi",
            "body",
            extra_metadata={"work_item_id": "wi-1", "evil_key": "x", "random_junk": "y"},
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "worker")[0]
        assert raw.metadata.get("work_item_id") == "wi-1"
        assert "evil_key" not in raw.metadata
        assert "random_junk" not in raw.metadata

    def test_send_dm_keeps_all_whitelist_keys(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        all_keys = (*mgr._MAILBOX_METADATA_KEYS, *mgr._ENVELOPE_METADATA_KEYS)
        extra = {k: f"v-{k}" for k in all_keys}
        mgr.send_dm("manager", "worker", "hi", "body", extra_metadata=extra)
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "worker")[0]
        for k in all_keys:
            assert raw.metadata.get(k) == f"v-{k}"


class TestReplyToMessage:
    def test_reply_to_message_returns_new_id(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        original_id = mgr.send_dm("manager", "worker", "q", "status?")
        reply_id = mgr.reply_to_message(original_id, "worker", "done")
        assert reply_id
        assert reply_id != original_id

    def test_reply_to_message_persists_relation(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        original_id = mgr.send_dm("manager", "worker", "q", "status?")
        mgr.reply_to_message(original_id, "worker", "done", subject="re")
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("reply_to_msg_id") == original_id
        assert raw.body == "done"

    def test_reply_to_message_with_metadata(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        original_id = mgr.send_dm("manager", "worker", "q", "status?")
        mgr.reply_to_message(
            original_id,
            "worker",
            "done",
            metadata={"work_item_id": "wi-9", "target_seat_id": "seat-2"},
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("reply_to_msg_id") == original_id
        assert raw.metadata.get("work_item_id") == "wi-9"
        assert raw.metadata.get("target_seat_id") == "seat-2"

    def test_reply_to_message_tracks_sent_record(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        original_id = mgr.send_dm("manager", "worker", "q", "status?")
        reply_id = mgr.reply_to_message(original_id, "worker", "done")
        status = mgr.get_message_status(reply_id)
        assert status == MessageStatus.DELIVERED

    def test_reply_to_nonexistent_original_raises(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        with pytest.raises(ValueError, match="Cannot resolve recipient"):
            mgr.reply_to_message("missing", "worker", "hello")


class TestHydrateRecovery:
    def test_hydrate_recovers_unread_messages(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm("alice", "bob", "hi", "msg body")
        mgr.send_dm("carol", "bob", "hello", "another")
        count = mgr.hydrate()
        assert count == 2
        queued = mgr.read_inbox_v2("bob")
        assert len(queued) == 2

    def test_hydrate_empty_inbox_returns_zero(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        assert mgr.hydrate() == 0

    def test_hydrate_is_idempotent(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm("alice", "bob", "hi", "msg body")
        first = mgr.hydrate()
        second = mgr.hydrate()
        assert first == 1
        assert second == 0  # 已恢复的消息不重复入队
        assert len(mgr.read_inbox_v2("bob")) == 1

    def test_hydrate_does_not_move_filesystem_messages(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.send_dm("alice", "bob", "hi", "msg body")
        mgr.hydrate()
        inbox = mgr.read_inbox("bob")
        assert len(inbox) == 1  # 文件系统 NEW 目录消息仍保留（read_inbox v1 不受影响）

    def test_hydrate_recovers_only_unread_not_seen(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        first_id = mgr.send_dm("alice", "bob", "hi", "msg body")
        mgr.send_dm("carol", "bob", "bye", "another")
        mgr.mark_as_read("bob", first_id)
        assert mgr.hydrate() == 1  # 只恢复未读的


class TestReplyToPersistence:
    def test_send_message_persists_reply_to(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        original_id = mgr.send_dm("manager", "worker", "q", "please respond")
        reply_id = mgr.send_dm(
            "worker",
            "manager",
            "re",
            "done",
            reply_to_msg_id=original_id,
        )
        assert reply_id != original_id
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("reply_to_msg_id") == original_id

    def test_reply_metadata_keeps_work_item(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        original_id = mgr.send_dm("manager", "worker", "q", "status?")
        mgr.send_dm(
            "worker",
            "manager",
            "re",
            "done",
            reply_to_msg_id=original_id,
            extra_metadata={"work_item_id": "wi-7"},
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("reply_to_msg_id") == original_id
        assert raw.metadata.get("work_item_id") == "wi-7"


class TestMeetingEnhancements:
    def test_respond_to_meeting_appends_message(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Sprint planning")
        assert mgr.respond_to_meeting(room_id, "alice", "I propose Python") is True
        room = mgr.get_room(room_id)
        assert len(room.messages) == 1
        assert room.messages[0].content == "I propose Python"
        assert room.messages[0].sender_role == "alice"

    def test_respond_to_nonexistent_room_returns_false(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        assert mgr.respond_to_meeting("invalid", "alice", "hi") is False

    def test_respond_to_meeting_finalize_marks_room(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Decide stack")
        mgr.respond_to_meeting(room_id, "alice", "Python", finalize=True)
        room = mgr.get_room(room_id)
        assert room.metadata.get("status") == "finalized"

    def test_respond_to_meeting_finalize_appends_message(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Decide stack")
        mgr.respond_to_meeting(room_id, "alice", "Python", finalize=True)
        room = mgr.get_room(room_id)
        assert len(room.messages) == 1
        assert room.messages[0].content == "Python"

    def test_meeting_transcript_written_to_disk(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        room_id = mgr.create_room(["alice", "bob"], "Sprint planning")
        mgr.respond_to_meeting(room_id, "alice", "I propose Python")
        transcript = (
            Path(tmp_workspace)
            / ".bandi-comms"
            / "p"
            / "s"
            / "meetings"
            / room_id
            / "transcript.md"
        )
        assert transcript.exists()
        text = transcript.read_text(encoding="utf-8")
        assert "alice" in text
        assert "I propose Python" in text


class TestPeerWait:
    def test_open_peer_wait_returns_async_mailbox(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        result = mgr.open_peer_wait(
            from_agent="engineer",
            to_agents=["manager"],
            subject="question",
            body="blocked?",
        )
        assert result["requires_peer_wait"] is False
        assert result["delivery_mode"] == "async_mailbox"
        assert "message" in result

    def test_open_peer_wait_delivers_to_recipient(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.open_peer_wait(
            from_agent="engineer",
            to_agents=["manager"],
            subject="question",
            body="blocked?",
        )
        inbox = mgr.read_inbox("manager")
        assert len(inbox) == 1
        assert inbox[0].subject == "question"
        assert inbox[0].blocking is False

    def test_open_peer_wait_metadata_keys(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.open_peer_wait(
            from_agent="engineer",
            to_agents=["manager"],
            subject="question",
            body="blocked?",
            timeout_seconds=120,
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("reply_requested") == "true"
        assert raw.metadata.get("async_mailbox") == "true"
        assert raw.metadata.get("timeout_seconds") == "120"
        assert raw.metadata.get("legacy_blocking_request") == "true"
        assert raw.blocking is False  # async mailbox 不阻塞
        assert raw.metadata.get("blocking") == "false"  # frontmatter 同步持久化

    def test_open_peer_wait_execution_task_ids(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.open_peer_wait(
            from_agent="engineer",
            to_agents=["manager"],
            subject="question",
            body="blocked?",
            execution_task_ids=["t1", "t2"],
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("execution_task_ids") == "t1,t2"

    def test_open_peer_wait_default_no_execution_task_ids(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        mgr.open_peer_wait(
            from_agent="engineer",
            to_agents=["manager"],
            subject="question",
            body="blocked?",
        )
        raw: CommsMessage = list_inbox(tmp_workspace, "p", "s", "manager")[0]
        assert raw.metadata.get("execution_task_ids") in (None, "")

    def test_open_peer_wait_empty_recipients(self, tmp_workspace):
        mgr = CommunicationManager(tmp_workspace, "p", "s")
        result = mgr.open_peer_wait(
            from_agent="engineer",
            to_agents=[],
            subject="q",
            body="b",
        )
        assert result["message"] is None


@pytest.fixture
def tmp_workspace(tmp_path):
    return str(tmp_path)
