# 功能：契约——company 子批 5 项修复（第 4 轮审计）
# 钉行为（不钉实现）：
# 无幻影 transition 报告。
# 消息仍按 append-only union 合并；内存终态正常落盘不被阻塞。
# store.try_begin_run 原子占位；自动 _fire 对已 RUNNING 任务跳过（不双跑）。
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.company.communication import CommunicationManager, DirectMessage, MeetingStatus
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.company.work_item_transition import (
    DEPENDENCY_WORK_ITEM_IDS_KEY,
    refresh_dependents,
    work_item_id,
)
from bandi.scheduler.scheduler import TaskScheduler
from bandi.scheduler.store import ScheduledTask, TaskStatus, TaskStore

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


# ── 工具 ────────────────────────────────────────────────────────────────


def _dispatcher(store: WorkItemStore) -> CompanyDispatcher:
    return CompanyDispatcher(
        store=store,
        recruiter=Recruiter(),
        org_engine=OrgEngine(),
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )


def _mk_item(store: WorkItemStore, phase: Phase, metadata: dict[str, Any] | None = None) -> str:
    item = create_work_item(title="HB-P1 用例", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = phase
    if metadata:
        item.metadata.update(metadata)
    store.save(item)
    return item.id


class _FakeOrgStore:
    """最小 org 持久化面（save/load roles+employees），用于断言落盘行为。"""

    def __init__(self) -> None:
        self.roles: list[dict[str, Any]] = []
        self.employees: list[dict[str, Any]] = []
        self.save_calls = 0

    def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
        self.roles = list(roles)
        self.save_calls += 1

    def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
        self.employees = list(employees)

    def load_org_roles(self) -> list[dict[str, Any]]:
        return list(self.roles)

    def load_org_employees(self) -> list[dict[str, Any]]:
        return list(self.employees)


# ── 合法等待保护 ────────────────────────────────────────────────────


class TestRecoverStaleClaimsPreservesLegalWaits:
    def test_claimless_waiting_for_peer_not_recovered(self, tmp_path: Path) -> None:
        """无执行痕迹的 WAITING_FOR_PEER = 合法等待，不得重置（否则等待链断、重复升级）。"""
        store = WorkItemStore(str(tmp_path / "wi.db"))
        item_id = _mk_item(
            store,
            Phase.WAITING_FOR_PEER,
            {"peer_wait": {"kind": "meeting", "meeting_room_id": "room-1"}},
        )
        recovered = _dispatcher(store).recover_stale_claims()
        assert recovered == 0
        after = store.get(item_id)
        assert after is not None
        assert after.phase == Phase.WAITING_FOR_PEER, "合法等待不得被崩溃恢复重置"
        assert after.metadata.get("peer_wait"), "等待标记必须保留"

    def test_claimless_waiting_for_children_and_paused_not_recovered(self, tmp_path: Path) -> None:
        store = WorkItemStore(str(tmp_path / "wi.db"))
        children_id = _mk_item(store, Phase.WAITING_FOR_CHILDREN)
        paused_id = _mk_item(store, Phase.PAUSED)
        recovered = _dispatcher(store).recover_stale_claims()
        assert recovered == 0
        assert store.get(children_id).phase == Phase.WAITING_FOR_CHILDREN
        assert store.get(paused_id).phase == Phase.PAUSED

    def test_expired_claim_trace_still_recovered(self, tmp_path: Path) -> None:
        """有执行痕迹 + 租约过期 = 执行者已死，仍应恢复（崩溃路径不被修复破坏）。"""
        store = WorkItemStore(str(tmp_path / "wi.db"))
        expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        item_id = _mk_item(
            store,
            Phase.WAITING_FOR_PEER,
            {
                "peer_wait": {"kind": "meeting", "meeting_room_id": "room-1"},
                "execution_claim": {"instance_id": "dead-proc", "lease_expires_at": expired},
            },
        )
        recovered = _dispatcher(store).recover_stale_claims()
        assert recovered == 1
        after = store.get(item_id)
        assert after.phase == Phase.READY, "过期租约的残留卡必须恢复"

    def test_claimless_running_still_recovered(self, tmp_path: Path) -> None:
        """RUNNING 无痕迹（崩溃兜底）仍恢复——回归保护既有崩溃恢复语义。"""
        store = WorkItemStore(str(tmp_path / "wi.db"))
        item_id = _mk_item(store, Phase.RUNNING)
        recovered = _dispatcher(store).recover_stale_claims()
        assert recovered == 1
        assert store.get(item_id).phase == Phase.READY


# ── apply_changeset 持久化 ──────────────────────────────────────────


class TestApplyChangesetPersists:
    def _changeset(self) -> ReorgChangeSet:
        return ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="add",
                    role={"id": "qa_lead", "name": "质检负责人", "reports_to": "ceo"},
                )
            ]
        )

    def test_persist_true_writes_store_and_survives_reload(self) -> None:
        fake = _FakeOrgStore()
        engine = OrgEngine(persist_store=fake)
        engine.apply_changeset(self._changeset())
        assert any(r.get("role_id") == "qa_lead" for r in fake.roles), (
            "persist=True 必须落盘（此前 apply_changeset 从不调用 _persist）"
        )
        fresh = OrgEngine(persist_store=fake)
        assert fresh.get_role("qa_lead") is not None, "重载后新角色必须存在"

    def test_persist_false_skips_store_but_mutates_memory(self) -> None:
        fake = _FakeOrgStore()
        engine = OrgEngine(persist_store=fake)
        before_calls = fake.save_calls
        engine.apply_changeset(self._changeset(), persist=False)
        assert fake.save_calls == before_calls, "persist=False 不得写盘"
        assert engine.get_role("qa_lead") is not None, "内存变更仍生效"


