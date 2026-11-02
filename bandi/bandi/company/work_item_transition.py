from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from bandi.company.phase import Phase, is_terminal, validate_transition
from bandi.company.work_item import transition_work_item
from bandi.company.work_item_identity import work_item_identity_payload

logger = logging.getLogger(__name__)

DEPENDENCY_CLASS_DEFAULT = "hard"

DEPENDENCY_WORK_ITEM_IDS_KEY = "dependency_work_item_ids"
DEPENDENCY_CLASSES_KEY = "dependency_classes"
DEPENDENCY_SETTLEMENT_KEY = "dependency_settlement"
WAITING_ON_WORK_ITEM_IDS_KEY = "waiting_on_work_item_ids"
LAST_TRANSITION_REASON_KEY = "last_transition_reason"
PRUNED_DEPENDENCY_IDS_KEY = "pruned_dependency_work_item_ids"
REPLACEMENT_DEPENDENCY_IDS_KEY = "replacement_dependency_work_item_ids"
DELETED_BY_MANAGER_TOOL_KEY = "deleted_by_manager_tool"
HIDDEN_FROM_COMPANY_KANBAN_KEY = "hidden_from_company_kanban"
UPSTREAM_VISIBILITY_KEY = "upstream_visibility"

_SOFT_DEPENDENCY_CLASSES = frozenset({"soft", "info"})

FRONTIER_KEY = "frontier"
REWORK_FEEDBACK_KEY = "rework_feedback"
DELEGATED_CHILDREN_PENDING_KEY = "delegated_children_pending"
CASCADED_AT_KEY = "cascaded_at"
SETTLED_AT_KEY = "settled_at"
DEPENDENCY_PRUNED_AT_KEY = "dependency_pruned_at"
PRE_SYNTHESIS_WORK_KIND_KEY = "pre_synthesis_work_kind"
SYNTHESIS_TURN_STARTED_KEY = "synthesis_turn_started"
CURRENT_TURN_MODE_KEY = "current_turn_mode"
WAITING_FOR_CHILDREN_FRONTIER = "waiting_for_children"
SYNTHESIS_READY_FRONTIER = "synthesis_ready"
SETTLEMENT_READY_FRONTIER = "settlement_ready"
RESUMED_FRONTIER = "resumed"

# settlement path: their whole job is to integrate child results (including
# partial/failed ones) and carry them upward.
_SETTLEMENT_ROLLUP_KINDS: frozenset[str] = frozenset(
    {
        "aggregate",
        "deliver",
        "delivery",
        "synthesis",
        "synthesize",
    }
)

# 终态判定统一走 phase.py 公共 is_terminal()（WIT-07：不再本地镜像 _DONE_PHASES）


def _metadata(item: Any) -> dict[str, Any]:
    return dict(getattr(item, "metadata", {}) or {}) if item is not None else {}


