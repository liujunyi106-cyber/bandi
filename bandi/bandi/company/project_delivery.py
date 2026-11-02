# 功能：项目交付显式动作——建交付里程碑卡并触发按员工反思与技能晋升（OP-27 生产触发点）
#
# 触发链：显式交付（API/交付卡终态）→ 收集项目内未反思的终态工作项 → 反思入档
# → 达阈值生成技能入暂存区待批准。本模块是 `record_project_reflections` 的

from __future__ import annotations

from typing import Any

from bandi.company.employee_evolution import EmployeeEvolutionManager
from bandi.company.phase import Phase, is_terminal
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item

# 交付卡标识：metadata.kind 用于把交付里程碑与普通工作项区分开
PROJECT_DELIVERY_KIND = "project_delivery"

# 交付卡上的幂等标记与覆盖清单（重复交付/重复触发的唯一去重依据）
REFLECTION_RECORDED_KEY = "reflection_recorded"
REFLECTED_ITEM_IDS_KEY = "reflected_item_ids"

STATUS_RECORDED = "recorded"
STATUS_ALREADY_RECORDED = "already_recorded"
STATUS_ALREADY_DELIVERED = "already_delivered"
STATUS_NO_WORK_ITEMS = "no_work_items"


def project_id_of(item: WorkItem) -> str:
    """功能：解析工作项的项目归属（属性优先，回落 metadata）。"""
    direct = str(getattr(item, "project_id", "") or "").strip()
    if direct:
        return direct
    return str((getattr(item, "metadata", None) or {}).get("project_id") or "").strip()


def _is_reflectable(item: WorkItem) -> bool:
    """功能：可反思工作项 = 非交付卡 + 终态 + 有员工归属。"""
    if item.turn_type is WorkItemTurnType.DELIVER:
        return False
    if not is_terminal(item.phase):
        return False
    assignment = dict((item.metadata or {}).get("employee_assignment") or {})
    return bool(str(assignment.get("employee_id") or "").strip())


def reflected_item_ids(store: WorkItemStore, project_id: str) -> set[str]:
    """功能：汇总既有交付卡已覆盖的工作项 id（每个工作项至多被反思一次）。"""
    covered: set[str] = set()
    for item in store.list_all():
        if item.turn_type is not WorkItemTurnType.DELIVER:
            continue
        metadata = dict(item.metadata or {})
        if str(metadata.get("kind") or "") != PROJECT_DELIVERY_KIND:
            continue
        if project_id_of(item) != project_id:
            continue
        for value in list(metadata.get(REFLECTED_ITEM_IDS_KEY) or []):
            text = str(value).strip()
            if text:
                covered.add(text)
    return covered


def collect_pending_items(store: WorkItemStore, project_id: str) -> list[WorkItem]:
    """功能：收集该项目本次交付应覆盖的工作项（已交付过的排除）。"""
    covered = reflected_item_ids(store, project_id)
    return [
        item
        for item in store.list_all()
        if project_id_of(item) == project_id and _is_reflectable(item) and item.id not in covered
    ]


def project_item_count(store: WorkItemStore, project_id: str) -> int:
    """功能：项目下工作项总数，**不含交付卡**（用于区分「空项目」与「已全部交付」）。

    交付卡不算工作项：项目里若只有一张从未成功反思的交付卡，应报
    `no_work_items` 而非 `already_delivered`（后者会误导用户以为已交付过）。
    """
    return len(
        [
            item
            for item in store.list_all()
            if project_id_of(item) == project_id and item.turn_type is not WorkItemTurnType.DELIVER
        ]
    )


def _mark_recorded(store: WorkItemStore, item_id: str, covered_ids: list[str]) -> None:
    """功能：在交付卡上落「已反思」标记与覆盖清单（重读后再写，避免覆盖并发元数据）。"""
    current = store.get(item_id)
    if current is None:
        return
    metadata = dict(current.metadata or {})
    merged = {
        str(value).strip()
        for value in list(metadata.get(REFLECTED_ITEM_IDS_KEY) or []) + list(covered_ids)
        if str(value).strip()
    }
    metadata[REFLECTED_ITEM_IDS_KEY] = sorted(merged)
    metadata[REFLECTION_RECORDED_KEY] = True
    current.metadata = metadata
    store.save(current)