# ── refresh CAS 失败回滚 ────────────────────────────────────────────


class TestRefreshCasConflictRollsBack:
    def test_conflict_rolls_back_memory_state_without_phantom_transition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_a = WorkItemStore(str(tmp_path / "wi.db"))
        store_b = WorkItemStore(str(tmp_path / "wi.db"))
        dep_id = _mk_item(store_a, Phase.APPROVED)
        item_id = _mk_item(
            store_a,
            Phase.WAITING_DEPENDENCIES,
            {DEPENDENCY_WORK_ITEM_IDS_KEY: [dep_id]},
        )

        captured: dict[str, Any] = {}
        real_list_all = store_a.list_all

        def list_all_with_race() -> list[Any]:
            items = real_list_all()
            captured["items"] = items
            other = store_b.get(item_id)
            assert other is not None
            other.metadata["concurrent_probe"] = "keep-me"
            store_b.save(other)
            return items

        monkeypatch.setattr(store_a, "list_all", list_all_with_race)
        errors: list[str] = []
        transitions: list[tuple[str, str, str]] = []
        refresh_dependents(store_a, transitions_out=transitions, errors_out=errors)

        target = next(i for i in captured["items"] if work_item_id(i) == item_id)
        assert target.phase == Phase.WAITING_DEPENDENCIES, (
            "CAS 提交失败必须回滚内存态（否则同 tick 后续逻辑消费脏 phase）"
        )
        assert any(item_id in e for e in errors), "冲突必须记入 errors_out"
        assert all(t[0] != item_id for t in transitions), "不得产生幻影 transition"
        final = store_b.get(item_id)
        assert final is not None
        assert final.metadata.get("concurrent_probe") == "keep-me", "他进程数据不得被覆盖"


# ── 会议终态不可逆 ──────────────────────────────────────────────────


class TestMeetingTerminalStatusNotRegressed:
    def _open_room(self, workspace: str) -> tuple[CommunicationManager, str]:
        mgr = CommunicationManager(workspace, "p", "s")
        task = create_work_item(title="会议卡", turn_type=WorkItemTurnType.EXECUTE)
        task.phase = Phase.RUNNING
        mgr.open_meeting_wait(task, "topic", ["alice"], "agenda")
        room_id = str(task.metadata["peer_wait"]["meeting_room_id"])
        return mgr, room_id

    def test_stale_snapshot_cannot_reopen_decided_room(self, tmp_path: Path) -> None:
        workspace = str(tmp_path / "ws")
        mgr_a, room_id = self._open_room(workspace)
        room_a = mgr_a._rooms[room_id]
        room_a.status = MeetingStatus.IN_PROGRESS
        mgr_a._persist_room(room_a)  # 盘上：进行中

        mgr_b = CommunicationManager(workspace, "p", "s")
        room_b = mgr_b.get_room(room_id)
        assert room_b is not None
        room_b.status = MeetingStatus.DECIDED
        room_b.outcome = "已达成共识"
        room_b.consensus = True
        mgr_b._persist_room(room_b)  # 他进程：终态先落盘

        room_a.messages.append(
            DirectMessage(sender_role="alice", recipient_role="bob", content="补充发言")
        )
        mgr_a._persist_room(room_a)  # 旧快照回写（LWW 攻击点）

        # 第三人新实例强制从盘加载（mgr_b 有内存缓存，不能当盘用）
        disk = CommunicationManager(workspace, "p", "s").get_room(room_id)
        assert disk is not None
        assert disk.status == MeetingStatus.DECIDED, "终态不得被旧快照拉回（会议复活）"
        assert disk.outcome == "已达成共识" and disk.consensus is True
        assert any(m.content == "补充发言" for m in disk.messages), "消息仍须 append-only 合并"

    def test_memory_terminal_flush_not_blocked(self, tmp_path: Path) -> None:
        """正常 finalize：内存终态写盘不被"盘上旧状态"阻塞。"""
        workspace = str(tmp_path / "ws")
        mgr_a, room_id = self._open_room(workspace)
        room_a = mgr_a._rooms[room_id]
        room_a.status = MeetingStatus.IN_PROGRESS
        mgr_a._persist_room(room_a)
        room_a.status = MeetingStatus.DECIDED
        room_a.outcome = "正常决议"
        mgr_a._persist_room(room_a)
        disk = CommunicationManager(workspace, "p", "s").get_room(room_id)
        assert disk is not None
        assert disk.status == MeetingStatus.DECIDED and disk.outcome == "正常决议"