def _clean_ids(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    try:
        values = list(raw or [])
    except TypeError:
        return []
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


def work_item_id(item: Any) -> str:
    return str(getattr(item, "id", "") or "").strip()


def dependency_work_item_ids(item: Any) -> list[str]:
    return _clean_ids(_metadata(item).get(DEPENDENCY_WORK_ITEM_IDS_KEY, []))


def waiting_on_work_item_ids(item: Any) -> list[str]:
    return _clean_ids(_metadata(item).get(WAITING_ON_WORK_ITEM_IDS_KEY, []))


def _dependency_class(classes_map: dict[str, Any], dependency_id: str) -> str:
    value = classes_map.get(dependency_id, DEPENDENCY_CLASS_DEFAULT) or DEPENDENCY_CLASS_DEFAULT
    return str(value).strip().lower()


def dependency_class_for(item: Any, dependency_id: str) -> str:
    classes = dict(_metadata(item).get(DEPENDENCY_CLASSES_KEY, {}) or {})
    return _dependency_class(classes, dependency_id)


def is_soft_dependency(item: Any, dependency_id: str) -> bool:
    return dependency_class_for(item, dependency_id) in _SOFT_DEPENDENCY_CLASSES


def _dependency_replacement_ids(item: Any) -> list[str]:
    metadata = _metadata(item)
    raw = (
        metadata.get(REPLACEMENT_DEPENDENCY_IDS_KEY)
        or metadata.get("replacement_work_item_ids")
        or metadata.get("superseded_by_work_item_ids")
        or []
    )
    return _clean_ids(raw)


def is_prunable_dependency_work_item(item: Any) -> bool:
    """True when a dependency target is obsolete rather than merely failed."""
    if item is None:
        return False
    metadata = _metadata(item)
    if bool(metadata.get(DELETED_BY_MANAGER_TOOL_KEY, False)):
        return True
    upstream_visibility = str(metadata.get(UPSTREAM_VISIBILITY_KEY, "") or "").strip().lower()
    return (
        getattr(item, "phase", None) == Phase.CANCELLED
        and bool(metadata.get(HIDDEN_FROM_COMPANY_KANBAN_KEY, False))
        and upstream_visibility == "hidden"
    )


def normalize_dependency_work_item_ids(
    raw_dependency_ids: list[str] | tuple[str, ...] | set[str],
    work_item_by_id: dict[str, Any],
    *,
    owner_work_item_id: str = "",
) -> tuple[list[str], list[str]]:
    """Drop or replace stale dependency ids while preserving hard failures.

    Returns ``(active_ids, pruned_ids)``. Replacement ids come from metadata
    on the obsolete dependency target and are validated against the current
    graph so a deleted replacement cannot resurrect another stale edge.
    """
    owner_id = str(owner_work_item_id or "").strip()
    active: list[str] = []
    pruned: list[str] = []

    def append_active(candidate_id: str) -> None:
        candidate = str(candidate_id or "").strip()
        if not candidate:
            return
        if candidate == owner_id:
            pruned.append(candidate)
            return
        if is_prunable_dependency_work_item(work_item_by_id.get(candidate)):
            pruned.append(candidate)
            return
        active.append(candidate)

    for raw_id in list(raw_dependency_ids or []):
        dep_id = str(raw_id or "").strip()
        if not dep_id:
            continue
        item = work_item_by_id.get(dep_id)
        if is_prunable_dependency_work_item(item):
            pruned.append(dep_id)
            for replacement_id in _dependency_replacement_ids(item):
                append_active(replacement_id)
            continue
        append_active(dep_id)

    return list(dict.fromkeys(active)), list(dict.fromkeys(pruned))


def compute_doomed_work_item_ids(work_item_by_id: dict[str, Any]) -> set[str]:
    """Ids of work items that can never reach on their own.

    Seeds are FAILED/CANCELLED items. Propagation: a QUEUED /
    WAITING_DEPENDENCIES / READY item whose hard dependency is doomed (and
    not ) can never become runnable, so it is doomed too. Pure
    fixpoint over the run snapshot — no IO. Dependencies are normalized
    first, so a doomed card that was rewired via ``delete_work_item`` +
    replacement ids drops back out of the set. A card carrying a
    ``dependency_settlement`` stamp was RELEASED by the frontier pass over
    its failures — it is a runnable triage card, not doomed.
    """
    doomed: set[str] = {
        item_id
        for item_id, item in work_item_by_id.items()
        if getattr(item, "phase", None) in (Phase.FAILED, Phase.CANCELLED)
    }
    if not doomed:
        return doomed
    changed = True
    while changed:
        changed = False
        for item_id, item in work_item_by_id.items():
            if item_id in doomed:
                continue
            if getattr(item, "phase", None) not in (
                Phase.QUEUED,
                Phase.WAITING_DEPENDENCIES,
                Phase.READY,
            ):
                continue
            metadata = _metadata(item)
            if dict(metadata.get(DEPENDENCY_SETTLEMENT_KEY, {}) or {}):
                continue
            raw_ids = _clean_ids(metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY, []))
            if not raw_ids:
                continue
            dep_ids, _pruned = normalize_dependency_work_item_ids(
                raw_ids, work_item_by_id, owner_work_item_id=item_id
            )
            dependency_classes = dict(metadata.get(DEPENDENCY_CLASSES_KEY, {}) or {})
            for dep_id in dep_ids:
                dep = work_item_by_id.get(dep_id)
                if dep is None or dep_id not in doomed:
                    continue
                dep_class = _dependency_class(dependency_classes, dep_id)
                if dep_class in _SOFT_DEPENDENCY_CLASSES:
                    continue
                if getattr(dep, "phase", None) == Phase.APPROVED:
                    continue
                doomed.add(item_id)
                changed = True
                break
    return doomed


