# 功能：路线 接线契约——run_work_item 按 assigned_external_agent 分支选择执行器
# （解析有、执行无是 根因；本契约钉：选外部走 ExternalSeatExecutor
# force_native 保持引擎、外部执行器缺位时显式失败不静默回退 native）

from __future__ import annotations

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "external-branch.db"))


class _RecordingExecutor:
    """完整实现 SeatExecutor 接口的记录桩——区分 engine/external 调用。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def prepare_seat(self, runtime, work_item) -> None:
        self.calls.append("prepare")

    async def run_turn(self, runtime, work_item) -> SeatResult:
        self.calls.append("run")
        return SeatResult(success=True, output=f"{self.name}-done", metadata={})

    async def checkpoint(self, runtime) -> dict:
        return {}

    async def interrupt(self, runtime) -> None:
        self.calls.append("interrupt")

    async def shutdown(self) -> None:
        pass


def _dispatcher(store: WorkItemStore, external: _RecordingExecutor | None) -> CompanyDispatcher:
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_RecordingExecutor("engine"),
        external_seat_executor=external,
        config=DispatcherConfig(max_concurrent=3),
    )


def _item(agent: str | None, force_native: bool = False):
    item = create_work_item(
        title="routed task",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    if agent is not None:
        item.metadata["selected_execution_agent"] = agent
    if force_native:
        item.metadata["force_native_execution"] = True
    return item


@pytest.mark.asyncio
async def test_assigned_external_routes_to_external_executor(store):
    external = _RecordingExecutor("external")
    disp = _dispatcher(store, external)
    item = _item("opencode")
    disp.store.save(item)
    assert disp.claim("worker")
    result = await disp.run_work_item(item.id)
    assert result.success is True
    assert result.output == "external-done"
    assert "run" in external.calls


@pytest.mark.asyncio
async def test_force_native_stays_engine(store):
    external = _RecordingExecutor("external")
    disp = _dispatcher(store, external)
    item = _item("opencode", force_native=True)
    disp.store.save(item)
    assert disp.claim("worker")
    result = await disp.run_work_item(item.id)
    assert result.output == "engine-done"
    assert external.calls == []


@pytest.mark.asyncio
async def test_no_assignment_stays_engine(store):
    disp = _dispatcher(store, None)
    item = _item(None)
    disp.store.save(item)
    assert disp.claim("worker")
    result = await disp.run_work_item(item.id)
    assert result.output == "engine-done"


@pytest.mark.asyncio
async def test_assigned_external_without_executor_fails_closed(store):
    # 且租约清理 + 卡回 READY。
    disp = _dispatcher(store, None)
    item = _item("opencode")
    disp.store.save(item)
    assert disp.claim("worker")
    result = await disp.run_work_item(item.id)
    assert result.success is False
    assert "external" in result.output.lower()
    refreshed = disp.store.get(item.id)
    assert refreshed.phase == Phase.READY


@pytest.mark.asyncio
async def test_force_native_string_false_routes_external(store):
    # API/JSON 层布尔被序列化为字符串 "false" 时，不得误判为强制 native
    external = _RecordingExecutor("external")
    disp = _dispatcher(store, external)
    item = _item("opencode", force_native=True)
    item.metadata["force_native_execution"] = "false"
    disp.store.save(item)
    assert disp.claim("worker")
    result = await disp.run_work_item(item.id)
    assert result.output == "external-done", "字符串 'false' 必须按假值处理，走外部执行器"


@pytest.mark.asyncio
async def test_force_native_string_true_stays_engine(store):
    external = _RecordingExecutor("external")
    disp = _dispatcher(store, external)
    item = _item("opencode", force_native=True)
    item.metadata["force_native_execution"] = "true"
    disp.store.save(item)
    assert disp.claim("worker")
    result = await disp.run_work_item(item.id)
    assert result.output == "engine-done", "字符串 'true' 必须按真值处理，留在引擎"
