"""契约族：停滞会话巡检接入 dispatcher tick（B6-A1）。

行为不变式：
1. 聚焦超时（≥ `STUCK_FOCUS_MINUTES`）的会话在 tick 报告中显式告警（errors 条目）；
2. 新鲜聚焦零告警；巡检异常不阻断调度主链（tick 照常返回报告）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.store import WorkItemStore


@pytest.fixture()
def dispatcher(tmp_path: Any) -> Any:
    store = WorkItemStore(str(tmp_path / "dispatch.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
    )


def _age_focus(session_id: str, minutes: int) -> None:
    """功能：把某会话的聚焦起始时间回溯 N 分钟（模拟停滞）。"""
    from bandi.company import phase_hooks

    with phase_hooks._STATE_LOCK:
        phase_hooks._focus_start[session_id] = datetime.now(UTC) - timedelta(minutes=minutes)


class TestStuckFocusTickWiring:
    def test_tick_reports_stuck_focused_session(self, dispatcher: Any) -> None:
        """预期：注入超时聚焦 → tick 报告出现停滞告警（日志/告警出现）。"""
        import asyncio

        from bandi.company.phase_hooks import clear_session_focus, set_session_focus

        set_session_focus("sess-stuck", "w-stuck")
        _age_focus("sess-stuck", 11)
        try:
            report = asyncio.run(dispatcher.tick())
        finally:
            clear_session_focus("sess-stuck")
        assert any("stuck" in str(e).lower() for e in report.errors), (
            f"tick 报告应含停滞告警：{report.errors}"
        )
        assert any("sess-stuck" in str(e) for e in report.errors)

    def test_tick_silent_when_focus_fresh(self, dispatcher: Any) -> None:
        import asyncio

        from bandi.company.phase_hooks import clear_session_focus, set_session_focus

        set_session_focus("sess-fresh", "w1")
        try:
            report = asyncio.run(dispatcher.tick())
        finally:
            clear_session_focus("sess-fresh")
        assert not any("stuck" in str(e).lower() for e in report.errors)

    def test_check_failure_does_not_block_tick(
        self, dispatcher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """破坏者：巡检自身异常不得炸 tick（调度主链必须照常返回报告）。"""
        import asyncio

        from bandi.company import phase_hooks

        def _boom() -> list[Any]:
            raise RuntimeError("check down")

        monkeypatch.setattr(phase_hooks, "check_stuck_focused_sessions", _boom)
        report = asyncio.run(dispatcher.tick())
        assert report.tick_id, "巡检异常后 tick 仍须正常完成"