def settled_failure_dependency_ids(metadata: dict[str, Any] | None) -> set[str]:
    """Dependency ids this card was explicitly released over despite failure.

    Includes the ``stuck`` ids (transitively-blocked, still non-terminal):
    a released triage card must be claimable even while its stuck
    dependencies linger — they are settled context awaiting rebuild or the
    settlement cascade, not blockers. Reads the ``dependency_settlement``
    stamp written by the frontier pass.
    """
    settlement = dict((metadata or {}).get(DEPENDENCY_SETTLEMENT_KEY, {}) or {})
    return {
        str(item).strip()
        for key in ("failed", "cancelled", "stuck")
        for item in list(settlement.get(key, []) or [])
        if str(item).strip()
    }


def _work_item_kind(item: Any, metadata: dict[str, Any]) -> str:
    turn = getattr(item, "turn_type", None)
    turn_value = turn.value if hasattr(turn, "value") else ""
    return (
        str(
            metadata.get("work_kind")
            or metadata.get("delegation_turn_kind")
            or getattr(item, "kind", "")
            or turn_value
            or ""
        )
        .strip()
        .lower()
    )


def _dependency_settlement_snapshot(
    metadata: dict[str, Any],
    dependency_phases: dict[str, Any],
    doomed_ids: set[str],
) -> dict[str, Any]:
    """Single truth for the failure-triage gate over one card's deps.

    "Settled with failures": every dependency is terminal or doomed
    (transitively blocked by a terminal failure), so waiting longer cannot
    change the outcome. The failure may be purely transitive (a direct dep
    is stuck behind a FAILED card elsewhere), so the trigger counts stuck
    deps too. Info-class deps never gate claiming, so they do not gate
    settlement either. Missing deps (phase None) stay unsettled.
    """
    dependency_class_map = dict(metadata.get(DEPENDENCY_CLASSES_KEY, {}) or {})

    def _dep_is_info(dep_id: str) -> bool:
        return _dependency_class(dependency_class_map, dep_id) == "info"

    all_approved = all(p == Phase.APPROVED for p in dependency_phases.values())
    failed = [d for d, p in dependency_phases.items() if p == Phase.FAILED]
    cancelled = [d for d, p in dependency_phases.items() if p == Phase.CANCELLED]
    stuck = [
        d
        for d, p in dependency_phases.items()
        if d in doomed_ids and p not in (Phase.FAILED, Phase.CANCELLED)
    ]
    settled_with_failures = (
        not all_approved
        and bool(failed or cancelled or stuck)
        and all(
            (p is not None and is_terminal(p)) or dep_id in doomed_ids or _dep_is_info(dep_id)
            for dep_id, p in dependency_phases.items()
        )
    )
    return {
        "all_approved": all_approved,
        "failed": failed,
        "cancelled": cancelled,
        "stuck": stuck,
        "settled_with_failures": settled_with_failures,
    }


def _is_settlement_release_candidate(item: Any, metadata: dict[str, Any]) -> bool:
    """Cards a failure-triage release may wake: the delegating parent, a
    roll-up card, or an already-released (stamped) in-flight triage card."""
    phase = getattr(item, "phase", None)
    if phase == Phase.WAITING_FOR_CHILDREN:
        return True
    if (
        phase == Phase.WAITING_DEPENDENCIES
        and _work_item_kind(item, metadata) in _SETTLEMENT_ROLLUP_KINDS
    ):
        return True
    return phase in (Phase.READY, Phase.READY_FOR_REWORK, Phase.RUNNING) and bool(
        dict(metadata.get(DEPENDENCY_SETTLEMENT_KEY, {}) or {})
    )


def _dependency_phase_snapshot(
    work_item_by_id: dict[str, Any], dependency_ids: list[str]
) -> dict[str, Any]:
    return {
        dep_id: (
            getattr(work_item_by_id[dep_id], "phase", None) if dep_id in work_item_by_id else None
        )
        for dep_id in dependency_ids
    }


def has_pending_settlement_release(work_item_by_id: dict[str, Any]) -> bool:
    """True when some card is due a failure-triage release.

    Used by the dispatcher tick for cards created AFTER their dependency
    already failed: the failure's transition hook predates the card, so no
    future event would ever run the frontier for it. Cards already
    released (stamp present, phase moved) return False here, keeping the
    tick idempotent.
    """
    doomed_ids = compute_doomed_work_item_ids(work_item_by_id)
    if not doomed_ids:
        return False
    for item_id, item in work_item_by_id.items():
        metadata = _metadata(item)
        phase = getattr(item, "phase", None)
        if not (
            phase == Phase.WAITING_FOR_CHILDREN
            or (
                phase == Phase.WAITING_DEPENDENCIES
                and _work_item_kind(item, metadata) in _SETTLEMENT_ROLLUP_KINDS
            )
        ):
            continue
        raw_ids = _clean_ids(metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY, []))
        if not raw_ids:
            continue
        dependency_ids, _pruned = normalize_dependency_work_item_ids(
            raw_ids, work_item_by_id, owner_work_item_id=item_id
        )
        dependency_phases = _dependency_phase_snapshot(work_item_by_id, dependency_ids)
        snapshot = _dependency_settlement_snapshot(metadata, dependency_phases, doomed_ids)
        if snapshot["settled_with_failures"]:
            return True
    return False


