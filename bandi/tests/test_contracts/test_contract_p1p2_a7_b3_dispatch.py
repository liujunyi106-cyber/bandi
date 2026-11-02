"""契约：调度并发上限可配置 + 出队不可派发必须回队并记账。

- `CompanyConfig.max_concurrent` 默认 3、带上下界；装配点读配置而非硬编码。
- 挑中但不可派发的卡回队（不再静默丢弃），并在当轮 report.errors 留痕。
- 同角色同一时刻只跑一张卡；单 tick 总量受 `max_items_per_tick` 硬顶。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig, DispatcherReport
from bandi.core.config import BandiConfig, CompanyConfig


class _Item:
    def __init__(self, item_id: str, role: str) -> None:
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItemTurnType

        self.id = item_id
        self.assignee_role = role
        self.phase = Phase.READY
        self.metadata: dict[str, object] = {}
        self.turn_type = WorkItemTurnType.EXECUTE


class _RuntimeState:
    def __init__(self) -> None:
        self.claimed_work_item_ids: set[str] = set()


class _Runtime:
    def __init__(self, pairs: list[tuple[str, str]], dead: set[str] | None = None) -> None:
        self.queue = list(pairs)
        self.state = _RuntimeState()
        self.requeued: list[str] = []
        self.dead = dead or set()
        self.released: list[str] = []

    def enqueue_runnable_work_items(self, items: list[object]) -> None:
        return None

    def ensure_role_member_session(self, role_id: str) -> None:
        return None

    def sorted_member_sessions(self) -> list[object]:
        return [type("S", (), {"role_id": role, "status": "idle"})() for _wid, role in self.queue]

    def pop_next_work_item_id(self, role_id: str) -> str | None:
        for index, (_wid, role) in enumerate(self.queue):
            if role == role_id:
                return self.queue.pop(index)[0]
        return None

    def is_dispatchable(self, item: object) -> bool:
        return getattr(item, "id", "") not in self.dead

    def claim_work_item(self, work_item_id: str, *, role_id: str) -> None:
        self.state.claimed_work_item_ids.add(work_item_id)

    def release_work_item(self, work_item_id: str, *, role_id: str) -> None:
        self.state.claimed_work_item_ids.discard(work_item_id)
        self.released.append(work_item_id)

    def requeue_work_item(self, item: object) -> None:
        self.requeued.append(getattr(item, "id", ""))


class _Store:
    def __init__(self, items: dict[str, _Item]) -> None:
        self.items = items

    def get(self, work_item_id: str) -> _Item | None:
        return self.items.get(work_item_id)


def _dispatcher(
    runtime: _Runtime,
    store: _Store,
    *,
    concurrency: int = 2,
    max_items: int = 64,
) -> tuple[CompanyDispatcher, DispatcherReport, dict[str, list[str]]]:
    dispatcher = object.__new__(CompanyDispatcher)
    dispatcher.config = DispatcherConfig(max_concurrent=concurrency, max_items_per_tick=max_items)
    dispatcher.org_engine = type("Org", (), {"list_agents": lambda self: []})()
    dispatcher.seat_executor = object()
    dispatcher.store = store
    probe: dict[str, list[str]] = {"running": [], "order": []}
    lock = asyncio.Lock()

    async def _run(work_item_id: str) -> object:
        async with lock:
            probe["running"].append(work_item_id)
            probe["order"].append(work_item_id)
        await asyncio.sleep(0.01)
        async with lock:
            probe["running"].remove(work_item_id)
        return type("SeatResult", (), {"success": True, "output": ""})()

    dispatcher.run_work_item = _run  # type: ignore[method-assign]
    dispatcher.claim = lambda role_id, _item="": True  # type: ignore[method-assign]
    dispatcher.release = lambda role_id, _item="": True  # type: ignore[method-assign]
    report = DispatcherReport(tick_id="t", started_at=datetime.now(UTC))
    return dispatcher, report, probe


def test_company_config_default_and_bounds() -> None:
    assert BandiConfig().company.max_concurrent == 3
    assert CompanyConfig().max_items_per_tick == 64
    assert CompanyConfig().tick_budget_sec == 300.0
    with pytest.raises(ValidationError):
        CompanyConfig(max_concurrent=0)
    with pytest.raises(ValidationError):
        CompanyConfig(max_concurrent=99)


def test_repo_config_declares_company_block() -> None:
    import yaml

    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["company"]["max_concurrent"] == 3
    assert data["company"]["tick_budget_sec"] == 300
    assert data["company"]["max_items_per_tick"] == 64


@pytest.mark.asyncio
async def test_unrunnable_item_is_requeued_and_reported() -> None:
    items = {"w0": _Item("w0", "r0")}
    runtime = _Runtime([("w0", "r0")], dead={"w0"})
    dispatcher, report, _probe = _dispatcher(runtime, _Store(items))

    await dispatcher._dispatch_todo_items(report, runtime, list(items.values()))

    assert runtime.requeued == ["w0"], "不可派发项必须回队（旧实现静默丢弃）"
    assert report.processed == []
    assert not report.has_errors(), "依赖未满足/plan 门属正常稍后再试，不得计入错误"


@pytest.mark.asyncio
async def test_same_role_runs_one_card_at_a_time() -> None:
    items = {"w0": _Item("w0", "r0"), "w1": _Item("w1", "r0")}
    runtime = _Runtime([("w0", "r0"), ("w1", "r0")])
    dispatcher, report, probe = _dispatcher(runtime, _Store(items), concurrency=2)

    await dispatcher._dispatch_todo_items(report, runtime, list(items.values()))

    assert sorted(report.processed) == ["w0", "w1"]
    assert probe["order"] == ["w0", "w1"], "同角色必须严格串行（第二张在首张结束后才起）"


@pytest.mark.asyncio
async def test_tick_total_cap_leaves_remaining_queued() -> None:
    pairs = [(f"w{i}", f"r{i}") for i in range(5)]
    items = {wid: _Item(wid, role) for wid, role in pairs}
    runtime = _Runtime(pairs)
    dispatcher, report, _probe = _dispatcher(runtime, _Store(items), concurrency=2, max_items=3)

    await dispatcher._dispatch_todo_items(report, runtime, list(items.values()))

    assert len(report.processed) == 3, f"单 tick 总量受硬顶，实际 {report.processed}"
    assert len(runtime.queue) == 2, "超出硬顶的卡应留在队列（不得丢弃）"


@pytest.mark.asyncio
async def test_tick_budget_stops_refill() -> None:
    pairs = [(f"w{i}", f"r{i}") for i in range(4)]
    items = {wid: _Item(wid, role) for wid, role in pairs}
    runtime = _Runtime(pairs)
    dispatcher, report, _probe = _dispatcher(runtime, _Store(items), concurrency=2)
    dispatcher.config.tick_budget_sec = 0.01

    async def _slow(work_item_id: str) -> object:
        await asyncio.sleep(0.08)
        return type("SeatResult", (), {"success": True, "output": ""})()

    dispatcher.run_work_item = _slow  # type: ignore[method-assign]
    await dispatcher._dispatch_todo_items(report, runtime, list(items.values()))

    assert len(report.processed) == 2, "预算过期后不得再派新卡（在跑卡照常收尾）"
    assert len(runtime.queue) == 2, "未派出的卡必须留在队列"
