# 功能：契约——公司调度硬化四件（wakeup 全局上限 / rework 计入每 tick 预算 /
# 并发 tick 串行化 / 常驻调度 CLI 入口）。
# 钉行为：wakeup 达到全局并发上限必须跳过（防旁路无上限起后台 run）；
# 并发两次 tick 不得重叠执行（进程内锁；跨进程由 CAS 兜底）；
# `bandi company run` 提供常驻调度生产入口（run_forever + running 回调）。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig, DispatcherReport
from bandi.company.heartbeat import HeartbeatScheduler
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.seat_executor import SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item

# ── wakeup 全局并发上限 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wakeup_respects_global_concurrency_cap() -> None:
    scheduler = HeartbeatScheduler(
        store=object(),  # type: ignore[arg-type]
        org_engine=object(),  # type: ignore[arg-type]
        dispatcher=object(),  # type: ignore[arg-type]
        max_concurrent_runs=1,
    )

    async def _blocking(agent_id: str) -> None:
        await asyncio.sleep(30)

    scheduler._run_agent_heartbeat = _blocking  # type: ignore[method-assign]
    scheduler.wakeup("a")
    scheduler.wakeup("b")

    assert len(scheduler._active_runs) == 1, (
        "wakeup 达到全局并发上限后必须跳过（防旁路无上限起后台 run）"
    )
    for task in scheduler._active_runs.values():
        task.cancel()
    await asyncio.gather(*scheduler._active_runs.values(), return_exceptions=True)


# ── rework 阶段遵守每 tick 预算 ──────────────────────────────────────


class _OkSeatExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prepare_seat(self, runtime: Any, _item: WorkItem) -> None:
        return None

    async def run_turn(self, runtime: Any, item: WorkItem) -> SeatResult:
        self.calls.append(str(item.id))
        return SeatResult(success=True, output="ok")

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        return None


def _mk_rework_item(store: WorkItemStore, assignee: str = "worker") -> WorkItem:
    item = create_work_item(title="rework 卡", turn_type=WorkItemTurnType.EXECUTE)
    item.assignee_role = assignee
    item.phase = Phase.READY_FOR_REWORK
    store.save(item)
    return store.get(item.id)  # type: ignore[return-value]


def test_rework_respects_per_tick_claim_budget(tmp_path: Path, monkeypatch) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    for _ in range(3):
        _mk_rework_item(store)
    seat = _OkSeatExecutor()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        seat_executor=seat,
        config=DispatcherConfig(max_concurrent=2, enable_auto_process=True),
    )
    dispatcher.task_preparer = None  # 聚焦预算守卫
    monkeypatch.setattr(dispatcher, "claim", lambda _role, _item="": True)
    monkeypatch.setattr(dispatcher, "release", lambda _role, _item="": True)

    asyncio.run(dispatcher.tick())

    assert len(seat.calls) == 2, (
        f"rework 阶段最多处理 max_concurrent 张卡（实际处理 {len(seat.calls)} 张）"
    )


# ── 并发 tick 串行化 ─────────────────────────────────────────────────


def test_concurrent_ticks_are_serialized(tmp_path: Path, monkeypatch) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=True),
    )
    active = 0
    peak = 0

    async def _slow_dispatch(report: DispatcherReport, runtime: Any) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    monkeypatch.setattr(dispatcher, "_tick_work_items", _slow_dispatch)

    async def _run() -> None:
        await asyncio.gather(dispatcher.tick(), dispatcher.tick())

    asyncio.run(_run())

    assert peak == 1, "并发 tick 必须进程内串行（重叠执行会造成同进程双 Dispatch）"


# ── 常驻调度 CLI 入口 ────────────────────────────────────────────────


def test_cli_company_run_invokes_run_forever(monkeypatch) -> None:
    from typer.testing import CliRunner

    import bandi.cli.company as company_mod

    class _FakeDispatcher:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run_forever(
            self,
            running: Any,
            *,
            wait_timeout_sec: float = 30.0,
            loop_name: str = "CompanyDispatcher",
        ) -> None:
            self.calls.append({"running": running, "wait_timeout_sec": wait_timeout_sec})

    fake = _FakeDispatcher()
    monkeypatch.setattr(company_mod, "_dispatcher", lambda: (fake, None))

    result = CliRunner().invoke(company_mod.company_app, ["run", "--wait-timeout", "5"])

    assert result.exit_code == 0, result.output
    assert fake.calls, "company run 必须调用 run_forever（常驻调度入口）"
    assert fake.calls[0]["wait_timeout_sec"] == 5.0
    assert fake.calls[0]["running"]() is True, "running 回调初始必须为 True（Ctrl+C 才停）"
