# 功能：元数据所有权——所有权校验（字段矩阵数据见 metadata_field_matrix.py）

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from bandi.company.metadata_field_matrix import (
    _EXECUTION_COPY_KEYS_SORTED,
    EXECUTION_COPY_KEYS,
    METADATA_FIELD_SPECS,
    RUNTIME_TASK_OWNED_KEYS,
    WORK_ITEM_OWNED_KEYS,
    MetadataFieldSpec,
    MetadataOwner,
    MetadataOwnershipIssue,
)

logger = logging.getLogger(__name__)


def metadata_owner_for_key(key: str) -> MetadataOwner | None:
    spec = METADATA_FIELD_SPECS.get(str(key or "").strip())
    return spec.owner if spec is not None else None


def metadata_spec_for_key(key: str) -> MetadataFieldSpec | None:
    return METADATA_FIELD_SPECS.get(str(key or "").strip())


def is_work_item_owned_key(key: str) -> bool:
    return metadata_owner_for_key(key) == MetadataOwner.WORK_ITEM


def is_runtime_task_owned_key(key: str) -> bool:
    return metadata_owner_for_key(key) == MetadataOwner.RUNTIME_TASK


def is_execution_copy_key(key: str) -> bool:
    return str(key or "").strip() in EXECUTION_COPY_KEYS


def get_owner(key: str, *, default: MetadataOwner = MetadataOwner.RUNTIME_TASK) -> MetadataOwner:
    """Query the owning metadata scope for a key.

    注意：未知/未登记 key 回退 ``default``（默认 RUNTIME_TASK），与
    func:`metadata_owner_for_key`（未知返回 None）语义不同——需要严格
    校验时请用后者。
    """
    return metadata_owner_for_key(key) or default


def get_spec(key: str) -> MetadataFieldSpec | None:
    """Query the field spec for a key (normalized, unknown keys return None)."""
    return metadata_spec_for_key(key)


def check_ownership(key: str, location: str) -> MetadataOwnershipIssue | None:
    spec = METADATA_FIELD_SPECS.get(key)
    if spec is None:
        return None
    if location not in spec.allowed_locations:
        return MetadataOwnershipIssue(
            code="ownership_violation",
            severity="warning",
            key=key,
            owner=spec.owner.value,
            message=f"Key '{key}' owned by {spec.owner.value}, but accessed from '{location}'",
        )
    return None


def list_keys_by_owner(owner: MetadataOwner) -> list[str]:
    return [k for k, s in METADATA_FIELD_SPECS.items() if s.owner == owner]


def supports_legacy_task_fallback(key: str) -> bool:
    spec = metadata_spec_for_key(key)
    return bool(spec and spec.legacy_fallback)


def validate_metadata(metadata: dict[str, Any], location: str) -> list[MetadataOwnershipIssue]:
    """Validate single-scope location compliance for a metadata dict.

    注意：与 :func:`validate_metadata_ownership` 职责不同——本函数只校验
    key 的 owner 是否允许出现在给定 location；后者校验 WorkItem/Task 间
    的字段漂移。
    """
    issues: list[MetadataOwnershipIssue] = []
    for key in metadata:
        issue = check_ownership(key, location)
        if issue is not None:
            issues.append(issue)
    return issues


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    try:
        return value != ""
    except (TypeError, ValueError):
        return True


