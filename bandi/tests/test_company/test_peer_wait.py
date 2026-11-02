import asyncio
import time
from pathlib import Path

import pytest

from bandi.company.communication import CommunicationManager, MeetingStatus
from bandi.company.phase import Phase
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item


@pytest.fixture
def comms(tmp_path: Path) -> CommunicationManager:
    return CommunicationManager(str(tmp_path / "ws"), "p", "s")


def _item(task_id: str = "wi-1", assignee: str = "alice") -> WorkItem:
    item = create_work_item(
        title=f"task {task_id}",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=assignee,
        phase=Phase.RUNNING,
    )
    item.id = task_id
    return item


class TestAskPeerAndWait:
    def test_returns_requires_peer_wait(self, comms: CommunicationManager) -> None:
        result = comms.ask_peer_and_wait("alice", ["bob"], "需要确认", "方案 A 可以吗")
        assert result["requires_peer_wait"] is True
        wait = result["peer_wait"]
        assert wait["kind"] == "peer_message"
        assert wait["waiting_on_agents"] == ["bob"]
        assert wait["on_timeout"] == "continue"
        assert wait["timeout_at"] > time.time()

    def test_sends_blocking_dm(self, comms: CommunicationManager) -> None:
        result = comms.ask_peer_and_wait(
            "alice", ["bob", "carol"], "需要确认", "请回复", timeout_seconds=60
        )
        assert len(comms._sent_messages) == 2
        assert result["peer_wait"]["waiting_on_agents"] == ["bob", "carol"]

    def test_validate_self_wait_rejected(self, comms: CommunicationManager) -> None:
        task = _item()
        assert comms._validate_blocking_recipients_ready(task, ["bob"]) is True
        assert comms._validate_blocking_recipients_ready(task, ["alice"]) is False


class TestResolvePeerWait:
    def test_resolve_on_reply(self, comms: CommunicationManager, tmp_path: Path) -> None:
        task = _item()
        result = comms.ask_peer_and_wait("alice", ["bob"], "q", "确认？", timeout_seconds=600)
        wait = result["peer_wait"]
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER
        assert comms.resolve_task_peer_wait(task) is False

        comms.reply_to_message(wait["message_id"], "bob", "同意", recipient_role="alice")
        assert comms.resolve_task_peer_wait(task) is True
        assert task.phase == Phase.READY
        assert task.metadata["peer_wait_released"] is True
        assert "同意" in task.metadata["latest_peer_reply"]

    def test_no_wait_returns_false(self, comms: CommunicationManager) -> None:
        assert comms.resolve_task_peer_wait(_item()) is False

    def test_contract_reply_sender_checked(self, comms: CommunicationManager) -> None:
        """契约（解除不误触发·破坏者）：只接受 waiting_on_agents 内的回复解除——
        第三方消息不得误触发（修复前任何回复即解除等待）。"""
        task = _item()
        result = comms.ask_peer_and_wait("alice", ["bob"], "q", "确认？", timeout_seconds=600)
        wait = result["peer_wait"]
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER

        # 第三方（carol）先回复——不得解除
        comms.reply_to_message(wait["message_id"], "carol", "我来说两句", recipient_role="alice")
        assert comms.resolve_task_peer_wait(task) is False, "第三方回复不得解除等待"
        assert task.phase == Phase.WAITING_FOR_PEER

        # 正确对象（bob）回复——解除
        comms.reply_to_message(wait["message_id"], "bob", "同意", recipient_role="alice")
        assert comms.resolve_task_peer_wait(task) is True
        assert task.phase == Phase.READY

    def test_timeout_continue_releases(self, comms: CommunicationManager) -> None:
        task = _item()
        wait = {
            "kind": "peer_message",
            "message_id": "m-nonexistent",
            "waiting_on_agents": ["bob"],
            "timeout_at": time.time() - 1,
            "on_timeout": "continue",
            "live_required": True,
        }
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER
        assert comms.resolve_task_peer_wait(task) is True
        assert task.phase == Phase.READY
        assert task.metadata.get("peer_timeout_action") == "continue"

    def test_timeout_manager_escalates(self, comms: CommunicationManager) -> None:
        task = _item()
        wait = {
            "kind": "peer_message",
            "message_id": "m-nonexistent",
            "waiting_on_agents": ["bob"],
            "timeout_at": time.time() - 1,
            "on_timeout": "manager",
            "live_required": True,
            "manager_role": "manager",
        }
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER
        assert comms.resolve_task_peer_wait(task) is True
        assert task.metadata["peer_timeout_escalation"]["mode"] == "manager"
        assert any("决策请求" in r.subject for r in comms._sent_messages.values())

    def test_timeout_meeting_escalates(self, comms: CommunicationManager) -> None:
        task = _item()
        wait = {
            "kind": "peer_message",
            "message_id": "m-nonexistent",
            "waiting_on_agents": ["bob"],
            "timeout_at": time.time() - 1,
            "on_timeout": "meeting",
            "live_required": True,
            "manager_role": "manager",
        }
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER
        assert comms.resolve_task_peer_wait(task) is False
        assert task.metadata["peer_wait"]["kind"] == "meeting"
        assert task.metadata["peer_wait"]["escalated_from"] == "peer_message_timeout"
        assert task.phase == Phase.WAITING_FOR_PEER


