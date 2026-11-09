from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bandi.company.heartbeat import HeartbeatScheduler
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


class FakeDispatcher:
    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.runs: list[str] = []
        self.fail_claim = False
        self.raise_on_run = False
        self.run_result: Any = None

    def claim(self, role_id: str) -> bool:
        if self.fail_claim or role_id in self.claimed:
            return False
        self.claimed.append(role_id)
        return True

    def release(self, role_id: str) -> bool:
        self.claimed = [r for r in self.claimed if r != role_id]
        return True

    async def run_work_item(self, work_item_id: str) -> Any:
        if self.raise_on_run:
            raise RuntimeError("boom")
        self.runs.append(work_item_id)
        return self.run_result


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "heartbeat.db"))


@pytest.fixture
def org():
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    org.register_role("manager", "Manager", "Delegates tasks")
    return org


@pytest.fixture
def dispatcher():
    return FakeDispatcher()


@pytest.fixture
def scheduler(store, org, dispatcher):
    return HeartbeatScheduler(store=store, org_engine=org, dispatcher=dispatcher)


def _run_and_drain(scheduler: HeartbeatScheduler, coro: Any) -> None:
    async def inner() -> None:
        await coro
        while scheduler._active_runs:
            await asyncio.gather(*list(scheduler._active_runs.values()), return_exceptions=True)
            scheduler._cleanup_done_runs()

    asyncio.run(inner())


def _pending_run(scheduler: HeartbeatScheduler) -> asyncio.Task[Any]:
    """手动塞一个未完成的后台 task，模拟运行中的心跳。"""

    async def _long_running() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_long_running())
    scheduler._active_runs["worker"] = task
    return task


async def _wake(scheduler: HeartbeatScheduler, agent_id: str) -> None:
    scheduler.wakeup(agent_id)


def _pending_item(
    item_id: str,
    *,
    role: str | None = "worker",
    phase: Phase = Phase.QUEUED,
    updated_minutes_ago: int = 0,
) -> Any:
    item = create_work_item(
        title=f"task {item_id}",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=role,
        phase=phase,
    )
    item.id = item_id
    if updated_minutes_ago:
        item.updated_at = datetime.now(UTC) - timedelta(minutes=updated_minutes_ago)
    return item


class TestSchedulerConstruction:
    def test_default_params(self, store, org, dispatcher) -> None:
        s = HeartbeatScheduler(store=store, org_engine=org, dispatcher=dispatcher)
        assert s.heartbeat_role_ids is None
        assert s.interval_sec == 30
        assert s.max_concurrent_runs == 1
        assert not s._running
        assert s._task is None

    def test_custom_params(self, store, org, dispatcher) -> None:
        s = HeartbeatScheduler(
            store=store,
            org_engine=org,
            dispatcher=dispatcher,
            heartbeat_role_ids={"worker"},
            interval_sec=10,
            max_concurrent_runs=3,
        )
        assert s.heartbeat_role_ids == {"worker"}
        assert s.interval_sec == 10
        assert s.max_concurrent_runs == 3

    def test_last_stale_report_empty_initially(self, scheduler) -> None:
        assert scheduler.last_stale_report == []


class TestSchedulerLifecycle:
    def test_start(self, scheduler) -> None:
        asyncio.run(scheduler.start())
        assert scheduler._running
        assert scheduler._task is not None

    def test_start_idempotent(self, scheduler) -> None:
        async def inner() -> None:
            await scheduler.start()
            first = scheduler._task
            await scheduler.start()
            assert scheduler._task is first

        asyncio.run(inner())

    def test_stop(self, scheduler) -> None:
        asyncio.run(scheduler.start())
        asyncio.run(scheduler.stop())
        assert not scheduler._running
        assert scheduler._task is None

    def test_stop_without_start_is_safe(self, scheduler) -> None:
        asyncio.run(scheduler.stop())
        assert not scheduler._running

    def test_stop_cancels_active_runs(self, scheduler, store) -> None:
        async def inner() -> None:
            await scheduler.start()
            scheduler.wakeup("worker")
            await asyncio.sleep(0)
            assert scheduler._active_runs
            await scheduler.stop()
            assert not scheduler._active_runs

        asyncio.run(inner())