def settlement_cascade_cancel_ids(
    work_item_by_id: dict[str, Any],
    settlement: dict[str, Any],
) -> set[str]:
    """Ids to cancel once a failure-triage card is

    Transitive closure through the doomed set: cancelling a stuck child
    kills anything hard-chained onto it, and those deeper nodes appear in
    no released card's direct dependency list — without the closure they
    would linger non-terminal forever. ``covered`` seeds from ALL stamped
    stuck ids regardless of phase: a stuck child already cancelled by a
    previous (partially failed) cascade attempt must still conduct the
    traversal, or deeper nodes behind it become unreachable on retry.
    Growth only follows edges INTO the covered set (doomed dependents of a
    covered node), so doomed subtrees owned by a different (not-yet-approved)
    triage card are left for that card's own decision. Soft/info edges never
    close. Callers guard idempotency via the ``cascaded_at`` stamp.
    """
    doomed_ids = compute_doomed_work_item_ids(work_item_by_id)
    covered_ids: set[str] = {
        str(item).strip()
        for item in list(settlement.get("stuck", []) or [])
        if str(item).strip() and str(item).strip() in work_item_by_id
    }
    grew = bool(covered_ids)
    while grew:
        grew = False
        for item_id, item in work_item_by_id.items():
            if item_id in covered_ids or item_id not in doomed_ids:
                continue
            item_metadata = _metadata(item)
            raw_ids = _clean_ids(item_metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY, []))
            if not raw_ids:
                continue
            dep_ids, _pruned = normalize_dependency_work_item_ids(
                raw_ids, work_item_by_id, owner_work_item_id=item_id
            )
            item_classes = dict(item_metadata.get(DEPENDENCY_CLASSES_KEY, {}) or {})
            for dep_id in dep_ids:
                if dep_id not in covered_ids:
                    continue
                dep_class = _dependency_class(item_classes, dep_id)
                if dep_class in _SOFT_DEPENDENCY_CLASSES:
                    continue
                covered_ids.add(item_id)
                grew = True
                break
    return {
        covered_id
        for covered_id in covered_ids
        if covered_id in doomed_ids
        and not is_terminal(getattr(work_item_by_id.get(covered_id), "phase", None))
    }


# Default staleness threshold (minutes) for dependency-deadlock detection.
STAGNATION_MINUTES = 30

# Re-entrancy guard: refresh_dependents writes to work items, which fires
# phase-transition hooks, which can re-call refresh. The outer call already
_REFRESH_IN_FLIGHT: ContextVar[frozenset[str]] = ContextVar(
    "refresh_dependents_in_flight", default=frozenset()
)

# synthesis turn by the frontier pass.
_SYNTHESIS_SKIP_KINDS: frozenset[str] = frozenset(
    {
        "aggregate",
        "deliver",
        "delivery",
        "intake",
        "review",
        "synthesis",
        "synthesize",
    }
)


def _should_enter_synthesis_turn(
    item: Any,
    metadata: dict[str, Any],
    dependency_ids: list[str],
) -> bool:
    if getattr(item, "phase", None) != Phase.WAITING_FOR_CHILDREN:
        return False
    if not dependency_ids:
        return False
    if bool(metadata.get(SYNTHESIS_TURN_STARTED_KEY, False)):
        return False
    if _work_item_kind(item, metadata) in _SYNTHESIS_SKIP_KINDS:
        return False
    if not (
        bool(metadata.get(DELEGATED_CHILDREN_PENDING_KEY, False))
        or str(metadata.get(FRONTIER_KEY, "") or "").strip() == WAITING_FOR_CHILDREN_FRONTIER
        or str(metadata.get("last_delegated_by_seat_id", "") or "").strip()
    ):
        return False
    return True


