from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from bandi.company import reactivation_sweeper as sweeper_mod
from bandi.company.comms import CommsMessage
from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.reactivation_sweeper import (
    CommsReactivationSweeper,
    make_unread_mail_reactivate_fn,
)
from bandi.company.recruiter import Recruiter
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item


def _comms_message(
    msg_id: str,
    *,
    sender: str = "peer",
    blocking: bool = True,
) -> CommsMessage:
    return CommsMessage(
        id=msg_id,
        sender_role=sender,
        recipient_role="worker",
        subject="ping",
        body="hello",
        blocking=blocking,
        metadata={},
        created_at=datetime.now(UTC),
    )


class FakeCommunication:
    def __init__(self) -> None:
        self._inbox: dict[str, list[CommsMessage]] = {}

    def seed(self, role_id: str, messages: list[CommsMessage]) -> None:
        self._inbox[role_id] = messages

    def read_inbox(self, role_id: str, include_seen: bool = False) -> list[CommsMessage]:
        return list(self._inbox.get(role_id, []))


async def _true_fn(_item: WorkItem) -> bool:
    return True


async def _false_fn(_item: WorkItem) -> bool:
    return False


async def _raise_fn(_item: WorkItem) -> bool:
    raise RuntimeError("boom")


def _pending_item(
    item_id: str,
    *,
    role: str = "worker",
    phase: Phase = Phase.APPROVED,
) -> WorkItem:
    item = create_work_item(
        title=f"task {item_id}",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=role,
        phase=phase,
    )
    item.id = item_id
    return item


def _make_sweeper(
    store: WorkItemStore,
    *,
    project_id: str = "p1",
    reactivate_fn: Any = _true_fn,
    interval_sec: float = 10.0,
) -> CommsReactivationSweeper:
    return CommsReactivationSweeper(
        store=store,
        project_id_getter=lambda: project_id,
        reactivate_fn=reactivate_fn,
        interval_sec=interval_sec,
    )


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "reactivation.db"))


class TestSweeperConstruction:
    def test_default_params(self, store) -> None:
        s = _make_sweeper(store)
        assert s.interval_sec == 10.0
        assert s.reactivate_fn is _true_fn
        assert not s._running
        assert s._task is None

    def test_interval_sec_clamped_to_one(self, store) -> None:
        s = _make_sweeper(store, interval_sec=0.3)
        assert s.interval_sec == 1.0

    def test_last_reactivation_report_empty_initially(self, store) -> None:
        s = _make_sweeper(store)
        assert s.last_reactivation_report == []


class TestSweeperLifecycle:
    def test_start(self, store) -> None:
        s = _make_sweeper(store)
        asyncio.run(s.start())
        assert s._running
        assert s._task is not None

    def test_start_idempotent(self, store) -> None:
        s = _make_sweeper(store)

        async def inner() -> None:
            await s.start()
            first = s._task
            await s.start()
            assert s._task is first

        asyncio.run(inner())

    def test_stop(self, store) -> None:
        s = _make_sweeper(store)
        asyncio.run(s.start())
        asyncio.run(s.stop())
        assert not s._running
        assert s._task is None

    def test_stop_without_start_is_safe(self, store) -> None:
        s = _make_sweeper(store)
        asyncio.run(s.stop())
        assert not s._running

    def test_start_without_store_skips(self) -> None:
        s = CommsReactivationSweeper(
            store=None,
            project_id_getter=lambda: "p1",
            reactivate_fn=_true_fn,
        )
        asyncio.run(s.start())
        assert not s._running
        assert s._task is None


