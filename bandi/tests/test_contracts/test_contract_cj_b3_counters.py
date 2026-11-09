# 功能：契约——事件丢弃 / 维度漏传计数接入可观测出口
#
# `core/event_bus.py::persist_dropped` 仅属性自计数（无进程级出口）；
# `memory/long_term.py::scope_strict_misses()` 无生产消费方——两处「不静默
# 丢弃」承诺实际无可观测出口（值班看不到）。
#
# 本契约钉：
# 1. 进程级计数注册表（跨 bus/store 实例聚合）+ 读快照/重置；
# 2. 事件队列满丢弃 → 注册表 `event_bus.persist_dropped` 增加；
# 3. long_term 漏传维度 → 注册表 `memory.long_term.scope_strict_miss.*` 增加；
# 4. 维护晋升（合法镜像作用域写）净增为 0（不得污染漏传观测）；
# 5. 既有 /health 出口暴露 counters（字段名稳定）。
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from bandi.observability.counters import (
    COUNTER_EVENT_BUS_PERSIST_DROPPED,
    increment,
    reset,
    scope_miss_counter_name,
    snapshot,
)


@pytest.fixture(autouse=True)
def _isolate_counters() -> None:
    reset()


class TestCounterRegistry:
    """注册表基本契约：进程级聚合、快照隔离、可重置。"""

    def test_increment_and_snapshot(self) -> None:
        assert snapshot() == {}
        assert increment("x") == 1
        assert increment("x") == 2
        assert increment("y", 3) == 3
        assert snapshot() == {"x": 2, "y": 3}

    def test_snapshot_is_isolated_copy(self) -> None:
        increment("x")
        snap = snapshot()
        snap["x"] = 999
        assert snapshot()["x"] == 1, "快照必须是副本（调用方改不动内部状态）"

    def test_reset_clears_all(self) -> None:
        increment("x")
        increment("y", 5)
        reset()
        assert snapshot() == {}

    def test_scope_miss_counter_name_is_stable(self) -> None:
        assert scope_miss_counter_name("project_id") == (
            "memory.long_term.scope_strict_miss.project_id"
        ), "计数键是观测契约，不得随意变更"


class TestLongTermScopeMissExport:
    """long_term 漏传维度 → 注册表计数（与 scope_strict_misses 单源同步）。"""

    def test_store_without_dims_publishes_counter(self, tmp_path: Path) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        memory.store(MemoryItem(item_id="m1", content="无维度写入"))

        snap = snapshot()
        assert snap.get(scope_miss_counter_name("project_id"), 0) >= 1, (
            f"漏传 project_id 必须发布进程级计数：{snap}"
        )
        assert snap.get(scope_miss_counter_name("employee_id"), 0) >= 1

    def test_scoped_store_does_not_publish(self, tmp_path: Path) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        memory.store(
            MemoryItem(item_id="m1", content="带维度写入"), project_id="p", employee_id="e"
        )

        snap = snapshot()
        assert snap.get(scope_miss_counter_name("project_id"), 0) == 0
        assert snap.get(scope_miss_counter_name("employee_id"), 0) == 0

    def test_maintenance_promotion_keeps_counter_net_zero(self, tmp_path: Path) -> None:
        """维护晋升是合法镜像作用域写（契约同源）：净增必须为 0。"""
        from bandi.memory.episodic import Episode, EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                summary="全局经验",
                episode_id="global",
                session_id="s1",
                importance=0.3,
                recall_count=2,
            )
        )
        reset()
        promoted = promote_notable_episodes(episodic, long_term)

        assert promoted == ["global"]
        snap = snapshot()
        assert snap.get(scope_miss_counter_name("project_id"), 0) == 0, (
            f"维护晋升的预扣/归还必须同步观测计数（不得净增漏传）：{snap}"
        )
        assert snap.get(scope_miss_counter_name("employee_id"), 0) == 0

    def test_scope_clause_strict_publishes_counter(self) -> None:
        """残余决策 (a)：`_scope_clause(strict=True)` 保留为显式读语义
        其漏传计数同样接入观测出口（不再只是模块私有 dict）。"""
        from bandi.memory.long_term import LongTermMemory

        LongTermMemory._scope_clause(None, None, strict=True)

        snap = snapshot()
        assert snap.get(scope_miss_counter_name("project_id"), 0) == 1
        assert snap.get(scope_miss_counter_name("employee_id"), 0) == 1