def _synthesis_turn_summary(item: Any, dependency_ids: list[str]) -> str:
    title = str(getattr(item, "title", "") or "delegated work").strip()
    child_count = len(dependency_ids)
    manager_label = str(getattr(item, "assignee_role", "") or "the upstream owner").strip()
    return (
        f"Synthesize the {child_count} approved child work item"
        f"{'' if child_count == 1 else 's'} for `{title}` and prepare the "
        f"handoff for {manager_label}. Include what was completed, evidence, "
        "remaining risks, and any decision needed from the upper role."
    )


def _failure_triage_turn_summary(
    item: Any,
    dependency_phases: dict[str, Any],
    failed_ids: list[str],
    cancelled_ids: list[str],
    stuck_ids: list[str],
) -> str:
    title = str(getattr(item, "title", "") or "delegated work").strip()
    manager_label = str(getattr(item, "assignee_role", "") or "the upstream owner").strip()
    approved_count = sum(1 for p in dependency_phases.values() if p == Phase.APPROVED)
    parts = [
        f"{approved_count} approved",
        f"{len(failed_ids) + len(cancelled_ids)} failed/cancelled",
    ]
    if stuck_ids:
        parts.append(f"{len(stuck_ids)} blocked downstream")
    return (
        f"Triage the delegated results for `{title}` ({', '.join(parts)}). "
        "Decide how to handle the failures — rebuild the failed work, accept "
        "partial results, or escalate the gap — then prepare the handoff for "
        f"{manager_label}."
    )


def _synthesis_metadata(item: Any, dependency_ids: list[str], stamp: datetime) -> dict[str, Any]:
    previous_kind = _work_item_kind(item, _metadata(item))
    return {
        PRE_SYNTHESIS_WORK_KIND_KEY: previous_kind,
        "work_kind": "synthesize",
        "delegation_turn_kind": "synthesize",
        **work_item_identity_payload(
            projection_id=work_item_id(item),
            turn_type="aggregate",
        ),
        CURRENT_TURN_MODE_KEY: "synthesize_required",
        SYNTHESIS_TURN_STARTED_KEY: True,
        "synthesis_ready_at": stamp.isoformat(),
        "synthesis_source_work_item_ids": list(dependency_ids),
        "synthesis_reports_to_role_id": str(getattr(item, "assignee_role", "") or "").strip(),
        FRONTIER_KEY: SYNTHESIS_READY_FRONTIER,
        "needs_manager_attention": False,
        "turn_summary": _synthesis_turn_summary(item, dependency_ids),
    }


def _delivery_metadata(item: Any, metadata: dict[str, Any], stamp: datetime) -> dict[str, Any]:
    if _work_item_kind(item, metadata) not in {"deliver", "delivery"}:
        return {}
    return {
        "work_kind": "delivery",
        "delegation_turn_kind": "delivery",
        **work_item_identity_payload(
            projection_id=work_item_id(item),
            turn_type="deliver",
        ),
        CURRENT_TURN_MODE_KEY: "deliver_required",
        "delivery_turn_ready_at": stamp.isoformat(),
    }


