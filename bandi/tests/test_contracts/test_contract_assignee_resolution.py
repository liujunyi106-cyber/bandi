# 功能：指派员工精确化契约——metadata 指定优先 / 角色反查回退 / 无员工不写脏 id
# 回落 role_id（经验/成本会挂错归属），且已在卡上指派的员工（recruiter/秘书写入
# metadata.employee_assignment）优先于角色首位反查——同角色多员工时不得选错人。
from __future__ import annotations

import asyncio
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


class _CapturingSeatExecutor:
    """功能：捕获 run_turn 收到的 runtime（断言 seat 执行器实际拿到的员工身份）。"""

    def __init__(self) -> None:
        self.runtimes: list[Any] = []

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        pass

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        self.runtimes.append(runtime)
        return SeatResult(success=True, output="done")

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _dispatcher(tmp_path: Any, executor: Any) -> CompanyDispatcher:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=executor,
        config=DispatcherConfig(max_concurrent=3),
    )


def _run(disp: CompanyDispatcher, metadata: dict[str, Any] | None = None) -> str:
    item = create_work_item(
        title="指派契约任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata=metadata,
    )
    disp.store.save(item)
    assert disp.claim("worker")
    result = asyncio.run(disp.run_work_item(item.id))
    assert result.success, result.output
    return item.id


def _two_employees(org: OrgEngine) -> None:
    org.register_employee(OrgEmployee(employee_id="E-1", name="一", role_id="worker"))
    org.register_employee(OrgEmployee(employee_id="E-2", name="二", role_id="worker"))


class TestAssigneeResolution:
    def test_metadata_pinned_employee_wins_over_role_first(self, tmp_path: Any) -> None:
        """破坏者：同角色两员工时按卡上指派取 ——不得落到角色首位 。"""
        executor = _CapturingSeatExecutor()
        disp = _dispatcher(tmp_path, executor)
        _two_employees(disp.org_engine)
        item_id = _run(
            disp,
            metadata={"employee_assignment": {"employee_id": "E-2"}},
        )
        assert executor.runtimes[-1].employee_id == "E-2"
        saved = disp.store.get(item_id)
        assert saved is not None
        assignment = dict(saved.metadata.get("employee_assignment", {}) or {})
        assert assignment.get("employee_id") == "E-2"

    def test_role_lookup_falls_back_to_active_employee(self, tmp_path: Any) -> None:
        """无 metadata 指派 → 按角色反查 active 首位（不缺省退化为角色 id）。"""
        executor = _CapturingSeatExecutor()
        disp = _dispatcher(tmp_path, executor)
        _two_employees(disp.org_engine)
        _run(disp)
        assert executor.runtimes[-1].employee_id == "E-1"

    def test_invalid_pinned_employee_falls_back_to_lookup(self, tmp_path: Any) -> None:
        """metadata 指向不存在员工（脏数据）→ 回退角色反查，不得把无效 id 交给 seat。"""
        executor = _CapturingSeatExecutor()
        disp = _dispatcher(tmp_path, executor)
        _two_employees(disp.org_engine)
        item_id = _run(disp, metadata={"employee_assignment": {"employee_id": "E-404"}})
        assert executor.runtimes[-1].employee_id == "E-1"
        saved = disp.store.get(item_id)
        assert saved is not None
        assignment = dict(saved.metadata.get("employee_assignment", {}) or {})
        assert assignment.get("employee_id") == "E-1", "脏 id 不得被回写进 metadata"

    def test_no_employee_never_falls_back_to_role_id(self, tmp_path: Any) -> None:
        """org 无员工 → employee_id 置空跟随全局模型，不得回落 role_id 冒充员工。"""
        executor = _CapturingSeatExecutor()
        disp = _dispatcher(tmp_path, executor)
        item_id = _run(disp)
        assert executor.runtimes[-1].employee_id == ""
        saved = disp.store.get(item_id)
        assert saved is not None
        assignment = saved.metadata.get("employee_assignment")
        assert not assignment or assignment.get("employee_id") != "worker"
