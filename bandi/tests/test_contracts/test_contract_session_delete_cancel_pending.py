# 功能：契约——删除会话时取消该会话发起的非终态工作项（排队/等待卡不再执行）。
from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.company.company_mode import CompanyDispatcher
from bandi.company.org_engine import OrgEngine
from bandi.company.seat_recruiter import Recruiter
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase, WorkItemTurnType, create_work_item


def _dispatcher(tmp_path: Path) -> tuple[CompanyDispatcher, WorkItemStore]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    dispatcher = CompanyDispatcher(store=store, recruiter=Recruiter(), org_engine=OrgEngine())
    return dispatcher, store


def _item(store: WorkItemStore, session_id: str, phase: Phase = Phase.QUEUED):
    item = create_work_item(
        title="待执行任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata={"origin_session_id": session_id},
        phase=phase,
    )
    store.save(item)
    return item


def test_cancel_pending_item_marks_cancelled(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    try:
        item = _item(store, "sess-a")
        assert asyncio.run(dispatcher.cancel_work_item(item.id, reason="session_deleted"))
        assert store.get(item.id).phase == Phase.CANCELLED
    finally:
        store.close()


def test_cancel_does_not_touch_other_sessions(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    try:
        target = _item(store, "sess-a")
        other = _item(store, "sess-b")
        assert asyncio.run(dispatcher.cancel_work_item(target.id, reason="session_deleted"))
        assert store.get(target.id).phase == Phase.CANCELLED
        assert store.get(other.id).phase == Phase.QUEUED
    finally:
        store.close()


def test_cancel_terminal_item_is_noop(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    try:
        item = _item(store, "sess-a", phase=Phase.CANCELLED)
        assert asyncio.run(dispatcher.cancel_work_item(item.id, reason="session_deleted")) is False
        assert store.get(item.id).phase == Phase.CANCELLED
    finally:
        store.close()


def test_cancel_running_item_ends_cancelled(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    try:
        item = _item(store, "sess-a", phase=Phase.RUNNING)
        assert asyncio.run(dispatcher.cancel_work_item(item.id, reason="session_deleted"))
        assert store.get(item.id).phase == Phase.CANCELLED
    finally:
        store.close()