class TestWakeup:
    def test_wakeup_runs_agent(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, _wake(scheduler, "worker"))
        assert scheduler._active_runs == {}
        assert "wi-1" in scheduler.dispatcher.runs

    def test_wakeup_skips_when_active_run_exists(self, scheduler, store) -> None:
        async def inner() -> None:
            task = _pending_run(scheduler)
            scheduler.wakeup("worker")
            assert scheduler._active_runs["worker"] is task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(inner())

    def test_wakeup_after_completion_repeats(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, _wake(scheduler, "worker"))
        _run_and_drain(scheduler, _wake(scheduler, "worker"))
        assert scheduler.dispatcher.runs == ["wi-1", "wi-1"]

    def test_wakeup_logs_failure_result(self, scheduler, store, caplog) -> None:
        from bandi.company.seat_executor import SeatResult

        store.save(_pending_item("wi-1", role="worker"))
        scheduler.dispatcher.run_result = SeatResult(success=False, output="seat run failed")
        with caplog.at_level("WARNING"):
            _run_and_drain(scheduler, _wake(scheduler, "worker"))
        assert any("returned failure" in record.message for record in caplog.records)


class TestTickFiltering:
    def test_tick_schedules_white_listed_agents(self, scheduler, store) -> None:
        scheduler.heartbeat_role_ids = {"worker"}
        store.save(_pending_item("wi-1", role="worker"))
        store.save(_pending_item("wi-2", role="manager"))
        _run_and_drain(scheduler, scheduler._tick())
        assert scheduler.dispatcher.runs == ["wi-1"]

    def test_tick_all_agents_when_none_whitelist(self, store, org, dispatcher) -> None:
        s = HeartbeatScheduler(
            store=store,
            org_engine=org,
            dispatcher=dispatcher,
            heartbeat_role_ids={"worker", "manager"},
            max_concurrent_runs=2,
        )
        store.save(_pending_item("wi-1", role="worker"))
        store.save(_pending_item("wi-2", role="manager"))
        _run_and_drain(s, s._tick())
        assert set(s.dispatcher.runs) == {"wi-1", "wi-2"}

    def test_tick_skips_agents_outside_interval(self, scheduler, store) -> None:
        scheduler.heartbeat_role_ids = {"worker"}
        store.save(_pending_item("wi-1", role="worker"))
        scheduler._last_heartbeat["worker"] = datetime.now(UTC)
        _run_and_drain(scheduler, scheduler._tick())
        assert scheduler.dispatcher.runs == []

    def test_tick_respects_max_concurrent(self, store, org, dispatcher) -> None:
        s = HeartbeatScheduler(
            store=store,
            org_engine=org,
            dispatcher=dispatcher,
            heartbeat_role_ids={"worker", "manager"},
        )
        store.save(_pending_item("wi-1", role="worker"))
        store.save(_pending_item("wi-2", role="manager"))
        _run_and_drain(s, s._tick())
        assert len(s.dispatcher.runs) == 1

    def test_tick_skips_agent_with_running_task(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))

        async def inner() -> None:
            task = _pending_run(scheduler)
            await scheduler._tick()
            assert scheduler.dispatcher.runs == []
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(inner())


