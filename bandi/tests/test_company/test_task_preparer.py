from __future__ import annotations

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.secretary import PolicyScope, SecretaryService
from bandi.company.store import WorkItemStore
from bandi.company.task_preparer import TaskPreparer
from bandi.company.work_item import WorkItemTurnType, create_work_item


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "tp.db"))


@pytest.fixture
def recruiter():
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return recruiter


@pytest.fixture
def runtime():
    return CompanyRuntime(project_id="p", session_scope_id="s", role_id="worker", employee_id="e")


@pytest.mark.asyncio
async def test_prepare_ready_when_conditions_met(store, recruiter, runtime):
    item = create_work_item(title="ok", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker")
    store.save(item)
    recruiter.recruit("worker")
    result = await TaskPreparer().prepare(item, store, recruiter, runtime)
    assert result.ready
    assert result.metadata["prepared"] == item.id


@pytest.mark.asyncio
async def test_prepare_blocks_on_unmet_dependency(store, recruiter, runtime):
    parent = create_work_item(
        title="parent", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
    )
    store.save(parent)
    item = create_work_item(
        title="child",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        children_ids=[parent.id],
    )
    store.save(item)
    result = await TaskPreparer().prepare(item, store, recruiter, runtime)
    assert not result.ready
    assert "dependencies" in result.reason


@pytest.mark.asyncio
async def test_prepare_blocks_without_claimed_seat(store, recruiter, runtime):
    item = create_work_item(
        title="locked", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
    )
    store.save(item)
    result = await TaskPreparer().prepare(item, store, recruiter, runtime)
    assert not result.ready
    assert "claim" in result.reason


@pytest.mark.asyncio
async def test_prepare_allows_dispatched_busy_seat(store, recruiter, runtime):
    """dispatcher 先 claim（seat→BUSY）再 prepare 是生产主链路，BUSY 视为已认领必须放行。"""
    item = create_work_item(
        title="busy", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
    )
    store.save(item)
    assert recruiter.dispatch("worker")
    result = await TaskPreparer().prepare(item, store, recruiter, runtime)
    assert result.ready, "dispatcher claim 后 seat 为 BUSY，prepare 必须放行（否则装配即全量失败）"


@pytest.mark.asyncio
async def test_prepare_blocks_available_seat(store, recruiter, runtime):
    """回归：seat 完全未认领（AVAILABLE）时仍须拒绝——prepare 必须先 claim。"""
    item = create_work_item(
        title="available", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
    )
    store.save(item)
    recruiter.register_seat("worker")
    result = await TaskPreparer().prepare(item, store, recruiter, runtime)
    assert not result.ready
    assert "claim" in result.reason


@pytest.mark.asyncio
async def test_prepare_blocks_without_assignee(store, recruiter, runtime):
    item = create_work_item(title="no role", turn_type=WorkItemTurnType.EXECUTE)
    store.save(item)
    result = await TaskPreparer().prepare(item, store, recruiter, runtime)
    assert not result.ready
    assert "assignee" in result.reason


@pytest.mark.asyncio
async def test_prepare_injects_secretary_policy(store, recruiter, runtime, tmp_path):
    secretary = SecretaryService(base_dir=str(tmp_path / "secretary"))
    secretary.capture_policy("rule", "no sudo", "admin")
    secretary.capture_policy(
        "rule", "project guard", "admin", scope=PolicyScope.PROJECT, project_id="p"
    )
    item = create_work_item(
        title="policy", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
    )
    store.save(item)
    recruiter.recruit("worker")
    result = await TaskPreparer(secretary=secretary).prepare(item, store, recruiter, runtime)
    assert result.ready
    policy = runtime.metadata("secretary_policy")
    assert policy is not None
    assert {entry["project_id"] for entry in policy} == {None, "p"}


def test_rollback_restores_origin_phase(store):
    item = create_work_item(title="rb", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.READY)
    store.save(item)
    TaskPreparer().rollback(item, Phase.READY, store)
    loaded = store.get(item.id)
    assert loaded is not None
    assert loaded.phase == Phase.READY


@pytest.mark.asyncio
async def test_dispatcher_blocks_unprepared_item(store, runtime):
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.seat_executor import EngineSeatExecutor

    org = OrgEngine()
    org.register_role("worker", "Worker", "does work")
    rec = Recruiter()
    rec.register_seat("worker")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=rec,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        task_preparer=TaskPreparer(),
        config=DispatcherConfig(max_concurrent=3),
    )
    item = create_work_item(
        title="blocked", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
    )
    store.save(item)
    result = await dispatcher.run_work_item(item.id)
    assert not result.success
    assert "Task preparation failed" in result.output