class TestTick:
    def test_empty_project_skips_scan(self, store) -> None:
        calls: list[str] = []

        async def fn(item: WorkItem) -> bool:
            calls.append(item.id)
            return True

        store.save(_pending_item("wi-1"))
        s = _make_sweeper(store, project_id="", reactivate_fn=fn)
        asyncio.run(s._tick())
        assert calls == []
        assert s.last_reactivation_report == []

    def test_no_approved_items_no_calls(self, store) -> None:
        calls: list[str] = []

        async def fn(item: WorkItem) -> bool:
            calls.append(item.id)
            return True

        store.save(_pending_item("wi-1", phase=Phase.RUNNING))
        s = _make_sweeper(store, reactivate_fn=fn)
        asyncio.run(s._tick())
        assert calls == []

    def test_only_scans_approved_phase(self, store) -> None:
        calls: list[str] = []
        store.save(_pending_item("wi-approved", phase=Phase.APPROVED))
        store.save(_pending_item("wi-failed", phase=Phase.FAILED))
        store.save(_pending_item("wi-running", phase=Phase.RUNNING))

        async def fn(item: WorkItem) -> bool:
            calls.append(item.id)
            return True

        s = _make_sweeper(store, reactivate_fn=fn)
        asyncio.run(s._tick())
        assert calls == ["wi-approved"]

    def test_reactivated_item_added_to_report(self, store) -> None:
        store.save(_pending_item("wi-1"))
        s = _make_sweeper(store, reactivate_fn=_true_fn)
        asyncio.run(s._tick())
        assert len(s.last_reactivation_report) == 1
        assert s.last_reactivation_report[0]["id"] == "wi-1"
        assert s.last_reactivation_report[0]["role"] == "worker"

    def test_false_reactivate_not_reported(self, store) -> None:
        store.save(_pending_item("wi-1"))
        s = _make_sweeper(store, reactivate_fn=_false_fn)
        asyncio.run(s._tick())
        assert s.last_reactivation_report == []

    def test_reactivate_exception_is_swallowed(self, store) -> None:
        store.save(_pending_item("wi-1"))
        store.save(_pending_item("wi-2"))
        s = _make_sweeper(store, reactivate_fn=_raise_fn)
        asyncio.run(s._tick())
        assert s.last_reactivation_report == []

    def test_multiple_reactivated_collected(self, store) -> None:
        store.save(_pending_item("wi-1"))
        store.save(_pending_item("wi-2"))
        s = _make_sweeper(store, reactivate_fn=_true_fn)
        asyncio.run(s._tick())
        assert {r["id"] for r in s.last_reactivation_report} == {"wi-1", "wi-2"}

    def test_report_reads_unread_msg_ids_from_metadata(self, store) -> None:
        async def fn(item: WorkItem) -> bool:
            item.metadata["reactivation_unread_msg_ids"] = ["m1", "m2"]
            return True

        store.save(_pending_item("wi-1"))
        s = _make_sweeper(store, reactivate_fn=fn)
        asyncio.run(s._tick())
        assert s.last_reactivation_report[0]["unread_msg_ids"] == ["m1", "m2"]


