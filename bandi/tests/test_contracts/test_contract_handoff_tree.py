# 功能：契约——Agent 间派活树（单卡等待门、上游保护、结果回传、长结果落盘、重启恢复）。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bandi.company.company_mode import CompanyDispatcher
from bandi.company.discussion_thread import ThreadManager
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.seat_recruiter import Recruiter
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.company.work_item_transition import (
    DELEGATED_CHILDREN_PENDING_KEY,
    DEPENDENCY_WORK_ITEM_IDS_KEY,
)


class _FakeSeat:
    def __init__(self, output: str = "父卡结果") -> None:
        self.output = output
        self.calls: list[str] = []

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        return None

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        self.calls.append(str(work_item.assignee_role or ""))
        return SeatResult(success=True, output=self.output)

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _org() -> OrgEngine:
    org = OrgEngine()
    for role_id in ("boss", "lead", "worker"):
        org.register_role(role_id, role_id)
    return org


def _parent(store: WorkItemStore, org: OrgEngine, role: str = "lead") -> Any:
    parent = create_work_item(
        title="父任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=role,
    )
    store.save(parent)
    return parent


def test_single_spawn_registers_parent_barrier(tmp_path: Path) -> None:
    from bandi.tools.thread_task import SpawnThreadTaskTool

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _org()
    threads = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    parent = _parent(store, org)
    tool = SpawnThreadTaskTool()
    tool.bind_deps(store=store, thread_manager=threads, org_engine=org)
    tool.current_work_item = parent

    result = asyncio.run(
        tool.execute(
            {"target_role": "worker", "task_prompt": "做实现"},
            context=None,
        )
    )
    assert result.get("success") is True, result
    child_id = result["work_item_id"]
    parent_now = store.get(parent.id)
    assert parent_now is not None
    assert parent_now.metadata.get(DELEGATED_CHILDREN_PENDING_KEY) is True
    assert parent_now.metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY) == [child_id]


def test_spawn_rejects_upstream_waiter(tmp_path: Path) -> None:
    from bandi.tools.thread_task import SpawnThreadTaskTool

    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _org()
    threads = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p1", session_id="s1")
    grandparent = _parent(store, org, role="boss")
    parent = create_work_item(
        title="中间任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="lead",
        parent_id=grandparent.id,
    )
    store.save(parent)
    tool = SpawnThreadTaskTool()
    tool.bind_deps(store=store, thread_manager=threads, org_engine=org)
    tool.current_work_item = parent

    result = asyncio.run(tool.execute({"target_role": "boss", "task_prompt": "回派"}, context=None))
    assert "success" not in result or result.get("success") is False
    assert "waiting for this work" in str(result)


@pytest.mark.asyncio
async def test_parent_holds_output_until_child_terminal(tmp_path: Path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _org()
    recruiter = Recruiter()
    for role_id in ("lead", "worker"):
        recruiter.register_seat(role_id)
        recruiter.recruit(role_id)
    seat = _FakeSeat()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        seat_executor=seat,
    )
    parent = create_work_item(
        title="父任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="lead",
        metadata={DELEGATED_CHILDREN_PENDING_KEY: True},
    )
    store.save(parent)
    child = create_work_item(
        title="子任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        parent_id=parent.id,
        phase=Phase.QUEUED,
    )
    store.save(child)
    parent_now = store.get(parent.id)
    assert parent_now is not None
    parent_now.metadata[DEPENDENCY_WORK_ITEM_IDS_KEY] = [child.id]
    store.save(parent_now)

    result = await dispatcher.run_work_item(parent.id)
    assert result.success
    parent_now = store.get(parent.id)
    assert parent_now is not None
    assert parent_now.phase == Phase.WAITING_FOR_CHILDREN
    assert parent_now.metadata["delegated_children_pending"] is True

    child_now = store.get(child.id)
    assert child_now is not None
    child_now.phase = Phase.APPROVED
    store.save(child_now)
    await dispatcher.tick()
    await dispatcher.tick()
    assert seat.calls == ["lead", "lead"]
    parent_now = store.get(parent.id)
    assert parent_now is not None
    assert parent_now.phase in (Phase.AWAITING_MANAGER_REVIEW, Phase.APPROVED)


def test_long_child_result_is_persisted_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BANDI_TOOL_OUTPUT_DIR", str(tmp_path / "tool_outputs"))
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = create_work_item(
        title="父任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="lead",
        metadata={DEPENDENCY_WORK_ITEM_IDS_KEY: ["child-1"]},
    )
    store.save(parent)
    child = create_work_item(
        title="子任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.APPROVED,
    )
    child.id = "child-1"
    child.runtime_metadata["_last_output"] = "A" * 7000
    store.save(child)
    executor = EngineSeatExecutor(store=store)
    rendered = executor._render_dependency_results(parent)
    assert "完整结果见：" in rendered
    assert list((tmp_path / "tool_outputs").glob("handoff_*.txt"))
