"""
契约族：peer wait 解析接入 tick。

背景：CommunicationManager.refresh_waiting_tasks / resolve_task_peer_wait
（回复到达解除 / 超时升级 manager / 超时升级 meeting）生产零调用——
WAITING_FOR_PEER 卡永不流转（回复到了不解除、超时不升级）。

本契约钉的行为族（生产调用路径）：
- `company/company_mode.py::CompanyDispatcher.tick`（调度入口）
- `company/communication.py::refresh_waiting_tasks`（批量解析）

断言「最终产物」：
- 回复已到达的等待卡：tick 后解除回 READY（可被重新 claim）且持久化；
- 超时 on_timeout=manager：tick 后升级 DM 发出 + 卡回 READY；
- 超时 on_timeout=meeting：tick 后建会议室 + 卡保持 WAITING_FOR_PEER
 且 escalated_from 落库（metadata 变更持久化）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _mk_dispatcher(tmp_path: Path) -> tuple[CompanyDispatcher, WorkItemStore, CommunicationManager]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine()
    comm = CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
    )
    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=org,
        communication=comm,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    return disp, store, comm


def _mk_waiting_card(store: WorkItemStore, item_id: str, wait: dict):
    item = create_work_item(title=f"task-{item_id}", turn_type=WorkItemTurnType.EXECUTE)
    item.id = item_id
    item.phase = Phase.WAITING_FOR_PEER
    item.assignee_role = "worker-1"
    item.metadata["peer_wait"] = dict(wait)
    store.save(item)
    return store.get(item.id)  # type: ignore[return-value]


def test_contract_tick_resolves_replied_wait(tmp_path: Path) -> None:
    """回复已到达的等待卡：tick 后解除回 READY 且落库。"""
    disp, store, comm = _mk_dispatcher(tmp_path)
    _mk_waiting_card(
        store,
        "w1",
        {
            "message_id": "m-1",
            "waiting_on_agents": ["peer-a"],
            "timeout_at": None,
            "on_timeout": "continue",
        },
    )
    # 模拟 peer 回复已到达（同 message_id 主题的 DM 落 inbox）
    comm.send_dm(
        sender_role="peer-a",
        recipient_role="worker-1",
        subject="Re: task-w1",
        body="done, here is the answer",
        extra_metadata={"reply_to_msg_id": "m-1"},
    )
    report = asyncio_run_tick(disp)
    resolved_ids = report.metadata.get("peer_wait_resolved", [])
    assert "w1" in resolved_ids, f"回复到达的等待卡必须被 tick 解除，resolved={resolved_ids}"
    saved = store.get("w1")
    assert saved is not None and saved.phase == Phase.READY, (
        f"解除后必须回 READY 且落库，实际 {saved.phase if saved else None}"
    )
    assert saved.metadata.get("peer_wait_released") is True


def asyncio_run_tick(disp: CompanyDispatcher):
    import asyncio

    return asyncio.run(disp.tick())


def test_contract_tick_escalates_timeout_to_manager(tmp_path: Path) -> None:
    """超时 on_timeout=manager：tick 后升级 DM + 卡回 READY。"""
    disp, store, comm = _mk_dispatcher(tmp_path)
    past = (datetime.now(UTC) - timedelta(seconds=10)).timestamp()
    _mk_waiting_card(
        store,
        "w2",
        {
            "message_id": "m-2",
            "waiting_on_agents": ["peer-b"],
            "timeout_at": past,
            "on_timeout": "manager",
            "manager_role": "manager-1",
        },
    )
    report = asyncio_run_tick(disp)
    assert "w2" in report.metadata.get("peer_wait_resolved", []), "超时升级必须计入 resolved"
    saved = store.get("w2")
    assert saved is not None and saved.phase == Phase.READY
    assert saved.metadata.get("peer_timeout_escalation", {}).get("mode") == "manager"


def test_contract_tick_escalates_timeout_to_meeting_persists(tmp_path: Path) -> None:
    """超时 on_timeout=meeting：建会议室 + 卡保持 WAITING + escalated_from 落库。"""
    disp, store, comm = _mk_dispatcher(tmp_path)
    past = (datetime.now(UTC) - timedelta(seconds=10)).timestamp()
    _mk_waiting_card(
        store,
        "w3",
        {
            "message_id": "m-3",
            "waiting_on_agents": ["peer-c"],
            "timeout_at": past,
            "on_timeout": "meeting",
            "manager_role": "manager-1",
        },
    )
    report = asyncio_run_tick(disp)
    saved = store.get("w3")
    assert saved is not None, "卡必须存在"
    wait = saved.metadata.get("peer_wait", {})
    assert wait.get("kind") == "meeting", f"必须升级为 meeting，实际 {wait}"
    assert wait.get("escalated_from"), "escalated_from 必须落库（metadata 变更持久化）"
    assert wait.get("meeting_room_id"), "会议室必须创建"
    assert "w3" not in report.metadata.get("peer_wait_resolved", []), (
        "meeting 升级未解除，不计入 resolved"
    )


def test_contract_meeting_upgrade_not_rewritten_every_tick(tmp_path: Path, monkeypatch) -> None:
    """已升级 meeting 卡只落库一次，后续 tick 无变更不得全量重写（幂等写放大）。"""
    disp, store, comm = _mk_dispatcher(tmp_path)
    past = (datetime.now(UTC) - timedelta(seconds=10)).timestamp()
    _mk_waiting_card(
        store,
        "w4",
        {
            "message_id": "m-4",
            "waiting_on_agents": ["peer-d"],
            "timeout_at": past,
            "on_timeout": "meeting",
            "manager_role": "manager-1",
        },
    )
    calls: list[str] = []
    original_save = store.save

    def counting_save(item):
        calls.append(item.id)
        return original_save(item)

    monkeypatch.setattr(store, "save", counting_save)
    asyncio_run_tick(disp)
    assert calls.count("w4") == 1, f"首次升级必须落库一次，实际 {calls.count('w4')}"
    calls.clear()
    asyncio_run_tick(disp)
    assert calls.count("w4") == 0, (
        f"已升级 meeting 卡无变更不得每 tick 重写（实际 {calls.count('w4')} 次）"
    )
