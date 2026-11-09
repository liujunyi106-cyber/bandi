# 功能：工作项身份——投影 ID/turn type/result delivery 稳定标识

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from bandi.company._utils import as_mapping as _as_mapping
from bandi.company.work_item import WorkItemTurnType

_T = TypeVar("_T")

WORK_ITEM_PROJECTION_ID_KEY = "work_item_projection_id"
WORK_ITEM_TURN_TYPE_KEY = "work_item_turn_type"
WORK_KIND_KEY = "work_kind"
DELEGATION_TURN_KIND_KEY = "delegation_turn_kind"
RESULT_DELIVERY_ID_KEY = "result_delivery_id"
SOURCE_TASK_ID_KEY = "source_task_id"
CANONICAL_TURN_ID_KEY = "canonical_turn_id"
GATE_REWORK_PROJECTION_ID_KEY = "rework_projection_id"
GATE_TARGET_PROJECTION_ID_KEY = "target_projection_id"
GATE_TARGET_PROJECTION_IDS_KEY = "target_projection_ids"
REVIEW_PROJECTION_ID_KEY = "review_projection_id"

CANONICAL_WORK_ITEM_TURN_TYPES: frozenset[str] = frozenset(t.value for t in WorkItemTurnType)

_NON_MANAGER_REVIEW_TURN_TYPES: frozenset[str] = frozenset(
    {"intake", "plan", "dispatch", "aggregate", "deliver", "self_evolution"}
)

_TURN_TYPE_ALIASES: dict[str, str] = {
    "delegate": "dispatch",
    "delegation": "dispatch",
    "delivery": "deliver",
    "follow-up": "followup",
    "follow_up": "followup",
    "synthesis": "aggregate",
    "synthesize": "aggregate",
    "self-evolution": "self_evolution",
    "self evolution": "self_evolution",
}


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _metadata(obj: Any) -> dict[str, Any]:
    """Return a copy of an object's metadata mapping, or {} for non-mappings."""
    raw = getattr(obj, "metadata", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def normalize_work_item_turn_type(value: Any, *, fallback: str = "") -> str:
    """Normalize runtime/work-item turn-kind aliases to canonical names (lenient).

    注：本函数是宽松别名归一化——未知输入原样返回（不保证 canonical）；
    需要 canonical 保证时用 :func:`canonical_work_item_turn_type_for_kind`。
    """
    normalized = _clean(value).lower() or _clean(fallback).lower()
    return _TURN_TYPE_ALIASES.get(normalized, normalized)


def canonical_work_item_turn_type_for_kind(value: Any, *, fallback: str = "execute") -> str:
    """Map a WorkItem/runtime business kind to the canonical runtime turn type."""
    normalized = normalize_work_item_turn_type(value, fallback="")
    if normalized in CANONICAL_WORK_ITEM_TURN_TYPES:
        return normalized
    fallback_normalized = normalize_work_item_turn_type(fallback, fallback="")
    if fallback_normalized in CANONICAL_WORK_ITEM_TURN_TYPES:
        return fallback_normalized
    return ""


def work_item_projection_id_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: str = "",
) -> str:
    """Read the canonical projected work-item task identity."""
    if not metadata:
        return _clean(fallback)
    value = _clean(metadata.get(WORK_ITEM_PROJECTION_ID_KEY))
    if value:
        return value
    return _clean(fallback)


def work_item_turn_type_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: str = "execute",
) -> str:
    """Read the canonical company work-item turn type."""
    if not metadata:
        return normalize_work_item_turn_type(fallback)
    for key in (
        WORK_ITEM_TURN_TYPE_KEY,
        WORK_KIND_KEY,
        DELEGATION_TURN_KIND_KEY,
    ):
        value = normalize_work_item_turn_type(metadata.get(key))
        if value:
            return value
    return normalize_work_item_turn_type(fallback)


def projection_id_for_task(task: Any) -> str:
    """Return the work-item projection identity for a projected Task."""
    metadata = _metadata(task)
    return work_item_projection_id_from_metadata(
        metadata,
        fallback=_clean(getattr(task, "id", "")),
    )


def turn_type_for_task(task: Any, *, fallback: str = "execute") -> str:
    """Return the work-item turn type for a projected Task."""
    metadata = _metadata(task)
    return work_item_turn_type_from_metadata(metadata, fallback=fallback)


def projection_id_for_work_item(item: Any) -> str:
    """Return the projection identity for a WorkItem-like object."""
    explicit_projection = _clean(getattr(item, "projection_id", ""))
    if explicit_projection:
        return explicit_projection
    metadata = _metadata(item)
    return work_item_projection_id_from_metadata(
        metadata,
        fallback=explicit_projection or _clean(getattr(item, "work_item_id", "")),
    )


def turn_type_for_work_item(item: Any, *, fallback: str = "execute") -> str:
    """Return the turn type for a WorkItem-like object."""
    metadata = _metadata(item)
    return work_item_turn_type_from_metadata(
        metadata,
        fallback=_clean(getattr(item, "kind", "")) or fallback,
    )