# ── 调度互斥 ────────────────────────────────────────────────────────


class TestSchedulerTriggerMutex:
    def test_try_begin_run_atomic(self) -> None:
        store = TaskStore(":memory:")
        store.save(ScheduledTask(task_id="t1", name="n", cron="0 9 * * *", prompt="p"))
        assert store.try_begin_run("t1") is True
        assert store.try_begin_run("t1") is False, "RUNNING 中不得重复占位"
        store.set_status("t1", TaskStatus.FAILED)
        assert store.try_begin_run("t1") is True, "FAILED 后允许再次触发（手动重试）"

    def test_trigger_409_when_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = sys.modules["bandi.api.app"]
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        fresh = TaskStore(":memory:")
        calls: list[str] = []

        async def fake_runner(task: ScheduledTask) -> None:
            calls.append(task.task_id)

        with (
            patch("bandi.api.app._API_KEY", "test"),
            patch("bandi.api.app._task_store", fresh),
            patch("bandi.api.app._task_runner", fake_runner),
            patch("bandi.api.app._task_scheduler", None),
        ):
            tid = client.post(
                "/api/scheduler/tasks",
                json={"name": "mutex", "cron": "0 9 * * *", "prompt": "p"},
            ).json()["task_id"]
            fresh.set_status(tid, TaskStatus.RUNNING)  # 他处（自动 tick/另一请求）跑中
            resp = client.post(f"/api/scheduler/tasks/{tid}/trigger")
            assert resp.status_code == 409, "RUNNING 中手动触发必须 409"
            assert "TASK_BUSY" in resp.text, "冻结错误体须含可识别错误码"
            assert calls == [], "互斥生效时 runner 绝不能被调用"
            assert fresh.get(tid).status == TaskStatus.RUNNING, "状态不得被吞"
        leaked = app_mod._task_scheduler
        if leaked is not None:
            try:
                asyncio.run(leaked.stop())
            except RuntimeError:
                pass

    def test_fire_skips_task_already_running(self) -> None:
        store = TaskStore(":memory:")
        store.save(ScheduledTask(task_id="t1", name="n", cron="0 9 * * *", prompt="p"))
        task = store.get("t1")
        assert task is not None
        store.set_status("t1", TaskStatus.RUNNING)

        fired: list[str] = []

        async def runner(t: ScheduledTask) -> None:
            fired.append(t.task_id)

        sched = TaskScheduler(store=store, runner=runner)
        asyncio.run(sched._fire(task, datetime.now(UTC)))
        assert fired == [], "自动触发对 RUNNING 任务必须跳过（防双跑）"
        assert store.get("t1").status == TaskStatus.RUNNING

    def test_fire_runs_idle_task(self) -> None:
        store = TaskStore(":memory:")
        store.save(ScheduledTask(task_id="t1", name="n", cron="0 9 * * *", prompt="p"))
        task = store.get("t1")
        assert task is not None

        fired: list[str] = []

        async def runner(t: ScheduledTask) -> None:
            fired.append(t.task_id)

        sched = TaskScheduler(store=store, runner=runner)
        asyncio.run(sched._fire(task, datetime.now(UTC)))
        assert fired == ["t1"]
        assert store.get("t1").status == TaskStatus.IDLE