def refresh_dependents(
    store: Any,
    *,
    now: datetime | None = None,
    transitions_out: list[tuple[str, str, str]] | None = None,
    errors_out: list[str] | None = None,
    runtime_owner: str | None = None,
) -> bool:
    """Walk all work items and propagate dependency state to parent phases.

    Synchronous Bandi adaptation of OpenOPC ``refresh_dependents_for_run``:
    the run_id dimension collapses to the single global store, and the
    update path collapses to ``store.save`` (Bandi's field-level
    ``update_delegation_work_item`` has no equivalent here). One pass:

    - WAITING_DEPENDENCIES → READY / READY_FOR_REWORK when all deps approve.
    - WAITING_FOR_CHILDREN → READY as a synthesis turn when delegated
      children are done; otherwise → RUNNING.
    - Failure-triage release: when every dep is terminal or doomed and at
      least one failed, the delegating parent / roll-up card is released
      anyway with a ``dependency_settlement`` stamp.
    - Reverse direction: READY → WAITING_DEPENDENCIES, RUNNING →
      WAITING_FOR_CHILDREN when a new dep appears.
    - Settlement cascade: once a stamped triage card reaches , its
      leftover doomed descendants are cancelled so the graph can terminalize.

    Transitions go through ``validate_transition`` (the extended state
    machine), and metadata is written in the same ``store.save``. Returns
    True when any item was actually mutated (value-level idempotent).

    两阶段提交：主循环先在内存计算全部 pending 变更（不写库），再统一经
    ``store.save_checked`` 乐观锁提交——并发修改的卡被跳过并记入 ``errors_out``
    下轮 tick 幂等重试，杜绝「前几张已保存、后几张失败」的部分提交静默态。
    """
    if not store or not hasattr(store, "list_all") or not hasattr(store, "save"):
        return False
    if not hasattr(store, "save_checked"):
        return False
    if _REFRESH_IN_FLIGHT.get():
        return False
    token = _REFRESH_IN_FLIGHT.set(frozenset({"bandi-refresh"}))
    stamp = now or datetime.now(UTC)
    try:
        work_items = store.list_all()
        if runtime_owner is not None:
            work_items = [
                item
                for item in work_items
                if str((item.metadata or {}).get("runtime_owner") or "company") == runtime_owner
            ]
        work_item_by_id = {work_item_id(item): item for item in work_items}
        doomed_ids = compute_doomed_work_item_ids(work_item_by_id)
        changed = False
        pending_writes: list[tuple[Any, int, str, str, dict[str, Any], Any]] = []
        for work_item in work_items:
            item_id = work_item_id(work_item)
            metadata = _metadata(work_item)
            raw_dependency_ids = _clean_ids(metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY, []))
            if not raw_dependency_ids:
                continue
            dependency_ids, pruned_ids = normalize_dependency_work_item_ids(
                raw_dependency_ids, work_item_by_id, owner_work_item_id=item_id
            )
            dependency_phases = _dependency_phase_snapshot(work_item_by_id, dependency_ids)
            snapshot = _dependency_settlement_snapshot(
                metadata,
                dependency_phases,
                # doomed 集在 pass 内按需重算：同一轮前面释放的依赖（phase/
                # 仅在 item 实际迁移 phase 后重算（doomed 只依赖 phase）
                # 无迁移的 item 复用当前缓存，避免每 item 一次 O(N) 全图扫描。
                doomed_ids,
            )
            all_approved = snapshot["all_approved"]
            failed_dep_ids = snapshot["failed"]
            cancelled_dep_ids = snapshot["cancelled"]
            stuck_dep_ids = snapshot["stuck"]
            settled_with_failures = snapshot["settled_with_failures"]
            phase = getattr(work_item, "phase", None)
            target_phase = phase
            metadata_updates: dict[str, Any] = {}
            entered_synthesis_turn = False
            if dependency_ids != raw_dependency_ids:
                metadata_updates[DEPENDENCY_WORK_ITEM_IDS_KEY] = list(dependency_ids)
                metadata_updates[DEPENDENCY_PRUNED_AT_KEY] = stamp.isoformat()
            if pruned_ids:
                previous_pruned = _clean_ids(metadata.get(PRUNED_DEPENDENCY_IDS_KEY, []))
                metadata_updates[PRUNED_DEPENDENCY_IDS_KEY] = list(
                    dict.fromkeys([*previous_pruned, *pruned_ids])
                )
            if all_approved:
                if _should_enter_synthesis_turn(work_item, metadata, dependency_ids):
                    entered_synthesis_turn = True
                    target_phase = Phase.READY
                    metadata_updates.update(_synthesis_metadata(work_item, dependency_ids, stamp))
                elif phase == Phase.WAITING_DEPENDENCIES:
                    target_phase = (
                        Phase.READY_FOR_REWORK
                        if str(metadata.get(REWORK_FEEDBACK_KEY, "") or "").strip()
                        else Phase.READY
                    )
                elif phase == Phase.WAITING_FOR_CHILDREN:
                    target_phase = Phase.RUNNING
                    metadata_updates.update(_delivery_metadata(work_item, metadata, stamp))
                metadata_updates[WAITING_ON_WORK_ITEM_IDS_KEY] = []
                if metadata.get(DELEGATED_CHILDREN_PENDING_KEY):
                    metadata_updates[DELEGATED_CHILDREN_PENDING_KEY] = False
                if (
                    str(metadata.get(FRONTIER_KEY, "") or "") == WAITING_FOR_CHILDREN_FRONTIER
                    and not entered_synthesis_turn
                ):
                    metadata_updates[FRONTIER_KEY] = RESUMED_FRONTIER
            elif settled_with_failures and _is_settlement_release_candidate(work_item, metadata):
                if phase in (Phase.READY, Phase.READY_FOR_REWORK, Phase.RUNNING):
                    stamp_record = dict(metadata.get(DEPENDENCY_SETTLEMENT_KEY, {}) or {})
                    known_failures = set(_clean_ids(stamp_record.get("failed", [])))
                    known_cancelled = set(_clean_ids(stamp_record.get("cancelled", [])))
                    new_failure_ids = [
                        dep_id
                        for dep_id in [*failed_dep_ids, *cancelled_dep_ids]
                        if dep_id not in known_failures and dep_id not in known_cancelled
                    ]
                    if new_failure_ids:
                        # 已 release 卡出现未分诊的新失败：回等待态重新分诊
                        # 而不是静默吸收后带着未处理失败被直接 。
                        target_phase = (
                            Phase.WAITING_DEPENDENCIES
                            if phase in (Phase.READY, Phase.READY_FOR_REWORK)
                            else Phase.WAITING_FOR_CHILDREN
                        )
                        metadata_updates[WAITING_ON_WORK_ITEM_IDS_KEY] = dependency_ids
                elif phase == Phase.WAITING_FOR_CHILDREN:
                    if _should_enter_synthesis_turn(work_item, metadata, dependency_ids):
                        entered_synthesis_turn = True
                        target_phase = Phase.READY
                        metadata_updates.update(
                            _synthesis_metadata(work_item, dependency_ids, stamp)
                        )
                    else:
                        target_phase = Phase.RUNNING
                        metadata_updates.update(_delivery_metadata(work_item, metadata, stamp))
                else:
                    target_phase = (
                        Phase.READY_FOR_REWORK
                        if str(metadata.get(REWORK_FEEDBACK_KEY, "") or "").strip()
                        else Phase.READY
                    )
                if target_phase != phase:
                    metadata_updates[DEPENDENCY_SETTLEMENT_KEY] = {
                        "failed": list(failed_dep_ids),
                        "cancelled": list(cancelled_dep_ids),
                        "stuck": list(stuck_dep_ids),
                        SETTLED_AT_KEY: stamp.isoformat(),
                    }
                    metadata_updates[FRONTIER_KEY] = SETTLEMENT_READY_FRONTIER
                    metadata_updates[WAITING_ON_WORK_ITEM_IDS_KEY] = []
                    metadata_updates["turn_summary"] = _failure_triage_turn_summary(
                        work_item,
                        dependency_phases,
                        failed_dep_ids,
                        cancelled_dep_ids,
                        stuck_dep_ids,
                    )
                    if metadata.get(DELEGATED_CHILDREN_PENDING_KEY):
                        metadata_updates[DELEGATED_CHILDREN_PENDING_KEY] = False
            else:
                if phase == Phase.READY:
                    target_phase = Phase.WAITING_DEPENDENCIES
                elif phase == Phase.RUNNING:
                    target_phase = Phase.WAITING_FOR_CHILDREN
                metadata_updates[WAITING_ON_WORK_ITEM_IDS_KEY] = dependency_ids
            new_metadata = {**metadata, **metadata_updates}
            if target_phase != phase or new_metadata != metadata:
                original_phase = getattr(work_item, "phase", None)
                original_metadata = _metadata(work_item)
                original_updated_at = getattr(work_item, "updated_at", None)
                try:
                    if target_phase != phase:
                        validate_transition(phase, target_phase)
                        work_item.phase = target_phase
                    work_item.metadata = new_metadata
                    work_item.updated_at = stamp
                    # transitions_out 不在阶段 1 append（阶段 2 提交失败会留下幻影
                    # 转换报告）；改为阶段 2 提交成功后按 (before, after) 记录。
                    pending_writes.append(
                        (
                            work_item,
                            getattr(work_item, "revision", 0),
                            phase.value,
                            target_phase.value,
                            original_metadata,
                            original_updated_at,
                        )
                    )
                    changed = True
                    if target_phase != phase:
                        doomed_ids = compute_doomed_work_item_ids(work_item_by_id)
                except Exception:
                    logger.warning(
                        "refresh: work item %s failed to propagate; restoring in-memory state",
                        item_id,
                        exc_info=True,
                    )
                    work_item.phase = original_phase
                    work_item.metadata = original_metadata
                    work_item.updated_at = original_updated_at
                    continue
        # 并发修改/提交失败的卡跳过并记入 errors_out（下轮 tick 幂等重试）；
        # 提交成功才记录 transitions_out（防幻影转换报告）。
        for (
            work_item,
            expected_revision,
            phase_before,
            phase_after,
            metadata_before,
            updated_at_before,
        ) in pending_writes:
            commit_error: str | None = None
            try:
                committed = store.save_checked(work_item, expected_revision)
            except Exception:
                committed = False
                commit_error = "commit failed"
                logger.warning(
                    "refresh: work item %s failed to commit; restoring in-memory state",
                    work_item_id(work_item),
                    exc_info=True,
                )
            if not committed:
                conflict_id = work_item_id(work_item)
                reason = commit_error or (
                    f"concurrent modification (rev {expected_revision}), will retry next tick"
                )
                # （settlement cascade 等）消费「未落盘的脏 phase」做决策
                try:
                    work_item.phase = Phase(phase_before)
                except ValueError:
                    pass
                work_item.metadata = metadata_before
                work_item.updated_at = updated_at_before
                logger.warning("refresh: work item %s %s", conflict_id, reason)
                if errors_out is not None:
                    errors_out.append(f"refresh: {conflict_id} {reason}")
            elif transitions_out is not None and phase_before != phase_after:
                transitions_out.append((work_item_id(work_item), phase_before, phase_after))
        # Settlement cascade: once a failure-triage card reaches
        # cancel them so the graph reaches a fully-terminal state. Children
        # rebuilt or rewired (delete_work_item + replacement ids) drop out
        for work_item in work_items:
            metadata = _metadata(work_item)
            settlement = dict(metadata.get(DEPENDENCY_SETTLEMENT_KEY, {}) or {})
            if not settlement or settlement.get(CASCADED_AT_KEY):
                continue
            if getattr(work_item, "phase", None) != Phase.APPROVED:
                continue
            cancel_ids = settlement_cascade_cancel_ids(work_item_by_id, settlement)
            cascade_complete = True
            for cancel_id in sorted(cancel_ids):
                cancel_item = work_item_by_id.get(cancel_id)
                if cancel_item is None:
                    continue
                try:
                    transition_work_item(
                        cancel_item,
                        Phase.CANCELLED,
                        store,
                        reason="upstream_dependency_failed_parent_settled",
                    )
                    changed = True
                except Exception:
                    logger.warning(
                        "refresh: settlement cascade cancel failed wid=%s", cancel_id, exc_info=True
                    )
                    cascade_complete = False
            if not cascade_complete:
                # leftover cancels instead of permanently orphaning them.
                continue
            triage = store.get(work_item_id(work_item))
            if triage is not None:
                try:
                    triage.metadata = {
                        **_metadata(triage),
                        DEPENDENCY_SETTLEMENT_KEY: {
                            **settlement,
                            CASCADED_AT_KEY: stamp.isoformat(),
                        },
                    }
                    triage.updated_at = stamp
                    store.save(triage)
                except Exception:
                    logger.warning(
                        "refresh: settlement cascade stamp failed wid=%s",
                        work_item_id(work_item),
                        exc_info=True,
                    )
        return changed
    finally:
        _REFRESH_IN_FLIGHT.reset(token)