def canonical_turn_type_for_work_item(item: Any, *, fallback: str = "execute") -> str:
    """Return a canonical turn type for a WorkItem-like object or metadata."""
    if isinstance(item, Mapping):
        return work_item_turn_type_from_metadata(item, fallback=fallback)
    return turn_type_for_work_item(item, fallback=fallback)


def _turn_type_for_value(value: Any, *, fallback: str = "") -> str:
    if isinstance(value, Mapping):
        return work_item_turn_type_from_metadata(value, fallback=fallback)
    if hasattr(value, "metadata"):
        return canonical_turn_type_for_work_item(value, fallback=fallback or "execute")
    return canonical_work_item_turn_type_for_kind(value, fallback=fallback)


def is_delivery_turn(value_or_metadata: Any) -> bool:
    """Return True for final delivery turns, including legacy ``delivery`` alias."""
    return _turn_type_for_value(value_or_metadata, fallback="") == "deliver"


# 预留：turn 类型默认经理评审策略判定；启用时机 未来评审派发面
def is_manager_reviewable_turn(value_or_metadata: Any) -> bool:
    """Return the turn type's default manager-review policy.

    Dispatch/intake/plan are normally board-producing turns and therefore
    exempt. The executor handles the one dynamic exception — a current turn
    that produced no board mutation — before it writes the authoritative
    WorkItem phase. Downstream review plumbing follows that phase directly.
    """
    turn_type = _turn_type_for_value(value_or_metadata, fallback="")
    if not turn_type:
        return False
    return turn_type not in _NON_MANAGER_REVIEW_TURN_TYPES


def mark_work_item_projection(
    metadata: Mapping[str, Any] | None = None,
    *,
    projection_id: str = "",
    turn_type: str = "",
) -> dict[str, Any]:
    """Return metadata with canonical work-item projection keys only."""
    result = dict(metadata or {})
    projection = _clean(projection_id) or work_item_projection_id_from_metadata(result)
    turn = normalize_work_item_turn_type(turn_type) or work_item_turn_type_from_metadata(result)
    if projection:
        result[WORK_ITEM_PROJECTION_ID_KEY] = projection
    if turn:
        result[WORK_ITEM_TURN_TYPE_KEY] = turn
    return result


def mark_projected_work_item_task(
    metadata: Mapping[str, Any] | None = None,
    *,
    projection_id: str = "",
    turn_type: str = "",
) -> dict[str, Any]:
    """Return projected task/work-item metadata with canonical identity keys."""
    return mark_work_item_projection(
        metadata,
        projection_id=projection_id,
        turn_type=turn_type,
    )


def work_item_identity_payload(
    *,
    projection_id: str = "",
    turn_type: str = "",
    source: Mapping[str, Any] | None = None,
    include_empty: bool = False,
) -> dict[str, str]:
    """Build a canonical event/checkpoint/ws payload identity fragment."""
    source_meta = dict(source or {})
    projection = _clean(projection_id) or work_item_projection_id_from_metadata(
        source_meta, fallback=""
    )
    turn = normalize_work_item_turn_type(turn_type) or work_item_turn_type_from_metadata(
        source_meta, fallback=""
    )
    payload: dict[str, str] = {}
    if projection or include_empty:
        payload[WORK_ITEM_PROJECTION_ID_KEY] = projection
    if turn or include_empty:
        payload[WORK_ITEM_TURN_TYPE_KEY] = turn
    return payload


def work_item_identity_payload_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    projection_id_fallback: str = "",
    turn_type_fallback: str = "",
    include_empty: bool = False,
) -> dict[str, str]:
    """Build a canonical payload identity fragment from metadata."""
    source_meta = dict(metadata or {})
    return work_item_identity_payload(
        projection_id=work_item_projection_id_from_metadata(
            source_meta,
            fallback=projection_id_fallback,
        ),
        turn_type=work_item_turn_type_from_metadata(
            source_meta,
            fallback=turn_type_fallback,
        ),
        include_empty=include_empty,
    )


def work_item_identity_payload_for_task(
    task: Any,
    *,
    fallback_turn_type: str = "",
    include_empty: bool = False,
) -> dict[str, str]:
    """Build a canonical payload identity fragment for a Task-like object."""
    if task is None:
        return work_item_identity_payload(
            turn_type=fallback_turn_type,
            include_empty=include_empty,
        )
    return work_item_identity_payload(
        projection_id=projection_id_for_task(task),
        turn_type=turn_type_for_task(task, fallback=fallback_turn_type),
        include_empty=include_empty,
    )


def canonical_result_turn_id_for_task(
    task: Any,
    *,
    canonical_turn_id: str = "",
) -> str:
    """Return the canonical conversation turn owning a task result."""
    explicit = _clean(canonical_turn_id)
    if explicit:
        return explicit
    metadata = _metadata(task) if task is not None else {}
    runtime_metadata = _as_mapping(metadata.get("runtime_v2"))
    # runtime_v2_current_turn_id 语义上属于 runtime_v2 层，只在
    # runtime_metadata 中查找，避免顶层 metadata 误命中其他字段。
    runtime_v2_current = _clean(runtime_metadata.get("runtime_v2_current_turn_id"))
    if runtime_v2_current:
        return runtime_v2_current
    for key in (CANONICAL_TURN_ID_KEY, "conversation_turn_id", "current_turn_id"):
        for source in (runtime_metadata, metadata):
            value = _clean(source.get(key))
            if value:
                return value
    return ""


