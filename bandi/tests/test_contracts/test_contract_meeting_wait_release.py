# 功能：契约——会议正常决议（DECIDED）必须解锁关联任务等待（此前仅超时兜底可解锁）。
# 破坏者视角：通过会议升级的任务在参会者正常 finalize 后永久滞留 WAITING_FOR_PEER；
# 只有「超时未决」的兜底路径能解锁，正常决议反而死锁（与使用直觉相反）。
from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _mk_task_with_meeting(
    comm: CommunicationManager, store: WorkItemStore | None = None, task_id: str = "m1"
):
    """功能：构造经会议升级的等待卡——open_meeting_wait 建会议室并挂 peer_wait。"""
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    item.id = task_id
    item.assignee_role = "alice"
    item.phase = Phase.RUNNING
    comm.open_meeting_wait(item, "topic", ["alice", "bob"], "agenda", decision_owner="alice")
    if store is not None:
        store.save(item)
    return item


def test_contract_auto_resolve_releases_decided_meeting(tmp_path: Path) -> None:
    """契约（正常决议必解锁）：会议在 deadline 前正常 finalize（DECIDED）后
    auto_resolve_stale_meetings 必须解除任务等待回 READY——修复前 DECIDED 被
    continue 跳过，任务永久滞留 WAITING_FOR_PEER。"""
    comm = CommunicationManager(str(tmp_path / "ws"), "p", "s")
    item = _mk_task_with_meeting(comm, task_id="m1")
    room_id = item.metadata["peer_wait"]["meeting_room_id"]
    comm.respond_to_meeting_v2(room_id, "alice", "用 Python", finalize=True)
    assert item.phase == Phase.WAITING_FOR_PEER

    resolved = comm.auto_resolve_stale_meetings([item])
    assert resolved == ["m1"], "正常决议的会议卡必须被解锁"
    assert item.phase == Phase.READY
    assert "peer_wait" not in item.metadata
    assert item.metadata["meeting_outcome"] == "用 Python"


def test_contract_tick_releases_decided_meeting(tmp_path: Path) -> None:
    """契约（DoD-12 生产链）：dispatcher 单次 tick 必须解锁正常决议的会议卡
    （heartbeat 未运行的桌面/API 手动 tick 场景是主路径）。"""
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1", org_engine=org
    )
    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=org,
        communication=comm,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    item = _mk_task_with_meeting(comm, store=store, task_id="m2")
    room_id = item.metadata["peer_wait"]["meeting_room_id"]
    comm.respond_to_meeting_v2(room_id, "alice", "结论：走方案 A", finalize=True)

    report = asyncio.run(disp.tick())
    saved = store.get("m2")
    assert saved is not None and saved.phase == Phase.READY, "tick 后正常决议的会议卡必须回 READY"
    assert "m2" in report.metadata.get("peer_wait_resolved", [])
    assert saved.metadata.get("meeting_outcome") == "结论：走方案 A"
    store._conn.close()


def test_contract_stale_wait_on_non_waiting_card_cleaned_without_transition(
    tmp_path: Path,
) -> None:
    """契约（复查加固·破坏者）：卡已被取消/重派（phase 非 WAITING_FOR_PEER）但遗留
    陈旧 meeting wait 时，auto_resolve 必须只清理元数据、不做非法相位迁移——
    修复前 `_transition_phase(CANCELLED→READY)` 被状态机拒绝并抛错穿透调用方。"""
    comm = CommunicationManager(str(tmp_path / "ws"), "p", "s")
    item = _mk_task_with_meeting(comm, task_id="m3")
    room_id = item.metadata["peer_wait"]["meeting_room_id"]
    comm.respond_to_meeting_v2(room_id, "alice", "结论", finalize=True)
    item.phase = Phase.CANCELLED

    resolved = comm.auto_resolve_stale_meetings([item])
    assert resolved == ["m3"], "陈旧等待应被清理并计入 resolved"
    assert item.phase == Phase.CANCELLED, "已取消卡不得被迁移"
    assert "peer_wait" not in item.metadata, "陈旧 wait 必须清除"