class TestEventBusDropExport:
    """事件队列满丢弃 → 注册表计数（bus 实例多也进程级聚合）。"""

    @pytest.mark.asyncio
    async def test_queue_full_publishes_counter(self, tmp_path: Path, monkeypatch: Any) -> None:
        from bandi.core.event_bus import EventBus
        from bandi.core.events import Event, EventType

        bus = EventBus(persist_path=tmp_path / "events.jsonl")
        bus.enable_persistence()
        release = threading.Event()
        monkeypatch.setattr(bus, "_persist_one", lambda record, *args: release.wait(5))
        bus._persist_queue = queue.Queue(maxsize=2)  # type: ignore[assignment]

        try:
            for i in range(6):
                await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))

            assert bus.persist_dropped >= 1, "队列满必须丢弃并自计数"
            assert snapshot().get(COUNTER_EVENT_BUS_PERSIST_DROPPED, 0) >= 1, (
                f"丢弃必须发布进程级计数：{snapshot()}"
            )
        finally:
            release.set()
            bus.disable_persistence()
            await bus.close()

    @pytest.mark.asyncio
    async def test_no_drop_no_counter(self, tmp_path: Path) -> None:
        from bandi.core.event_bus import EventBus
        from bandi.core.events import Event, EventType

        bus = EventBus(persist_path=tmp_path / "events.jsonl")
        bus.enable_persistence()
        for i in range(3):
            await bus.emit(Event(type=EventType.STATE_CHANGED, data={"i": i}))
        await bus.drain()
        await bus.close()

        assert snapshot().get(COUNTER_EVENT_BUS_PERSIST_DROPPED, 0) == 0


class TestHealthExport:
    """既有 HTTP 出口：/health 暴露 counters（字段名稳定、端点不炸）。"""

    def test_health_exposes_counters(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sys

        from fastapi.testclient import TestClient

        import bandi.api.app  # noqa: F401 — 确保模块已加载

        api_mod = sys.modules["bandi.api.app"]
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(api_mod, "_API_KEY", "test")
        monkeypatch.setattr(api_mod, "_account_store", None)
        monkeypatch.setattr(api_mod, "_store", None)
        monkeypatch.setattr(api_mod, "_db_path", tmp_path / "bandi.db")

        increment("contract.probe", 7)
        client = TestClient(api_mod.app)
        resp = client.get("/health")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "ok", "liveness 语义不得回退"
        counters = body.get("counters")
        assert isinstance(counters, dict), f"/health 必须暴露 counters 字段：{body}"
        assert counters.get("contract.probe") == 7, f"计数必须以注册表快照呈现：{counters}"

    def test_health_counters_include_production_sources(self, tmp_path: Path) -> None:
        """生产来源键名稳定：漏传与丢弃都能经 HTTP 出口读出。"""
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        increment(COUNTER_EVENT_BUS_PERSIST_DROPPED)
        LongTermMemory(tmp_path / "lt.db").store(MemoryItem(item_id="m1", content="无维度"))

        snap = snapshot()
        assert snap[COUNTER_EVENT_BUS_PERSIST_DROPPED] == 1
        assert snap[scope_miss_counter_name("project_id")] == 1
        assert snap[scope_miss_counter_name("employee_id")] == 1


class TestMaintenanceIntervalGuard:
    """interval_hours<=0 视为关闭维护（防每 tick 全量维护）。"""

    def test_zero_interval_skips_maintenance_wiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        import bandi.api.app  # noqa: F401 — 确保模块已加载
        from bandi.core.config import BandiConfig, MemoryMaintenanceConfig

        api_mod = sys.modules["bandi.api.app"]
        cfg = BandiConfig(
            memory_maintenance=MemoryMaintenanceConfig(enabled=True, interval_hours=0)
        )
        monkeypatch.setattr(BandiConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(api_mod, "_task_scheduler", None)
        monkeypatch.setattr(api_mod, "_task_store", object())
        monkeypatch.setattr(api_mod, "_task_runner", lambda task: None)

        wired: list[dict[str, Any]] = []

        def _spy_callable(**kwargs: Any) -> Any:
            wired.append(kwargs)
            return lambda: None

        monkeypatch.setattr("bandi.memory.maintenance.default_maintenance_callable", _spy_callable)

        scheduler = api_mod._get_task_scheduler()

        assert wired == [], "interval_hours=0 不得注入维护 callable"
        assert scheduler._config.maintenance_callable is None
        assert scheduler._config.maintenance_enabled is False, "0 间隔必须显式视为关闭维护"