class TestRunAgentHeartbeat:
    def test_no_candidate_does_nothing(self, scheduler) -> None:
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.claimed == []
        assert scheduler.dispatcher.runs == []

    def test_picks_own_assigned_item(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.runs == ["wi-1"]

    def test_skips_item_assigned_to_other_role(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="manager"))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.runs == []

    def test_concurrent_heartbeats_do_not_double_run_item(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role=None))
        runs: list[str] = []
        gate = asyncio.Event()

        async def slow_run(work_item_id: str) -> None:
            runs.append(work_item_id)
            await gate.wait()

        scheduler.dispatcher.run_work_item = slow_run  # type: ignore[method-assign]

        async def inner() -> None:
            worker_task = asyncio.create_task(scheduler._run_agent_heartbeat("worker"))
            await asyncio.sleep(0)
            manager_task = asyncio.create_task(scheduler._run_agent_heartbeat("manager"))
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(worker_task, manager_task)

        asyncio.run(inner())
        assert runs == ["wi-1"]

    def test_picks_unassigned_item(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role=None))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.runs == ["wi-1"]

    def test_skips_non_todo_phase(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker", phase=Phase.RUNNING))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.runs == []

    def test_skips_terminal_item(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker", phase=Phase.APPROVED))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.runs == []

    def test_claim_failure_skips_run(self, scheduler, store, dispatcher) -> None:
        dispatcher.fail_claim = True
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.runs == []

    def test_run_exception_is_swallowed(self, scheduler, store, dispatcher) -> None:
        dispatcher.raise_on_run = True
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert scheduler.dispatcher.claimed == []
        assert "worker" in scheduler._last_heartbeat

    def test_records_heartbeat_timestamp(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, scheduler._run_agent_heartbeat("worker"))
        assert "worker" in scheduler._last_heartbeat


class TestCleanupDoneRuns:
    def test_removes_done_runs(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, _wake(scheduler, "worker"))
        scheduler._cleanup_done_runs()
        assert scheduler._active_runs == {}

    def test_cleanup_keeps_pending_runs(self, scheduler) -> None:
        async def inner() -> None:
            task = _pending_run(scheduler)
            scheduler._cleanup_done_runs()
            assert "worker" in scheduler._active_runs
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(inner())


class TestResolveStaleWaits:
    def test_detects_stale_peer_wait(self, scheduler, store) -> None:
        store.save(
            _pending_item(
                "wi-1",
                role="worker",
                phase=Phase.WAITING_FOR_PEER,
                updated_minutes_ago=60,
            )
        )
        _run_and_drain(scheduler, scheduler._resolve_stale_waits())
        assert len(scheduler.last_stale_report) == 1
        assert scheduler.last_stale_report[0]["id"] == "wi-1"
        assert scheduler.last_stale_report[0]["phase"] == "waiting_for_peer"
        assert scheduler.last_stale_report[0]["elapsed_minutes"] >= 60

    def test_ignores_fresh_peer_wait(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker", phase=Phase.WAITING_FOR_PEER))
        _run_and_drain(scheduler, scheduler._resolve_stale_waits())
        assert scheduler.last_stale_report == []

    def test_ignores_settlement_released_peer_wait(self, scheduler, store) -> None:
        item = _pending_item("wi-1", role="worker", phase=Phase.WAITING_FOR_PEER)
        item.metadata["dependency_settlement"] = {"all_approved": True}
        item.updated_at = datetime.now(UTC) - timedelta(minutes=60)
        store.save(item)
        _run_and_drain(scheduler, scheduler._resolve_stale_waits())
        assert scheduler.last_stale_report == []

    def test_no_waiting_items_empty_report(self, scheduler, store) -> None:
        store.save(_pending_item("wi-1", role="worker"))
        _run_and_drain(scheduler, scheduler._resolve_stale_waits())
        assert scheduler.last_stale_report == []

    def test_does_not_auto_resume(self, scheduler, store) -> None:
        store.save(
            _pending_item(
                "wi-1",
                role="worker",
                phase=Phase.WAITING_FOR_PEER,
                updated_minutes_ago=60,
            )
        )
        _run_and_drain(scheduler, scheduler._resolve_stale_waits())
        item = store.get("wi-1")
        assert item is not None
        assert item.phase == Phase.WAITING_FOR_PEER


class TestStalledMeetings:
    def test_tick_calls_auto_resolve_stale_meetings(self, scheduler, store) -> None:
        # Create a waiting_for_peer item with meeting kind
        item = _pending_item("wi-meet", role="worker", phase=Phase.WAITING_FOR_PEER)
        item.metadata["peer_wait"] = {"kind": "meeting", "meeting_room_id": "room-1"}
        item.updated_at = datetime.now(UTC) - timedelta(minutes=60)
        store.save(item)
        _run_and_drain(scheduler, scheduler._tick())
        # FakeCommunication auto_resolve_stale_meetings returns empty
        item_after = store.get("wi-meet")
        assert item_after is not None

    def test_tick_skips_non_meeting_waits(self, scheduler, store) -> None:
        item = _pending_item("wi-peer", role="worker", phase=Phase.WAITING_FOR_PEER)
        item.metadata["peer_wait"] = {"kind": "dm", "reply_to": "msg-1"}
        store.save(item)
        _run_and_drain(scheduler, scheduler._tick())
        item_after = store.get("wi-peer")
        assert item_after is not None
        assert item_after.phase == Phase.WAITING_FOR_PEER

    def test_tick_no_communication_skips_meeting_resolution(self, scheduler, store) -> None:
        scheduler.communication = None
        item = _pending_item("wi-x", role="worker", phase=Phase.WAITING_FOR_PEER)
        item.metadata["peer_wait"] = {"kind": "meeting"}
        store.save(item)
        _run_and_drain(scheduler, scheduler._tick())
        assert scheduler.last_stale_report == []


def test_contract_deadlock_detected_from_real_store_data(store, tmp_path) -> None:
    """
    组装后必须报告环——建边数据源是声明式 peer_wait 元数据（角色图）
    不得依赖 waits/running 相位交集（相位互斥导致恒空转的旧 bug）。"""
    from bandi.company.communication import CommunicationManager

    comms = CommunicationManager(str(tmp_path), "p1", "s1")
    item_a = create_work_item(
        "A 等 B",
        WorkItemTurnType.EXECUTE,
        assignee_role="worker_a",
        phase=Phase.WAITING_FOR_PEER,
        metadata={"peer_wait": {"kind": "peer_message", "waiting_on_agents": ["worker_b"]}},
    )
    item_b = create_work_item(
        "B 等 A",
        WorkItemTurnType.EXECUTE,
        assignee_role="worker_b",
        phase=Phase.WAITING_FOR_PEER,
        metadata={"peer_wait": {"kind": "peer_message", "waiting_on_agents": ["worker_a"]}},
    )
    store.save(item_a)
    store.save(item_b)

    scheduler = HeartbeatScheduler(
        store=store,
        org_engine=OrgEngine(),
        dispatcher=FakeDispatcher(),
        communication=comms,
    )

    async def run():
        await scheduler._resolve_stale_waits()

    asyncio.run(run())
    assert scheduler.last_deadlock_report, (
        f"互等环未被检测（waits/running 相位互斥空转 bug）: {scheduler.last_deadlock_report}"
    )


def test_contract_no_deadlock_when_no_mutual_wait(store, tmp_path) -> None:
    from bandi.company.communication import CommunicationManager

    comms = CommunicationManager(str(tmp_path), "p1", "s1")
    item = create_work_item(
        "A 等 B",
        WorkItemTurnType.EXECUTE,
        assignee_role="worker_a",
        phase=Phase.WAITING_FOR_PEER,
        metadata={"peer_wait": {"kind": "peer_message", "waiting_on_agents": ["worker_b"]}},
    )
    store.save(item)

    scheduler = HeartbeatScheduler(
        store=store,
        org_engine=OrgEngine(),
        dispatcher=FakeDispatcher(),
        communication=comms,
    )

    async def run():
        await scheduler._resolve_stale_waits()

    asyncio.run(run())
    assert scheduler.last_deadlock_report == []