class TestMakeUnreadMailReactivateFn:
    def test_no_unread_mail_returns_false(self, store) -> None:
        comms = FakeCommunication()
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        assert asyncio.run(fn(item)) is False
        assert "reactivation_pending" not in item.metadata

    def test_blocking_mail_returns_true_and_marks(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        assert asyncio.run(fn(item)) is True
        assert item.metadata["reactivation_pending"] is True
        assert item.metadata["reactivation_unread_msg_ids"] == ["m1"]

    def test_non_blocking_mail_ignored(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1", blocking=False)])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        assert asyncio.run(fn(item)) is False

    def test_no_assignee_role_returns_false(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1", role=None)
        assert asyncio.run(fn(item)) is False

    def test_markers_persisted_to_store(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        store.save(item)
        asyncio.run(fn(item))
        reloaded = store.get("wi-1")
        assert reloaded is not None
        assert reloaded.metadata["reactivation_pending"] is True

    def test_only_blocking_mail_ids_recorded(self, store) -> None:
        comms = FakeCommunication()
        comms.seed(
            "worker", [_comms_message("m1", blocking=True), _comms_message("m2", blocking=False)]
        )
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        asyncio.run(fn(item))
        assert item.metadata["reactivation_unread_msg_ids"] == ["m1"]

    def test_repeat_call_skipped_by_fingerprint(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert item.phase == Phase.READY
        assert asyncio.run(fn(item)) is False


class FakeHeartbeat:
    def __init__(self) -> None:
        self.woken: list[str] = []

    def wakeup(self, agent_id: str, reason: str = "") -> None:
        self.woken.append(agent_id)


class FakeDispatcherWake:
    def __init__(self) -> None:
        self.calls = 0

    def signal_dispatcher_wake(self) -> None:
        self.calls += 1


def _build_dispatcher(store: WorkItemStore, workspace: str) -> CompanyDispatcher:
    org = OrgEngine()
    org.register_role(
        role_id="manager",
        name="Manager",
        responsibility="mgr",
        reports_to=None,
        can_spawn=["worker"],
    )
    org.register_role(
        role_id="worker", name="Worker", responsibility="wrk", reports_to="manager", can_spawn=[]
    )
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    comms = CommunicationManager(workspace=workspace, project_id="p1", session_id="s1")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        communication=comms,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=True),
    )


class TestFullReactivate:
    def test_reactivate_transitions_to_ready(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert item.phase == Phase.READY
        reloaded = store.get("wi-1")
        assert reloaded is not None
        assert reloaded.phase == Phase.READY

    def test_reactivate_writes_depth_fingerprint_history(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1", sender="peer")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert item.metadata["comms_reactivation_depth"] == 1
        assert item.metadata["comms_last_reactivation_fingerprint"] == ["m1"]
        assert item.metadata["reactivation_pending"] is True
        assert item.metadata["reactivation_unread_msg_ids"] == ["m1"]
        history = item.metadata["comms_cross_role_history"]
        assert history[0]["from_role"] == "peer"
        assert history[0]["target_work_item_id"] == "wi-1"
        assert history[0]["depth"] == 1
        assert item.runtime_metadata["last_transition_reason"] == "reactivated_by_inbound_comms"

    def test_skips_same_fingerprint(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert asyncio.run(fn(item)) is False

    def test_skips_at_max_depth(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1", sender="a"), _comms_message("m2", sender="b")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        item.metadata["comms_reactivation_depth"] = sweeper_mod.COMMS_REACTIVATION_MAX_DEPTH
        store.save(item)
        assert asyncio.run(fn(item)) is False

    def test_skips_cross_role_ping_pong(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1", sender="peer")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        item.metadata["comms_cross_role_history"] = [
            {
                "from_role": "peer",
                "subject_hash": sweeper_mod._subject_hash("ping"),
                "target_work_item_id": "wi-1",
                "msg_id": "old",
                "depth": 1,
            }
        ]
        store.save(item)
        assert asyncio.run(fn(item)) is False

    def test_new_sender_allowed_after_ping_pong_blocked(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m2", sender="peer2")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        item.metadata["comms_cross_role_history"] = [
            {
                "from_role": "peer1",
                "subject_hash": "x",
                "target_work_item_id": "wi-1",
                "msg_id": "old",
                "depth": 1,
            }
        ]
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert item.phase == Phase.READY

    def test_after_reactivate_dispatcher_picks_up(self, store, tmp_path) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1", role="worker")
        store.save(item)
        assert asyncio.run(fn(item)) is True
        dispatcher = _build_dispatcher(store, str(tmp_path))
        report = asyncio.run(dispatcher.tick())
        reloaded = store.get("wi-1")
        assert reloaded is not None
        assert reloaded.phase == Phase.READY
        assert "worker" in report.claimed


class TestSweeperWiring:
    def test_tick_wakes_heartbeat_on_reactivation(self, store) -> None:
        fake_hb = FakeHeartbeat()

        async def fn(_item: WorkItem) -> bool:
            return True

        store.save(_pending_item("wi-1", role="worker"))
        s = CommsReactivationSweeper(
            store=store,
            project_id_getter=lambda: "p1",
            reactivate_fn=fn,
            heartbeat=fake_hb,
        )
        asyncio.run(s._tick())
        assert "worker" in fake_hb.woken

    def test_tick_signals_dispatcher_wake_on_reactivation(self, store) -> None:
        fake_disp = FakeDispatcherWake()

        async def fn(_item: WorkItem) -> bool:
            return True

        store.save(_pending_item("wi-1", role="worker"))
        s = CommsReactivationSweeper(
            store=store,
            project_id_getter=lambda: "p1",
            reactivate_fn=fn,
            dispatcher=fake_disp,
        )
        asyncio.run(s._tick())
        assert fake_disp.calls == 1

    def test_tick_no_signal_without_reactivation(self, store) -> None:
        fake_hb = FakeHeartbeat()
        fake_disp = FakeDispatcherWake()

        async def fn(_item: WorkItem) -> bool:
            return False

        store.save(_pending_item("wi-1", role="worker"))
        s = CommsReactivationSweeper(
            store=store,
            project_id_getter=lambda: "p1",
            reactivate_fn=fn,
            heartbeat=fake_hb,
            dispatcher=fake_disp,
        )
        asyncio.run(s._tick())
        assert fake_hb.woken == []
        assert fake_disp.calls == 0


class TestReactivateDirtyIntInput:
    def test_non_numeric_depth_does_not_crash(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        item.metadata["comms_reactivation_depth"] = "abc"
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert item.metadata["comms_reactivation_depth"] == 1

    def test_none_depth_treated_as_zero(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        item.metadata["comms_reactivation_depth"] = None
        store.save(item)
        assert asyncio.run(fn(item)) is True
        assert item.metadata["comms_reactivation_depth"] == 1

    def test_dirty_depth_in_blocked_check_does_not_crash(self, store) -> None:
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        item.metadata["comms_reactivation_depth"] = "not-a-number"
        item.metadata["comms_last_reactivation_fingerprint"] = ["other"]
        store.save(item)
        assert asyncio.run(fn(item)) is True


class TestReactivateRollbackOnTransitionFailure:
    def test_transition_failure_rolls_back_metadata(self, store, monkeypatch) -> None:
        from bandi.company import reactivation_sweeper as sweeper_module

        def boom(*args, **kwargs):
            raise RuntimeError("transition failed")

        monkeypatch.setattr(sweeper_module, "transition_work_item", boom)
        comms = FakeCommunication()
        comms.seed("worker", [_comms_message("m1")])
        fn = make_unread_mail_reactivate_fn(comms, store)
        item = _pending_item("wi-1")
        store.save(item)
        original_metadata = dict(item.metadata)
        assert asyncio.run(fn(item)) is False
        assert item.metadata == original_metadata
        assert item.phase == Phase.APPROVED
