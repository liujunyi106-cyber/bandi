"""功能：经理工具面契约——board 巡检 / 子卡修订 / 子卡取消。

钉行为不钉实现：所有权 fail-closed（仅父卡负责人）、阶段约束（运行中/评审中/终态拒改）、
删除清依赖屏障、变更记账写父卡 metadata（dispatch guard 依据）。
"""

from __future__ import annotations

import pytest

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item
from bandi.tools.base import RetryPolicy, ToolPermissionContext
from bandi.tools.manager_board import (
    DeleteWorkItemTool,
    ManagerBoardReadTool,
    ModifyWorkItemTool,
)


class _FakeOrg:
    def list_roles(self) -> list[object]:
        return [type("Role", (), {"id": r})() for r in ("lead", "worker", "other")]


def _ctx(role: str = "lead") -> ToolPermissionContext:
    return ToolPermissionContext(role=role)


def _save_item(
    store: WorkItemStore,
    *,
    title: str,
    role: str,
    parent_id: str | None = None,
    phase: Phase = Phase.QUEUED,
    metadata: dict | None = None,
) -> WorkItem:
    item = create_work_item(
        title=title,
        turn_type=WorkItemTurnType.EXECUTE,
        description=f"{title} 简报",
        assignee_role=role,
        parent_id=parent_id,
        metadata=metadata or {},
        phase=phase,
    )
    store.save(item)
    return item


def _parent(store: WorkItemStore) -> WorkItem:
    return _save_item(store, title="父卡", role="lead")


@pytest.mark.asyncio
async def test_board_read_lists_children(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = _parent(store)
    child_a = _save_item(store, title="子卡A", role="worker", parent_id=parent.id)
    child_b = _save_item(
        store, title="子卡B", role="worker", parent_id=parent.id, phase=Phase.CANCELLED
    )
    tool = ManagerBoardReadTool(store=store, org_engine=_FakeOrg())
    result = await tool.execute({"parent_work_item_id": parent.id}, _ctx())
    assert result["success"] is True
    rows = {row["id"]: row for row in result["children"]}
    assert set(rows) == {child_a.id, child_b.id}
    assert rows[child_a.id]["assignee_role"] == "worker"
    assert rows[child_a.id]["phase"] == "queued"
    assert rows[child_a.id]["turn_type"] == "execute"

    result = await tool.execute(
        {"parent_work_item_id": parent.id, "include_terminal": False}, _ctx()
    )
    assert [row["id"] for row in result["children"]] == [child_a.id]


@pytest.mark.asyncio
async def test_board_read_owner_and_deps_fail_closed(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = _parent(store)
    tool = ManagerBoardReadTool(store=store, org_engine=_FakeOrg())
    result = await tool.execute({"parent_work_item_id": parent.id}, _ctx(role="other"))
    assert result["success"] is False
    result = await tool.execute({"parent_work_item_id": "ghost"}, _ctx())
    assert result["success"] is False
    bare = ManagerBoardReadTool()
    result = await bare.execute({"parent_work_item_id": parent.id}, _ctx())
    assert result["success"] is False


@pytest.mark.asyncio
async def test_modify_child_updates_fields_and_records_mutation(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = _parent(store)
    child = _save_item(store, title="旧标题", role="worker", parent_id=parent.id)
    tool = ModifyWorkItemTool(store=store, org_engine=_FakeOrg())
    result = await tool.execute(
        {
            "work_item_id": child.id,
            "title": "新标题",
            "description": "新简报",
            "turn_type": "plan",
        },
        _ctx(),
    )
    assert result["success"] is True
    fresh = store.get(child.id)
    assert fresh is not None
    assert fresh.title == "新标题"
    assert fresh.description == "新简报"
    assert fresh.turn_type == WorkItemTurnType.PLAN
    parent_fresh = store.get(parent.id)
    assert parent_fresh is not None
    assert parent_fresh.metadata["manager_board_mutation_performed"] is True
    assert child.id in parent_fresh.metadata["manager_board_modified_work_item_ids"]


@pytest.mark.asyncio
async def test_modify_rejects_running_ownerless_and_empty(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = _parent(store)
    running = _save_item(
        store, title="运行中", role="worker", parent_id=parent.id, phase=Phase.RUNNING
    )
    queued = _save_item(store, title="排队", role="worker", parent_id=parent.id)
    tool = ModifyWorkItemTool(store=store, org_engine=_FakeOrg())
    assert (await tool.execute({"work_item_id": running.id, "title": "x"}, _ctx()))[
        "success"
    ] is False
    assert (await tool.execute({"work_item_id": queued.id, "title": "x"}, _ctx(role="other")))[
        "success"
    ] is False
    assert (await tool.execute({"work_item_id": queued.id}, _ctx()))["success"] is False
    assert (await tool.execute({"work_item_id": queued.id, "turn_type": "bogus"}, _ctx()))[
        "success"
    ] is False
    root = _save_item(store, title="顶层卡", role="lead")
    assert (await tool.execute({"work_item_id": root.id, "title": "x"}, _ctx()))["success"] is False


@pytest.mark.asyncio
async def test_delete_cancels_child_and_clears_dependency(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = _save_item(
        store,
        title="父卡",
        role="lead",
        metadata={"dependency_work_item_ids": []},
    )
    child = _save_item(store, title="待取消", role="worker", parent_id=parent.id)
    other = _save_item(store, title="保留", role="worker", parent_id=parent.id)
    parent_fresh = store.get(parent.id)
    assert parent_fresh is not None
    parent_fresh.metadata["dependency_work_item_ids"] = [child.id, other.id]
    store.save(parent_fresh)

    tool = DeleteWorkItemTool(store=store, org_engine=_FakeOrg())
    result = await tool.execute({"work_item_id": child.id, "reason": "方向已变"}, _ctx())
    assert result["success"] is True
    assert store.get(child.id).phase == Phase.CANCELLED
    parent_final = store.get(parent.id)
    assert parent_final is not None
    assert parent_final.metadata["dependency_work_item_ids"] == [other.id]
    assert parent_final.metadata["manager_board_mutation_performed"] is True
    assert child.id in parent_final.metadata["manager_board_deleted_work_item_ids"]


@pytest.mark.asyncio
async def test_delete_rejects_running_live_children_and_ownerless(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    parent = _parent(store)
    running = _save_item(
        store, title="运行中", role="worker", parent_id=parent.id, phase=Phase.RUNNING
    )
    with_child = _save_item(store, title="有子卡", role="worker", parent_id=parent.id)
    _save_item(store, title="孙卡", role="worker", parent_id=with_child.id)
    tool = DeleteWorkItemTool(store=store, org_engine=_FakeOrg())
    assert (await tool.execute({"work_item_id": running.id}, _ctx()))["success"] is False
    assert (await tool.execute({"work_item_id": with_child.id}, _ctx()))["success"] is False
    assert (await tool.execute({"work_item_id": with_child.id}, _ctx(role="other")))[
        "success"
    ] is False


def test_manager_tools_declare_non_retryable() -> None:
    for cls in (ManagerBoardReadTool, ModifyWorkItemTool, DeleteWorkItemTool):
        policy = cls.retry_policy
        assert isinstance(policy, RetryPolicy) and policy.retryable is False
