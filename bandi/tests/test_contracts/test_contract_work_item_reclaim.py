"""功能：会话删除的级联回收——工作项不能变成永久残留。

真机缺陷：会话删了，它发起的工作项只被「中断」不删除，永久留在库里。这些项无人
查看，却每轮 tick 被全量扫描，越积越多（实测 332 条、26 个已删除会话的引用），
拖慢调度并让日志反复输出同类告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bandi.memory.maintenance import purge_orphan_work_items


@dataclass
class FakeItem:
    """功能：最小工作项替身（只需 id + metadata）。"""

    id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeStore:
    """功能：内存版工作项存储——只保留 purge 用到的两个方法。"""

    def __init__(self, items: list[FakeItem]) -> None:
        self._items = list(items)
        self.deleted: list[str] = []

    def list_all(self) -> list[FakeItem]:
        return list(self._items)

    def delete(self, item_id: str) -> None:
        self.deleted.append(item_id)
        self._items = [i for i in self._items if i.id != item_id]


def _item(item_id: str, session_id: str) -> FakeItem:
    return FakeItem(id=item_id, metadata={"origin_session_id": session_id})


def test_orphan_items_are_reclaimed() -> None:
    """功能：归属会话已删除的工作项必须被回收。"""
    store = FakeStore(
        [
            _item("a", "alive-1"),
            _item("b", "dead-1"),
            _item("c", "dead-2"),
        ]
    )
    removed = purge_orphan_work_items(store, alive_session_ids={"alive-1"})
    assert removed == 2
    assert store.deleted == ["b", "c"]
    assert [i.id for i in store.list_all()] == ["a"]


def test_alive_session_items_survive() -> None:
    """功能：破坏者视角——存活会话的工作项不能被当垃圾清掉（哪怕项目已归档）。"""
    store = FakeStore([_item("keep", "alive-1")])
    purge_orphan_work_items(store, alive_session_ids={"alive-1"})
    assert store.deleted == []
    assert len(store.list_all()) == 1


def test_items_without_origin_are_ignored() -> None:
    """功能：没有归属会话的项不动——来源不明不等于垃圾。"""
    store = FakeStore([FakeItem(id="x", metadata={})])
    purge_orphan_work_items(store, alive_session_ids=set())
    assert store.deleted == []


def test_cleanup_is_idempotent() -> None:
    """功能：重复回收不得反复计数（维护周期会多次调用）。"""
    store = FakeStore([_item("b", "dead-1")])
    assert purge_orphan_work_items(store, alive_session_ids=set()) == 1
    assert purge_orphan_work_items(store, alive_session_ids=set()) == 0


def test_scan_limit_bounds_work_data() -> None:
    """功能：单次扫描有上限——维护是后台任务，不能无界占用。"""
    store = FakeStore([_item(f"i{n}", "dead") for n in range(50)])
    removed = purge_orphan_work_items(store, alive_session_ids=set(), limit=10)
    assert removed == 10


def test_session_delete_cascades_work_items() -> None:
    """功能：删除会话的联动函数必须同时取消并删除工作项，不能只中断。"""
    import importlib
    import inspect

    # `bandi.api.app` 同时是子模块与其中的 FastAPI 实例，必须显式取模块对象
    app_module = importlib.import_module("bandi.api.app")

    source = inspect.getsource(app_module._interrupt_session_work_items)
    assert "cancel_work_item" in source or "interrupt_work_item" in source
    assert "delete" in source, "只中断不删除会让工作项永久残留"


def test_abandoned_attention_items_reach_terminal_state(tmp_path) -> None:
    """功能：超时的 needs_attention 必须真正进入终态，不能只记日志。

    真机缺陷：29 张卡长期停在 needs_attention，每轮 tick 都被重新判为「已放弃」
    并刷一条告警，但状态从未推进——于是永久自我重复。
    """
    from datetime import UTC, datetime, timedelta

    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import Phase, WorkItem, WorkItemTurnType
    from bandi.company.work_item_transition import refresh_dependents

    store = WorkItemStore(str(tmp_path / "company.db"))
    stale_at = datetime.now(UTC) - timedelta(days=30)
    item = WorkItem(
        id="stale-1",
        title="长期停留的卡",
        description="d",
        phase=Phase.NEEDS_ATTENTION,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=None,
        parent_id=None,
        children_ids=[],
        links={},
        metadata={},
        runtime_metadata={},
        updated_at=stale_at,
    )
    store.save(item)

    refresh_dependents(store)

    saved = store.get("stale-1")
    assert saved is not None
    assert saved.phase == Phase.FAILED, "超时卡必须进入终态，否则下轮又被重判一次"
    assert saved.metadata.get("abandoned_reason") == "stale_needs_attention"