def copy_work_item_execution_metadata(
    work_item: Any | None,
    *,
    keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return WorkItem-owned values allowed on Task as execution copies."""
    if work_item is None:
        return {}
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    selected = list(keys) if keys is not None else list(_EXECUTION_COPY_KEYS_SORTED)
    copied: dict[str, Any] = {}
    for key in selected:
        spec = metadata_spec_for_key(key)
        if spec is None or not spec.allows_task_execution_copy:
            continue
        if key not in metadata:
            continue
        value = metadata.get(key)
        if not _has_value(value):
            continue
        copied[key] = copy.deepcopy(value)
    return copied


def build_work_item_owner_execution_copy(work_item: Any | None) -> dict[str, Any]:
    """Build the runtime Task owner envelope from the WorkItem facts."""
    if work_item is None:
        return {}
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    payload = {
        "delegation_run_id": str(getattr(work_item, "run_id", "") or "").strip(),
        "delegation_cell_id": str(getattr(work_item, "cell_id", "") or "").strip(),
        "delegation_team_instance_id": str(
            getattr(work_item, "team_instance_id", "") or ""
        ).strip(),
        "delegation_team_id": str(
            # 回退链说明：team_id → metadata.team_id → cell_id（团队实例降级
            # 标识）→ ""。cell_id 非严格 team 概念，保留为 last-resort 兜底
            getattr(work_item, "team_id", "")
            or metadata.get("team_id", "")
            or getattr(work_item, "cell_id", "")
            or ""
        ).strip(),
        "delegation_seat_id": str(
            getattr(work_item, "seat_id", "") or metadata.get("seat_id", "") or ""
        ).strip(),
        "delegation_role_session_id": str(
            getattr(work_item, "role_runtime_session_id", "")
            or metadata.get("assigned_role_runtime_id", "")
            or ""
        ).strip(),
        "work_item_role_id": str(
            getattr(work_item, "role_id", "") or metadata.get("role_id", "") or ""
        ).strip(),
        "work_kind": str(
            metadata.get("work_kind", "") or getattr(work_item, "kind", "") or ""
        ).strip(),
        "manager_role_id": str(
            getattr(work_item, "manager_role_id", "") or metadata.get("manager_role_id", "") or ""
        ).strip(),
        "manager_seat_id": str(
            getattr(work_item, "manager_seat_id", "") or metadata.get("manager_seat_id", "") or ""
        ).strip(),
        "work_item_batch_id": str(getattr(work_item, "batch_id", "") or "").strip(),
    }
    return {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key in EXECUTION_COPY_KEYS and _has_value(value)
    }


def strip_disallowed_work_item_metadata_from_runtime_task(task: Any) -> list[str]:
    """Remove WorkItem-owned fields that are not valid Task execution copies."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    removed: list[str] = []
    for key in sorted(WORK_ITEM_OWNED_KEYS):
        if key in EXECUTION_COPY_KEYS:
            continue
        if key in metadata:
            metadata.pop(key, None)
            removed.append(key)
    if removed:
        task.metadata = metadata
    return removed


def filter_work_item_owned_metadata(
    updates: Mapping[str, Any], *, preserve_empty: bool = False
) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(value)
        for key, value in dict(updates or {}).items()
        if is_work_item_owned_key(str(key)) and (preserve_empty or _has_value(value))
    }


def filter_runtime_task_owned_metadata(
    updates: Mapping[str, Any], *, preserve_empty: bool = False
) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(value)
        for key, value in dict(updates or {}).items()
        if is_runtime_task_owned_key(str(key)) and (preserve_empty or _has_value(value))
    }


def validate_metadata_ownership(
    work_item: Any | None,
    task: Any | None,
) -> list[MetadataOwnershipIssue]:
    """Read-only diagnostics for WorkItem/runtime Task metadata drift."""
    if work_item is None and task is None:
        return []
    item_metadata = dict(getattr(work_item, "metadata", {}) or {}) if work_item is not None else {}
    task_metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    work_item_id = (
        str(getattr(work_item, "work_item_id", "") or "").strip()
        or str(getattr(task, "work_item_id", "") or "").strip()
    )
    runtime_task_id = str(getattr(task, "id", "") or "").strip()
    issues: list[MetadataOwnershipIssue] = []

    for key in sorted(WORK_ITEM_OWNED_KEYS):
        if key not in task_metadata:
            continue
        task_value = task_metadata.get(key)
        if not _has_value(task_value):
            continue
        work_item_has_value = key in item_metadata and _has_value(item_metadata.get(key))
        if not work_item_has_value:
            issues.append(
                MetadataOwnershipIssue(
                    code="metadata_ownership_violation",
                    severity="warning",
                    key=key,
                    owner=MetadataOwner.WORK_ITEM.value,
                    work_item_id=work_item_id,
                    runtime_task_id=runtime_task_id,
                    message=f"WorkItem-owned metadata `{key}` is present on runtime Task but missing on WorkItem.",
                )
            )
            continue
        if item_metadata.get(key) != task_value and not is_execution_copy_key(key):
            issues.append(
                MetadataOwnershipIssue(
                    code="metadata_ownership_conflict",
                    severity="warning",
                    key=key,
                    owner=MetadataOwner.WORK_ITEM.value,
                    work_item_id=work_item_id,
                    runtime_task_id=runtime_task_id,
                    message=f"WorkItem-owned metadata `{key}` differs between WorkItem and runtime Task; WorkItem wins.",
                    details={"work_item_value": item_metadata.get(key), "task_value": task_value},
                )
            )

    for key in sorted(RUNTIME_TASK_OWNED_KEYS):
        if key in item_metadata and _has_value(item_metadata.get(key)):
            issues.append(
                MetadataOwnershipIssue(
                    code="metadata_ownership_violation",
                    severity="warning",
                    key=key,
                    owner=MetadataOwner.RUNTIME_TASK.value,
                    work_item_id=work_item_id,
                    runtime_task_id=runtime_task_id,
                    message=f"Runtime Task-owned metadata `{key}` should not be stored on WorkItem.",
                )
            )
    return issues


def update_runtime_task_owned_metadata(
    task: Any, updates: Mapping[str, Any], *, preserve_empty: bool = False
) -> dict[str, Any]:
    """Apply only runtime Task-owned metadata updates to an in-memory Task."""
    filtered = filter_runtime_task_owned_metadata(updates, preserve_empty=preserve_empty)
    if not filtered:
        return {}
    task.metadata = {**dict(getattr(task, "metadata", {}) or {}), **filtered}
    return filtered
