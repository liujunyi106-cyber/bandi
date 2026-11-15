"""契约：执行失败终态不得把工作项永久留在 running。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.phase import Phase
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _dispatcher(tmp_path: Path) -> tuple[CompanyDispatcher, WorkItemStore]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=SimpleNamespace(get_role=lambda role_id: None),  # type: ignore[arg-type]
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    dispatcher.seat_executor = None
    return dispatcher, store


def _running_item(store: WorkItemStore) -> Any:
    item = create_work_item(
        title="执行任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="aid_visual",
        phase=Phase.RUNNING,
    )
    store.save(item)
    return store.get(item.id)


def test_escalated_marks_needs_attention(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    item = _running_item(store)

    dispatcher._mark_failed_execution(
        item.id, SeatResult(success=False, output="", metadata={"terminal_reason": "ESCALATED"})
    )

    fresh = store.get(item.id)
    assert fresh is not None
    assert fresh.phase == Phase.NEEDS_ATTENTION


def test_awaiting_approval_keeps_running(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    item = _running_item(store)

    dispatcher._mark_failed_execution(
        item.id,
        SeatResult(success=False, output="", metadata={"terminal_reason": "AWAITING_APPROVAL"}),
    )

    fresh = store.get(item.id)
    assert fresh is not None
    assert fresh.phase == Phase.RUNNING


def test_success_is_untouched(tmp_path: Path) -> None:
    dispatcher, store = _dispatcher(tmp_path)
    item = _running_item(store)

    dispatcher._mark_failed_execution(item.id, SeatResult(success=True, output=""))

    fresh = store.get(item.id)
    assert fresh is not None
    assert fresh.phase == Phase.RUNNING
