# 功能：外部执行链三合一 E2E——CompanyDispatcher 完整调度 + ExternalSeatExecutor
# 真实子进程（FAKE 命令） + WorkItemStore 最终产物断言
# 契约直连 run_turn，均未同时成立「dispatcher 调度 + 真子进程 + store 断言」）

from __future__ import annotations

import asyncio
import sys
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.external_seat_executor import ExternalSeatExecutor
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item

FAKE_OK = (sys.executable, "-c", "print('external-done')")
FAKE_FAIL = (sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)")


class _NativeRecordingExecutor:
    """native 侧记录桩——用于断言「外部任务绝不回退到 native 引擎」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prepare_seat(self, runtime, work_item) -> None:
        self.calls.append("prepare")

    async def run_turn(self, runtime, work_item) -> SeatResult:
        self.calls.append("run")
        return SeatResult(success=True, output="native-done", metadata={})

    async def checkpoint(self, runtime) -> dict:
        return {}

    async def interrupt(self, runtime) -> None:
        self.calls.append("interrupt")

    async def shutdown(self) -> None:
        pass


def _build_dispatcher(
    tmp_path: Any, agent_commands: dict[str, tuple[str, ...]]
) -> tuple[CompanyDispatcher, WorkItemStore, _NativeRecordingExecutor]:
    store = WorkItemStore(str(tmp_path / "external-e2e.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    native = _NativeRecordingExecutor()
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=native,
        external_seat_executor=ExternalSeatExecutor(
            workspace_root=tmp_path, agent_commands=agent_commands
        ),
        config=DispatcherConfig(max_concurrent=1),
    )
    return disp, store, native


def _external_item(description: str = "external task"):
    item = create_work_item(
        title="外部执行任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        description=description,
    )
    item.metadata["selected_execution_agent"] = "opencode"
    return item


def _tick_until_not_running(disp: CompanyDispatcher, store: WorkItemStore, item_id: str) -> None:
    for _ in range(6):
        asyncio.run(disp.tick())
        saved = store.get(item_id)
        if saved is not None and saved.phase != Phase.RUNNING:
            break


class TestExternalDispatcherFullE2E:
    def test_success_runs_real_subprocess_and_writes_store(self, tmp_path: Any) -> None:
        """生产主链：dispatcher tick 认领 → 外部真子进程执行 → 产物写回 store →
        gate PASS → APPROVED（无审批员）；native 桩全程零调用。"""
        disp, store, native = _build_dispatcher(tmp_path, {"opencode": FAKE_OK})
        item = _external_item()
        store.save(item)

        _tick_until_not_running(disp, store, item.id)

        saved = store.get(item.id)
        assert saved is not None
        assert saved.runtime_metadata.get("_task_status") == "completed"
        assert "external-done" in saved.runtime_metadata.get("_last_output", "")
        assert saved.phase == Phase.APPROVED, f"实际 {saved.phase}"
        assert disp.is_claimed("worker") is False, "外部执行完成后 seat 必须释放"
        assert native.calls == [], "外部任务绝不走 native 引擎（EX-1 反面）"

    def test_failure_route_marks_needs_attention(self, tmp_path: Any) -> None:
        """失败闭环：外部子进程非零退出 → store 落 failed + 产物含 stderr → gate
        REWORK → NEEDS_ATTENTION（不静默放行，标记人工介入）。"""
        disp, store, native = _build_dispatcher(tmp_path, {"opencode": FAKE_FAIL})
        item = _external_item()
        store.save(item)

        _tick_until_not_running(disp, store, item.id)

        saved = store.get(item.id)
        assert saved is not None
        assert saved.runtime_metadata.get("_task_status") == "failed"
        assert saved.phase == Phase.NEEDS_ATTENTION, (
            f"外部失败卡必须转 NEEDS_ATTENTION 而非成功态，实际 {saved.phase}"
        )
        assert "boom" in saved.runtime_metadata.get("_last_output", "")
        assert disp.is_claimed("worker") is False