def detect_stagnant_work_items(
    store: Any,
    *,
    stale_minutes: int = STAGNATION_MINUTES,
) -> list[dict[str, Any]]:
    """Dependency-waiting cards idle past ``stale_minutes``.

    A WAITING_DEPENDENCIES / WAITING_FOR_CHILDREN card whose ``updated_at``
    is stale and that carries no ``dependency_settlement`` stamp is a
    dependency deadlock candidate: the frontier found nothing to propagate
    so no dependency event will ever transition it, and it is not a released
    triage card waiting on the worker queue. Returns records for manual /
    manager triage (e.g. stuck dependency rebuild).
    """
    if not store or not hasattr(store, "list_all"):
        return []
    items = store.list_all()
    now = datetime.now(UTC)
    stagnant: list[dict[str, Any]] = []
    for item in items:
        phase = getattr(item, "phase", None)
        if phase not in (Phase.WAITING_DEPENDENCIES, Phase.WAITING_FOR_CHILDREN):
            continue
        if dict(_metadata(item).get(DEPENDENCY_SETTLEMENT_KEY, {}) or {}):
            continue
        updated_at = getattr(item, "updated_at", None)
        if updated_at is None or updated_at.tzinfo is None:
            continue
        elapsed_minutes = (now - updated_at).total_seconds() / 60.0
        if elapsed_minutes >= stale_minutes:
            stagnant.append(
                {
                    "id": work_item_id(item),
                    "phase": phase.value,
                    "elapsed_minutes": round(elapsed_minutes, 1),
                }
            )
    return stagnant