def result_delivery_id_for_task(
    task: Any,
    *,
    canonical_turn_id: str = "",
    execution_id: str = "",
    result_delivery_id: str = "",
) -> str:
    """Build one stable identity shared by every surface of a task result."""
    explicit = _clean(result_delivery_id)
    if explicit:
        return explicit
    task_id = _clean(getattr(task, "id", ""))
    turn_id = canonical_result_turn_id_for_task(
        task,
        canonical_turn_id=canonical_turn_id,
    )
    execution = _clean(execution_id)
    if not task_id or (not turn_id and not execution):
        return ""
    try:
        attempt = max(0, int(getattr(task, "retry_count", 0) or 0))
    except (TypeError, ValueError):
        attempt = 0
    identity_kind = "turn" if turn_id else "execution"
    identity_value = turn_id or execution
    return f"result:task:{task_id}:{identity_kind}:{identity_value}:attempt:{attempt}"


def result_delivery_identity_payload_for_task(
    task: Any,
    *,
    canonical_turn_id: str = "",
    execution_id: str = "",
    result_delivery_id: str = "",
) -> dict[str, str]:
    """Return structured lineage shared by persisted result projections."""
    canonical_id = canonical_result_turn_id_for_task(
        task,
        canonical_turn_id=canonical_turn_id,
    )
    delivery_id = result_delivery_id_for_task(
        task,
        canonical_turn_id=canonical_id,
        execution_id=execution_id,
        result_delivery_id=result_delivery_id,
    )
    source_task_id = _clean(getattr(task, "id", ""))
    return {
        **({RESULT_DELIVERY_ID_KEY: delivery_id} if delivery_id else {}),
        **({SOURCE_TASK_ID_KEY: source_task_id} if source_task_id else {}),
        **({CANONICAL_TURN_ID_KEY: canonical_id} if canonical_id else {}),
    }


def migrate_work_item_projection_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    projection_id_fallback: str = "",
    turn_type_fallback: str = "",
) -> tuple[dict[str, Any], bool]:
    """Normalize canonical projection metadata from canonical inputs only."""
    before = dict(metadata or {})
    result = dict(before)
    projection = work_item_projection_id_from_metadata(
        result,
        fallback=projection_id_fallback,
    )
    turn = work_item_turn_type_from_metadata(
        result,
        fallback=turn_type_fallback or "execute",
    )
    if projection and not _clean(result.get(WORK_ITEM_PROJECTION_ID_KEY)):
        result[WORK_ITEM_PROJECTION_ID_KEY] = projection
    if turn and not _clean(result.get(WORK_ITEM_TURN_TYPE_KEY)):
        result[WORK_ITEM_TURN_TYPE_KEY] = turn
    return result, result != before


def rework_projection_id_for_gate(gate: Any, *, fallback: str = "") -> str:
    """Return the gate rework target as a work-item projection identity."""
    metadata = _metadata(gate)
    raw = _clean(metadata.get(GATE_REWORK_PROJECTION_ID_KEY)) or _clean(
        getattr(gate, "rework_projection_id", "")
    )
    return raw or _clean(fallback)


def mark_gate_rework_projection(gate: _T, projection_id: str) -> _T:
    """Attach projection-only gate rework identity."""
    projection = _clean(projection_id)
    metadata = _metadata(gate)
    if projection:
        metadata[GATE_REWORK_PROJECTION_ID_KEY] = projection
    setattr(gate, "metadata", metadata)
    if hasattr(gate, "rework_projection_id"):
        setattr(gate, "rework_projection_id", projection)
    return gate


def target_projection_id_for_decision(decision: Any, *, fallback: str = "") -> str:
    """Return a gate-harness target as a work-item projection identity."""
    raw = _clean(getattr(decision, GATE_TARGET_PROJECTION_ID_KEY, ""))
    return raw or _clean(fallback)


def target_projection_ids_for_decision(decision: Any) -> list[str]:
    """Return all gate-harness targets as work-item projection identities."""
    raw_ids = list(getattr(decision, GATE_TARGET_PROJECTION_IDS_KEY, []) or [])
    if not raw_ids:
        single = target_projection_id_for_decision(decision)
        raw_ids = [single] if single else []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_ids:
        value = _clean(item)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def gate_rework_payload(
    *,
    rework_projection_id: str = "",
    target_projection_id: str = "",
    review_projection_id: str = "",
) -> dict[str, Any]:
    """Build projection-only gate/rework payload metadata."""
    rework = _clean(rework_projection_id)
    target = _clean(target_projection_id)
    review = _clean(review_projection_id)
    payload: dict[str, Any] = {}
    if rework:
        payload[GATE_REWORK_PROJECTION_ID_KEY] = rework
    if target:
        payload[GATE_TARGET_PROJECTION_ID_KEY] = target
    if review:
        payload[REVIEW_PROJECTION_ID_KEY] = review
    return payload