class TestRefreshAndDeadlocks:
    def test_refresh_waiting_tasks(self, comms: CommunicationManager, tmp_path: Path) -> None:
        task = _item("wi-1")
        wait = comms.ask_peer_and_wait("alice", ["bob"], "q", "确认？", timeout_seconds=600)[
            "peer_wait"
        ]
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER
        other = _item("wi-2")
        other.phase = Phase.WAITING_FOR_PEER
        resolved = comms.refresh_waiting_tasks([task, other])
        assert resolved == []
        comms.reply_to_message(wait["message_id"], "bob", "ok", recipient_role="alice")
        resolved = comms.refresh_waiting_tasks([task, other])
        assert resolved == [task]
        assert task.phase == Phase.READY

    def test_detect_deadlock_cycle(self, comms: CommunicationManager) -> None:
        cycles = comms.detect_deadlocks({"alice": {"bob"}, "bob": {"alice"}})
        assert len(cycles) >= 1, f"应检测到环: {cycles}"

    def test_no_cycle_when_agents_idle(self, comms: CommunicationManager) -> None:
        # 单向等待不构成环：alice 等 bob，bob 未等待任何人
        assert comms.detect_deadlocks({"alice": {"bob"}}) == []

    def test_find_wait_cycles_pure(self) -> None:
        cycles = CommunicationManager._find_wait_cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}})
        assert len(cycles) >= 1
        assert CommunicationManager._find_wait_cycles({"a": {"b"}, "b": {"c"}}) == []


class TestMeetingWait:
    def test_open_meeting_wait_parks_task(self, comms: CommunicationManager) -> None:
        task = _item()
        result = comms.open_meeting_wait(
            task, "选技术栈", ["alice", "bob"], "决定用什么语言", decision_owner="alice"
        )
        assert result["requires_peer_wait"] is True
        assert task.phase == Phase.WAITING_FOR_PEER
        wait = task.metadata["peer_wait"]
        assert wait["kind"] == "meeting"
        assert task.metadata["active_meeting"]["topic"] == "选技术栈"

    def test_respond_and_resolve_meeting(self, comms: CommunicationManager) -> None:
        task = _item()
        room_id = comms.open_meeting_wait(
            task, "topic", ["alice", "bob"], "agenda", decision_owner="alice"
        )["meeting_room_id"]
        assert comms.resolve_task_meeting_wait(task) is False
        comms.respond_to_meeting_v2(room_id, "alice", "用 Python", finalize=True)
        assert comms.resolve_task_meeting_wait(task) is True
        assert task.phase == Phase.READY
        assert task.metadata["meeting_outcome"] == "用 Python"
        assert task.metadata["meeting_decision_method"] == "manual_owner_finalize"

    def test_finalize_requires_owner(self, comms: CommunicationManager) -> None:
        task = _item()
        room_id = comms.open_meeting_wait(
            task, "topic", ["alice", "bob"], "agenda", decision_owner="alice"
        )["meeting_room_id"]
        with pytest.raises(ValueError, match="decision owner"):
            comms.respond_to_meeting_v2(room_id, "bob", "我不同意", finalize=True)

    def test_run_meeting_autonomous(self, comms: CommunicationManager) -> None:
        task = _item()
        room_id = comms.open_meeting_wait(task, "t", ["alice", "bob"], "a")["meeting_room_id"]
        room = comms.run_meeting(room_id, default_content="同意")
        assert room is not None
        assert room.status == MeetingStatus.DECIDED
        assert len(room.messages) == 2

    def test_contract_run_meeting_partial_response_no_fake_consensus(
        self, comms: CommunicationManager
    ) -> None:
        """契约（共识不虚标·破坏者）：参与者未全员响应时 run_meeting 兜底必须
        consensus=False——修复前一律标 autonomous_consensus/已达成共识。"""
        task = _item()
        room_id = comms.open_meeting_wait(task, "t", ["alice", "bob"], "a")["meeting_room_id"]

        def _responder(role: str, _room: str) -> str:
            if role == "bob":
                raise RuntimeError("bob unavailable")
            return "同意"

        room = comms.run_meeting(room_id, agent_respond=_responder)
        assert room is not None
        assert room.status == MeetingStatus.DECIDED
        assert room.consensus is False, "未全员响应不得虚标共识"
        assert room.decision_method == "timeout_fallback"

    def test_auto_resolve_stale_meeting(self, comms: CommunicationManager) -> None:
        task = _item()
        room_id = comms.open_meeting_wait(task, "t", ["alice", "bob"], "a", timeout_seconds=1)[
            "meeting_room_id"
        ]
        comms._rooms[room_id].deadline_at = time.time() - 1
        resolved = comms.auto_resolve_stale_meetings([task])
        assert resolved == [task.id]
        assert task.phase == Phase.READY