def _empty_result(status: str, project_id: str, item: WorkItem | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "project_id": project_id,
        "item_ids": [],
        "delivery_item_id": item.id if item is not None else "",
        "reflections": [],
    }


def _record_reflections(
    *,
    store: WorkItemStore,
    evolution: EmployeeEvolutionManager,
    delivery: WorkItem,
    items: list[WorkItem],
    partial: bool,
    feedback: dict[str, Any] | None,
    evaluation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    reflections = evolution.record_project_reflections(
        delivery, items, partial=partial, feedback=feedback, evaluation=evaluation
    )
    _mark_recorded(store, delivery.id, [item.id for item in items])
    return reflections


def deliver_project(
    *,
    store: WorkItemStore,
    evolution: EmployeeEvolutionManager,
    project_id: str,
    feedback: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    partial: bool = False,
    title: str = "",
) -> dict[str, Any]:
    """功能：显式项目交付——建交付里程碑卡并触发按员工反思与技能晋升（幂等）。

    无新工作项时不建卡、不重复计数（防 `reflection_count` 被重复交付推高而提前晋升）。
    """
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id 不能为空")
    items = collect_pending_items(store, pid)
    if not items:
        status = (
            STATUS_NO_WORK_ITEMS
            if project_item_count(store, pid) == 0
            else STATUS_ALREADY_DELIVERED
        )
        return _empty_result(status, pid)
    delivery = create_work_item(
        title=title or f"项目交付：{pid}",
        turn_type=WorkItemTurnType.DELIVER,
        description=f"项目 {pid} 交付里程碑（覆盖 {len(items)} 个工作项）",
        # 注意：此处**不预写** reflected_item_ids——清单由 `_mark_recorded` 在
        # 反思成功后落卡。若预写而反思中途失败，卡上的清单会把这批工作项
        # 永久排除出后续交付（静默丢失反思）。
        metadata={
            "project_id": pid,
            "kind": PROJECT_DELIVERY_KIND,
        },
        phase=Phase.APPROVED,
    )
    store.save(delivery)
    reflections = _record_reflections(
        store=store,
        evolution=evolution,
        delivery=delivery,
        items=items,
        partial=partial,
        feedback=feedback,
        evaluation=evaluation,
    )
    return {
        "status": STATUS_RECORDED,
        "project_id": pid,
        "item_ids": [item.id for item in items],
        "delivery_item_id": delivery.id,
        "reflections": reflections,
    }


def maybe_record_delivery_reflection(
    *, store: WorkItemStore, evolution: EmployeeEvolutionManager, item: WorkItem
) -> dict[str, Any] | None:
    """功能：交付卡进入终态时触发反思（非交付卡/未终态返回 None；同卡幂等）。

    幂等依据取**库内**快照（调用方常持有过期对象，如调度链路传入的完成态快照）。
    """
    if item.turn_type is not WorkItemTurnType.DELIVER:
        return None
    stored = store.get(item.id) or item
    if not is_terminal(stored.phase):
        return None
    pid = project_id_of(stored)
    if not pid:
        return None
    if dict(stored.metadata or {}).get(REFLECTION_RECORDED_KEY):
        return _empty_result(STATUS_ALREADY_RECORDED, pid, item)
    items = collect_pending_items(store, pid)
    if not items:
        _mark_recorded(store, item.id, [])
        return _empty_result(STATUS_ALREADY_DELIVERED, pid, item)
    reflections = _record_reflections(
        store=store,
        evolution=evolution,
        delivery=item,
        items=items,
        partial=False,
        feedback=None,
        evaluation=None,
    )
    return {
        "status": STATUS_RECORDED,
        "project_id": pid,
        "item_ids": [pending.id for pending in items],
        "delivery_item_id": item.id,
        "reflections": reflections,
    }
