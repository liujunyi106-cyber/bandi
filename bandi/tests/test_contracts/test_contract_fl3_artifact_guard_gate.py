# 功能：契约——产物守卫接入公司完成判定：任务声明 expected_artifacts 时
# gate PASS 前校验产物可读回，缺失转 REWORK；未声明/无根不误伤（零回归）。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.memory.session.store import SessionStore


class _StubSeatExecutor:
    def __init__(self, result: SeatResult) -> None:
        self._result = result

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        pass

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        return self._result

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _dispatcher(tmp_path: Path, result: SeatResult) -> CompanyDispatcher:
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
        seat_executor=_StubSeatExecutor(result),
        config=DispatcherConfig(max_concurrent=2),
    )


def _run_and_tick(
    tmp_path: Path, metadata: dict[str, Any], monkeypatch: Any, root: Path | None
) -> tuple[CompanyDispatcher, Any]:
    monkeypatch.setattr(
        "bandi.company.seat_executor._resolve_project_root_for",
        lambda pid: str(root) if root is not None else None,
    )
    sessions = SessionStore(str(tmp_path / "bandi.db"))
    sessions.create_session("sess-fl3")
    result = SeatResult(
        success=True,
        output="报告已完成，产物已写入。",
        metadata={"terminal_reason": "COMPLETED", "total_tokens": 1, "turn_index": 1},
    )
    disp = _dispatcher(tmp_path, result)
    item = create_work_item(
        title="FL-3 产物守卫任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata={"origin_session_id": "sess-fl3", **metadata},
    )
    disp.store.save(item)
    assert disp.claim("worker")
    asyncio.run(disp.run_work_item(item.id))
    asyncio.run(disp.tick())
    return disp, disp.store.get(item.id)


def _reason(item: Any) -> str:
    return str(
        item.metadata.get("last_transition_reason", "")
        or item.runtime_metadata.get("last_transition_reason", "")
    )


class TestArtifactGuardGate:
    def test_missing_declared_artifact_blocks_pass(self, tmp_path: Path, monkeypatch: Any) -> None:
        """破坏者：声称完成但声明的产物缺失 → 不得 PASS，转 REWORK 等处理。"""
        disp, item = _run_and_tick(
            tmp_path,
            {"project_id": "p1", "expected_artifacts": ["report.md"]},
            monkeypatch,
            root=tmp_path,
        )
        assert item.phase == Phase.NEEDS_ATTENTION, (
            f"缺失产物必须拦下 PASS（实际 {item.phase.value}）"
        )
        reason = _reason(item)
        assert "rework" in reason.lower() and "artifactguard" in reason.lower(), (
            f"转态理由须含 rework + ArtifactGuard（实际 {reason!r}）"
        )

    def test_present_artifact_passes(self, tmp_path: Path, monkeypatch: Any) -> None:
        (tmp_path / "report.md").write_text("ok", encoding="utf-8")
        disp, item = _run_and_tick(
            tmp_path,
            {"project_id": "p1", "expected_artifacts": ["report.md"]},
            monkeypatch,
            root=tmp_path,
        )
        assert item.phase == Phase.APPROVED, f"产物可读回必须正常 PASS（实际 {item.phase.value}）"
        assert "pass" in _reason(item).lower()

    def test_no_declaration_zero_regression(self, tmp_path: Path, monkeypatch: Any) -> None:
        """对照组：未声明 expected_artifacts 的卡行为不变。"""
        disp, item = _run_and_tick(tmp_path, {"project_id": "p1"}, monkeypatch, root=tmp_path)
        assert item.phase == Phase.APPROVED

    def test_no_project_root_skips_guard(self, tmp_path: Path, monkeypatch: Any) -> None:
        """对照组：无项目根时守卫跳过（不误伤）。"""
        disp, item = _run_and_tick(
            tmp_path,
            {"project_id": "p1", "expected_artifacts": ["report.md"]},
            monkeypatch,
            root=None,
        )
        assert item.phase == Phase.APPROVED