class TestAnnotation:
    def test_add_task_comment(self, comms: CommunicationManager) -> None:
        task = _item()
        assert comms.add_task_comment(task, "alice", "注意数据脱敏") is True
        assert task.comments[0]["author"] == "alice"
        assert task.comments[0]["content"] == "注意数据脱敏"

    def test_empty_comment_rejected(self, comms: CommunicationManager) -> None:
        task = _item()
        assert comms.add_task_comment(task, "alice", "") is False
        assert task.comments == []

    def test_build_agent_context_annotations(self, comms: CommunicationManager) -> None:
        task = _item()
        ctx = comms.build_agent_context(task)
        assert ctx["annotations"] == []
        for i in range(12):
            comms.add_task_comment(task, "alice", f"c{i}")
        ctx = comms.build_agent_context(task)
        assert len(ctx["annotations"]) == 10
        assert ctx["annotations"][-1]["content"] == "c11"

    def test_comments_persisted_to_store(self, comms: CommunicationManager, tmp_path: Path) -> None:
        from bandi.company.store import WorkItemStore

        store = WorkItemStore(str(tmp_path / "ws2.db"))
        task = _item()
        comms.add_task_comment(task, "alice", "persist me")
        store.save(task)
        reloaded = store.get(task.id)
        assert reloaded is not None
        assert reloaded.comments[0]["content"] == "persist me"
        store._conn.close()


class TestHeartbeatWiring:
    def test_resolve_stale_waits_wired(self, tmp_path: Path) -> None:
        """E 接线：heartbeat 注入 communication 后自动解析 peer wait（回复到达解除）。"""
        from bandi.company.heartbeat import HeartbeatScheduler
        from bandi.company.org_engine import OrgEngine
        from bandi.company.store import WorkItemStore

        store = WorkItemStore(str(tmp_path / "hb.db"))
        comms = CommunicationManager(str(tmp_path / "ws"), "p", "s")

        class FakeDispatcher:
            async def signal_dispatcher_wake(self) -> None: ...

        scheduler = HeartbeatScheduler(
            store=store,
            org_engine=OrgEngine(),
            dispatcher=FakeDispatcher(),
            communication=comms,
        )
        task = _item("hb-1")
        wait = comms.ask_peer_and_wait("alice", ["bob"], "q", "确认？", timeout_seconds=600)[
            "peer_wait"
        ]
        task.metadata["peer_wait"] = wait
        task.phase = Phase.WAITING_FOR_PEER
        store.save(task)

        asyncio.run(scheduler._resolve_stale_waits())
        assert store.get("hb-1").phase == Phase.WAITING_FOR_PEER

        comms.reply_to_message(wait["message_id"], "bob", "ok", recipient_role="alice")
        asyncio.run(scheduler._resolve_stale_waits())
        assert store.get("hb-1").phase == Phase.READY, "回复后 heartbeat 必须解除 peer wait"
        store._conn.close()
