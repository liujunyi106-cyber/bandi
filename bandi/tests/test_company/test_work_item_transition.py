from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.company.work_item_transition import (
    _clean_ids,
    _dependency_settlement_snapshot,
    _failure_triage_turn_summary,
    _is_settlement_release_candidate,
    _should_enter_synthesis_turn,
    _synthesis_turn_summary,
    _work_item_kind,
    compute_doomed_work_item_ids,
    dependency_class_for,
    dependency_work_item_ids,
    detect_stagnant_work_items,
    has_pending_settlement_release,
    is_prunable_dependency_work_item,
    normalize_dependency_work_item_ids,
    refresh_dependents,
    settled_failure_dependency_ids,
    settlement_cascade_cancel_ids,
    waiting_on_work_item_ids,
    work_item_id,
)


def _item(
    item_id: str = "wi-1",
    *,
    phase: Phase = Phase.QUEUED,
    metadata: dict[str, Any] | None = None,
    turn_type: WorkItemTurnType = WorkItemTurnType.EXECUTE,
    title: str = "work item",
) -> Any:
    item = create_work_item(
        title=title,
        turn_type=turn_type,
        metadata=metadata,
        phase=phase,
    )
    item.id = item_id
    return item


def _by_id(*items: Any) -> dict[str, Any]:
    return {it.id: it for it in items}


class TestWorkItemId:
    def test_none_returns_empty_and_reads_work_item_id(self):
        assert work_item_id(None) == ""
        assert work_item_id(_item(item_id="wi-9")) == "wi-9"


class TestDependencyWorkItemIds:
    def test_empty_reads_and_drops_blank_ids(self):
        assert dependency_work_item_ids(_item()) == []
        item = _item(metadata={"dependency_work_item_ids": ["a", "b"]})
        assert dependency_work_item_ids(item) == ["a", "b"]
        item = _item(metadata={"dependency_work_item_ids": ["a", "", "b"]})
        assert dependency_work_item_ids(item) == ["a", "b"]


class TestWaitingOnWorkItemIds:
    def test_empty_metadata_and_reads_waiting_on_ids(self):
        assert waiting_on_work_item_ids(_item()) == []
        item = _item(metadata={"waiting_on_work_item_ids": ["a", "b"]})
        assert waiting_on_work_item_ids(item) == ["a", "b"]


class TestDependencyClassFor:
    def test_default_explicit_and_case_normalized_classes(self):
        assert dependency_class_for(_item(), "dep-1") == "hard"
        item = _item(metadata={"dependency_classes": {"dep-1": "soft"}})
        assert dependency_class_for(item, "dep-1") == "soft"
        item = _item(metadata={"dependency_classes": {"dep-1": "INFO"}})
        assert dependency_class_for(item, "dep-1") == "info"


class TestIsPrunableDependencyWorkItem:
    def test_none_is_not_prunable(self):
        assert is_prunable_dependency_work_item(None) is False

    def test_deleted_by_manager_tool_and_hidden_cancelled_prunable(self):
        item = _item(metadata={"deleted_by_manager_tool": True})
        assert is_prunable_dependency_work_item(item) is True
        item = _item(
            phase=Phase.CANCELLED,
            metadata={"hidden_from_company_kanban": True, "upstream_visibility": "hidden"},
        )
        assert is_prunable_dependency_work_item(item) is True

    def test_plain_cancelled_and_hidden_approved_not_prunable(self):
        item = _item(phase=Phase.CANCELLED)
        assert is_prunable_dependency_work_item(item) is False
        item = _item(
            phase=Phase.APPROVED,
            metadata={"hidden_from_company_kanban": True, "upstream_visibility": "hidden"},
        )
        assert is_prunable_dependency_work_item(item) is False


class TestNormalizeDependencyWorkItemIds:
    def test_active_ids_preserved_and_deduped(self):
        dep = _item(item_id="dep-1")
        active, pruned = normalize_dependency_work_item_ids(["dep-1"], _by_id(dep))
        assert active == ["dep-1"]
        assert pruned == []
        active, pruned = normalize_dependency_work_item_ids(
            ["dep-1", "dep-1", "dep-1"], _by_id(dep)
        )
        assert active == ["dep-1"]
        assert pruned == []

    def test_self_reference_and_deleted_dependency_pruned(self):
        dep = _item(item_id="dep-1")
        active, pruned = normalize_dependency_work_item_ids(
            ["dep-1"], _by_id(dep), owner_work_item_id="dep-1"
        )
        assert active == []
        assert pruned == ["dep-1"]
        deleted = _item(item_id="dep-1", metadata={"deleted_by_manager_tool": True})
        active, pruned = normalize_dependency_work_item_ids(["dep-1"], _by_id(deleted))
        assert active == []
        assert pruned == ["dep-1"]

    def test_replacement_and_unknown_ids(self):
        dep = _item(
            item_id="dep-1",
            metadata={
                "deleted_by_manager_tool": True,
                "replacement_dependency_work_item_ids": ["dep-2"],
            },
        )
        replacement = _item(item_id="dep-2")
        active, pruned = normalize_dependency_work_item_ids(["dep-1"], _by_id(dep, replacement))
        assert active == ["dep-2"]
        assert pruned == ["dep-1"]
        active, pruned = normalize_dependency_work_item_ids(["ghost"], {})
        assert active == ["ghost"]
        assert pruned == []


class TestComputeDoomedWorkItemIds:
    def test_empty_graph_and_no_terminal_seed_return_empty(self):
        assert compute_doomed_work_item_ids({}) == set()
        items = _by_id(
            _item("a", phase=Phase.READY),
            _item("b", phase=Phase.QUEUED),
        )
        assert compute_doomed_work_item_ids(items) == set()

    def test_failed_seed_is_doomed(self):
        items = _by_id(_item("a", phase=Phase.FAILED))
        assert compute_doomed_work_item_ids(items) == {"a"}

    def test_cancelled_seed_is_doomed(self):
        items = _by_id(_item("a", phase=Phase.CANCELLED))
        assert compute_doomed_work_item_ids(items) == {"a"}

    def test_waiting_ready_and_queued_dependents_doomed(self):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.FAILED)
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child", "parent"}
        parent = _item(
            "parent",
            phase=Phase.READY,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child", "parent"}
        parent = _item(
            "parent",
            phase=Phase.QUEUED,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child", "parent"}

    def test_soft_info_settlement_and_pruned_do_not_propagate(self):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={
                "dependency_work_item_ids": ["child"],
                "dependency_classes": {"child": "soft"},
            },
        )
        child = _item("child", phase=Phase.FAILED)
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child"}
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={
                "dependency_work_item_ids": ["child"],
                "dependency_classes": {"child": "info"},
            },
        )
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child"}
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={
                "dependency_work_item_ids": ["child"],
                "dependency_settlement": {
                    "failed": ["child"],
                    "settled_at": "T00:00:00",
                },
            },
        )
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child"}
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.FAILED, metadata={"deleted_by_manager_tool": True})
        assert compute_doomed_work_item_ids(_by_id(parent, child)) == {"child"}

    def test_transitive_fixpoint_propagation(self):
        top = _item(
            "top",
            phase=Phase.READY,
            metadata={"dependency_work_item_ids": ["mid"]},
        )
        mid = _item(
            "mid",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["leaf"]},
        )
        leaf = _item("leaf", phase=Phase.FAILED)
        assert compute_doomed_work_item_ids(_by_id(top, mid, leaf)) == {"leaf", "mid", "top"}


class TestSettledFailureDependencyIds:
    def test_none_metadata_and_no_stamp_return_empty(self):
        assert settled_failure_dependency_ids(None) == set()
        assert settled_failure_dependency_ids({}) == set()

    def test_collects_failed_cancelled_stuck_and_drops_blank(self):
        metadata = {
            "dependency_settlement": {
                "failed": ["a"],
                "cancelled": ["b"],
                "stuck": ["c"],
            }
        }
        assert settled_failure_dependency_ids(metadata) == {"a", "b", "c"}
        metadata = {"dependency_settlement": {"failed": ["", "  ", "d"]}}
        assert settled_failure_dependency_ids(metadata) == {"d"}


class TestDependencySettlementSnapshot:
    def test_all_approved(self):
        snapshot = _dependency_settlement_snapshot(
            {}, {"a": Phase.APPROVED, "b": Phase.APPROVED}, set()
        )
        assert snapshot["all_approved"] is True
        assert snapshot["settled_with_failures"] is False
        assert snapshot["failed"] == []
        assert snapshot["cancelled"] == []
        assert snapshot["stuck"] == []

    def test_failures_cancel_and_stuck_settle(self):
        snapshot = _dependency_settlement_snapshot(
            {}, {"a": Phase.APPROVED, "b": Phase.FAILED}, set()
        )
        assert snapshot["all_approved"] is False
        assert snapshot["failed"] == ["b"]
        assert snapshot["settled_with_failures"] is True
        snapshot = _dependency_settlement_snapshot(
            {}, {"a": Phase.CANCELLED, "b": Phase.QUEUED}, {"b"}
        )
        assert snapshot["cancelled"] == ["a"]
        assert snapshot["stuck"] == ["b"]
        assert snapshot["settled_with_failures"] is True

    def test_plain_waiting_missing_and_info_dependency_not_settled(self):
        snapshot = _dependency_settlement_snapshot(
            {}, {"a": Phase.FAILED, "b": Phase.QUEUED}, set()
        )
        assert snapshot["settled_with_failures"] is False
        snapshot = _dependency_settlement_snapshot({}, {"a": Phase.FAILED, "ghost": None}, set())
        assert snapshot["settled_with_failures"] is False
        metadata = {"dependency_classes": {"b": "info"}}
        snapshot = _dependency_settlement_snapshot(
            metadata, {"a": Phase.FAILED, "b": Phase.QUEUED}, set()
        )
        assert snapshot["settled_with_failures"] is True


class TestWorkItemKind:
    def test_reads_work_kind_and_delegation_turn_kind(self):
        item = _item(metadata={"work_kind": "aggregate"})
        assert _work_item_kind(item, {"work_kind": "aggregate"}) == "aggregate"
        item = _item(metadata={"delegation_turn_kind": "deliver"})
        assert _work_item_kind(item, {"delegation_turn_kind": "deliver"}) == "deliver"

    def test_turn_type_fallback_and_lowercase_normalization(self):
        item = _item(turn_type=WorkItemTurnType.AGGREGATE)
        assert _work_item_kind(item, {}) == "aggregate"
        item = _item(metadata={"work_kind": "AGGREGATE"})
        assert _work_item_kind(item, {"work_kind": "AGGREGATE"}) == "aggregate"


class TestSettlementReleaseCandidate:
    def test_waiting_for_children_is_candidate(self):
        item = _item(phase=Phase.WAITING_FOR_CHILDREN)
        assert _is_settlement_release_candidate(item, {}) is True

    def test_waiting_dependencies_rollup_and_turn_type_fallback_candidates(self):
        item = _item(phase=Phase.WAITING_DEPENDENCIES, metadata={"work_kind": "aggregate"})
        assert _is_settlement_release_candidate(item, {"work_kind": "aggregate"}) is True
        item = _item(phase=Phase.WAITING_DEPENDENCIES, turn_type=WorkItemTurnType.AGGREGATE)
        assert _is_settlement_release_candidate(item, {}) is True

    def test_stamped_running_and_rework_candidates(self):
        item = _item(phase=Phase.RUNNING, metadata={"dependency_settlement": {"failed": ["x"]}})
        assert (
            _is_settlement_release_candidate(item, {"dependency_settlement": {"failed": ["x"]}})
            is True
        )
        item = _item(
            phase=Phase.READY_FOR_REWORK,
            metadata={"dependency_settlement": {"failed": ["x"]}},
        )
        assert (
            _is_settlement_release_candidate(item, {"dependency_settlement": {"failed": ["x"]}})
            is True
        )

    def test_non_rollup_and_ready_without_stamp_not_candidates(self):
        item = _item(phase=Phase.WAITING_DEPENDENCIES, metadata={"work_kind": "execute"})
        assert _is_settlement_release_candidate(item, {"work_kind": "execute"}) is False
        item = _item(phase=Phase.READY)
        assert _is_settlement_release_candidate(item, {}) is False


class TestHasPendingSettlementRelease:
    def test_empty_graph_false(self):
        assert has_pending_settlement_release({}) is False

    def test_no_doomed_false(self):
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.APPROVED)
        assert has_pending_settlement_release(_by_id(parent, child)) is False

    def test_waiting_and_rollup_parent_with_failed_child_true(self):
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.FAILED)
        assert has_pending_settlement_release(_by_id(parent, child)) is True
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={
                "dependency_work_item_ids": ["child"],
                "work_kind": "aggregate",
            },
        )
        assert has_pending_settlement_release(_by_id(parent, child)) is True

    def test_ordinary_and_released_parent_false(self):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.FAILED)
        assert has_pending_settlement_release(_by_id(parent, child)) is False
        parent = _item(
            "parent",
            phase=Phase.READY,
            metadata={
                "dependency_work_item_ids": ["child"],
                "dependency_settlement": {"failed": ["child"], "settled_at": "T00:00:00"},
            },
        )
        assert has_pending_settlement_release(_by_id(parent, child)) is False


class TestSettlementCascadeCancelIds:
    def test_empty_settlement_returns_empty(self):
        assert settlement_cascade_cancel_ids({}, {}) == set()

    def test_terminal_covered_and_unknown_excluded(self):
        fail = _item("fail", phase=Phase.FAILED)
        assert settlement_cascade_cancel_ids(_by_id(fail), {"stuck": ["fail"]}) == set()
        approved = _item("approved", phase=Phase.APPROVED)
        assert settlement_cascade_cancel_ids(_by_id(approved), {"stuck": ["approved"]}) == set()
        queued = _item("queued", phase=Phase.QUEUED)
        assert settlement_cascade_cancel_ids(_by_id(queued), {"stuck": ["ghost"]}) == set()

    def test_stuck_and_transitive_cancel(self):
        fail = _item("fail", phase=Phase.FAILED)
        stuck = _item(
            "stuck",
            phase=Phase.QUEUED,
            metadata={"dependency_work_item_ids": ["fail"]},
        )
        assert settlement_cascade_cancel_ids(_by_id(fail, stuck), {"stuck": ["stuck"]}) == {"stuck"}
        leaf = _item("leaf", phase=Phase.QUEUED, metadata={"dependency_work_item_ids": ["fail"]})
        mid = _item("mid", phase=Phase.QUEUED, metadata={"dependency_work_item_ids": ["leaf"]})
        top = _item("top", phase=Phase.QUEUED, metadata={"dependency_work_item_ids": ["mid"]})
        result = settlement_cascade_cancel_ids(_by_id(fail, leaf, mid, top), {"stuck": ["leaf"]})
        assert result == {"leaf", "mid", "top"}

    def test_soft_edge_does_not_close(self):
        fail = _item("fail", phase=Phase.FAILED)
        leaf = _item("leaf", phase=Phase.QUEUED, metadata={"dependency_work_item_ids": ["fail"]})
        mid = _item(
            "mid",
            phase=Phase.QUEUED,
            metadata={
                "dependency_work_item_ids": ["leaf", "fail"],
                "dependency_classes": {"leaf": "soft"},
            },
        )
        result = settlement_cascade_cancel_ids(_by_id(fail, leaf, mid), {"stuck": ["leaf"]})
        assert result == {"leaf"}


@pytest.fixture
def store(tmp_path: Any) -> Any:
    work_item_store = WorkItemStore(str(tmp_path / "work_items.db"))
    yield work_item_store
    work_item_store._conn.close()


def _save(store: Any, *items: Any) -> None:
    for item in items:
        store.save(item)


def _age(item: Any, *, minutes: int = 60) -> Any:
    item.updated_at = datetime.now(UTC) - timedelta(minutes=minutes)
    return item


class TestShouldEnterSynthesisTurn:
    def test_not_waiting_or_empty_dependencies_false(self):
        item = _item(phase=Phase.WAITING_DEPENDENCIES)
        assert _should_enter_synthesis_turn(item, {}, []) is False
        item = _item(phase=Phase.WAITING_FOR_CHILDREN)
        assert _should_enter_synthesis_turn(item, {}, []) is False

    def test_already_started_skip_kind_and_no_trigger_false(self):
        item = _item(phase=Phase.WAITING_FOR_CHILDREN)
        metadata = {"synthesis_turn_started": True}
        assert _should_enter_synthesis_turn(item, metadata, ["child"]) is False
        item = _item(phase=Phase.WAITING_FOR_CHILDREN, turn_type=WorkItemTurnType.AGGREGATE)
        metadata = {"delegated_children_pending": True}
        assert _should_enter_synthesis_turn(item, metadata, ["child"]) is False
        item = _item(phase=Phase.WAITING_FOR_CHILDREN)
        assert _should_enter_synthesis_turn(item, {}, ["child"]) is False

    def test_pending_children_and_frontier_trigger_true(self):
        item = _item(phase=Phase.WAITING_FOR_CHILDREN)
        metadata = {"delegated_children_pending": True}
        assert _should_enter_synthesis_turn(item, metadata, ["child"]) is True
        metadata = {"frontier": "waiting_for_children"}
        assert _should_enter_synthesis_turn(item, metadata, ["child"]) is True


class TestSynthesisTurnSummary:
    def test_plural_and_single_child_summaries(self):
        item = _item(title="build feature")
        summary = _synthesis_turn_summary(item, ["a", "b"])
        assert "2 approved child work items" in summary
        assert "`build feature`" in summary
        summary = _synthesis_turn_summary(item, ["a"])
        assert "1 approved child work item" in summary


class TestFailureTriageTurnSummary:
    def test_summary_includes_counts_and_stuck(self):
        item = _item(title="integrate results")
        phases = {"a": Phase.APPROVED, "b": Phase.FAILED, "c": Phase.CANCELLED}
        summary = _failure_triage_turn_summary(item, phases, ["b"], ["c"], [])
        assert "1 approved" in summary
        assert "2 failed/cancelled" in summary
        assert "`integrate results`" in summary
        phases = {"a": Phase.APPROVED, "b": Phase.QUEUED}
        summary = _failure_triage_turn_summary(item, phases, [], [], ["b"])
        assert "1 blocked downstream" in summary


class TestDetectStagnantWorkItems:
    def test_empty_and_none_store_return_empty(self, store: Any):
        assert detect_stagnant_work_items(store) == []
        assert detect_stagnant_work_items(None) == []

    def test_non_waiting_fresh_released_and_custom_stale_not_stagnant(self, store: Any):
        _save(store, _age(_item("wi-1", phase=Phase.READY), minutes=120))
        assert detect_stagnant_work_items(store) == []
        _save(store, _item("wi-1", phase=Phase.WAITING_DEPENDENCIES))
        assert detect_stagnant_work_items(store) == []
        item = _item(
            "wi-1",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_settlement": {"failed": ["a"]}},
        )
        _save(store, _age(item, minutes=120))
        assert detect_stagnant_work_items(store) == []
        _save(store, _age(_item("wi-1", phase=Phase.WAITING_DEPENDENCIES), minutes=60))
        assert detect_stagnant_work_items(store, stale_minutes=1000) == []

    def test_stagnant_waiting_dependencies_and_children(self, store: Any):
        _save(store, _age(_item("wi-1", phase=Phase.WAITING_DEPENDENCIES), minutes=120))
        result = detect_stagnant_work_items(store)
        assert len(result) == 1
        assert result[0]["id"] == "wi-1"
        assert result[0]["phase"] == "waiting_dependencies"
        assert result[0]["elapsed_minutes"] >= 120
        _save(store, _age(_item("wi-1", phase=Phase.WAITING_FOR_CHILDREN), minutes=90))
        result = detect_stagnant_work_items(store)
        assert [r["id"] for r in result] == ["wi-1"]


class TestRefreshDependents:
    def test_none_empty_and_bare_store_false(self, store: Any):
        assert refresh_dependents(None) is False
        assert refresh_dependents(store) is False

        class BareStore:
            pass

        assert refresh_dependents(BareStore()) is False

    def test_no_changes_false(self, store: Any):
        _save(store, _item("a", phase=Phase.APPROVED), _item("b", phase=Phase.READY))
        assert refresh_dependents(store) is False

    def test_all_approved_children_release_parent(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.APPROVED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.READY
        assert new_parent.metadata["waiting_on_work_item_ids"] == []
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        assert store.get("parent") is not None and store.get("parent").phase == Phase.RUNNING

    def test_rework_feedback_releases_to_rework(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"], "rework_feedback": "redo it"},
        )
        child = _item("child", phase=Phase.APPROVED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        assert (
            store.get("parent") is not None and store.get("parent").phase == Phase.READY_FOR_REWORK
        )

    def test_doomed_recomputed_only_on_phase_change(self, store: Any, monkeypatch: Any):
        import bandi.company.work_item_transition as wit

        # 4 张卡仅 1 张迁移 phase：其余 3 张无依赖变更不迁移。
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.APPROVED)
        stale_a = _item("stale_a", phase=Phase.WAITING_DEPENDENCIES)
        stale_b = _item("stale_b", phase=Phase.WAITING_DEPENDENCIES)
        stale_c = _item("stale_c", phase=Phase.WAITING_DEPENDENCIES)
        _save(store, parent, child, stale_a, stale_b, stale_c)
        real = wit.compute_doomed_work_item_ids
        calls = 0

        def counting(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(wit, "compute_doomed_work_item_ids", counting)
        assert refresh_dependents(store) is True
        assert calls <= 2

    def test_synthesis_turn_enters_ready(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={
                "delegated_children_pending": True,
                "dependency_work_item_ids": ["child"],
            },
        )
        child = _item("child", phase=Phase.APPROVED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.READY
        assert new_parent.metadata["work_kind"] == "synthesize"
        assert new_parent.metadata["synthesis_turn_started"] is True
        assert new_parent.metadata["frontier"] == "synthesis_ready"
        assert new_parent.metadata["synthesis_source_work_item_ids"] == ["child"]
        assert new_parent.metadata["waiting_on_work_item_ids"] == []

    def test_deliver_kind_writes_delivery_mode(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={"dependency_work_item_ids": ["child"], "work_kind": "deliver"},
            turn_type=WorkItemTurnType.DELIVER,
        )
        child = _item("child", phase=Phase.APPROVED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.RUNNING
        assert new_parent.metadata["work_kind"] == "delivery"
        assert new_parent.metadata["current_turn_mode"] == "deliver_required"

    def test_failure_settlement_releases_waiting_and_synthesis(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.FAILED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.RUNNING
        assert "failed" in new_parent.metadata["dependency_settlement"]
        assert new_parent.metadata["frontier"] == "settlement_ready"
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={
                "delegated_children_pending": True,
                "dependency_work_item_ids": ["child"],
            },
        )
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.READY
        assert new_parent.metadata["work_kind"] == "synthesize"
        assert "failed" in new_parent.metadata["dependency_settlement"]

    def test_rollup_settlement_release(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"], "work_kind": "aggregate"},
        )
        child = _item("child", phase=Phase.FAILED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        assert store.get("parent") is not None and store.get("parent").phase == Phase.READY

    def test_ordinary_waiting_not_released_over_failure(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.FAILED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.WAITING_DEPENDENCIES
        assert new_parent.metadata["waiting_on_work_item_ids"] == ["child"]

    def test_reverse_regressions_ready_and_running_to_waiting(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.READY,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        child = _item("child", phase=Phase.QUEUED)
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        assert (
            store.get("parent") is not None
            and store.get("parent").phase == Phase.WAITING_DEPENDENCIES
        )
        parent = _item(
            "parent",
            phase=Phase.RUNNING,
            metadata={"dependency_work_item_ids": ["child"]},
        )
        _save(store, parent, child)
        assert refresh_dependents(store) is True
        assert (
            store.get("parent") is not None
            and store.get("parent").phase == Phase.WAITING_FOR_CHILDREN
        )

    def test_released_card_is_idempotent(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.READY,
            metadata={
                "dependency_work_item_ids": ["child"],
                "dependency_settlement": {"failed": ["child"], "settled_at": "T00:00:00"},
            },
        )
        child = _item("child", phase=Phase.FAILED)
        _save(store, parent, child)
        assert refresh_dependents(store) is False

    def test_stamped_ready_and_running_cards_reopen_triage(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.READY,
            metadata={
                "dependency_work_item_ids": ["child1", "child2"],
                "dependency_settlement": {
                    "failed": ["child1"],
                    "settled_at": "T00:00:00",
                },
            },
        )
        child1 = _item("child1", phase=Phase.FAILED)
        child2 = _item("child2", phase=Phase.FAILED)
        _save(store, parent, child1, child2)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.WAITING_DEPENDENCIES
        assert "child2" in new_parent.metadata["dependency_settlement"]["failed"]
        parent = _item(
            "parent",
            phase=Phase.RUNNING,
            metadata={
                "dependency_work_item_ids": ["child1", "child2"],
                "dependency_settlement": {
                    "failed": ["child1"],
                    "settled_at": "T00:00:00",
                },
            },
        )
        _save(store, parent, child1, child2)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.WAITING_FOR_CHILDREN
        assert "child2" in new_parent.metadata["dependency_settlement"]["failed"]

    def test_stamped_rework_card_new_failure_reopens_triage(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.READY_FOR_REWORK,
            metadata={
                "dependency_work_item_ids": ["child1", "child2"],
                "dependency_settlement": {
                    "failed": ["child1"],
                    "settled_at": "T00:00:00",
                },
            },
        )
        child1 = _item("child1", phase=Phase.FAILED)
        child2 = _item("child2", phase=Phase.FAILED)
        _save(store, parent, child1, child2)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert new_parent is not None and new_parent.phase == Phase.WAITING_DEPENDENCIES
        assert "child2" in new_parent.metadata["dependency_settlement"]["failed"]

    def test_doomed_snapshot_recomputes_after_inpass_release(self, store: Any):
        fail = _item("fail", phase=Phase.FAILED)
        rollup = _item(
            "rollup",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["fail"], "work_kind": "aggregate"},
        )
        parent = _item(
            "parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={
                "dependency_work_item_ids": ["rollup"],
                "delegated_children_pending": True,
            },
        )
        _save(store, fail, rollup, parent)
        assert refresh_dependents(store) is True
        new_rollup = store.get("rollup")
        assert new_rollup is not None and new_rollup.phase == Phase.READY
        new_parent = store.get("parent")
        assert new_parent is not None
        assert "dependency_settlement" not in new_parent.metadata
        assert new_parent.phase == Phase.WAITING_FOR_CHILDREN

    def test_pruned_dependency_is_recorded(self, store: Any):
        parent = _item(
            "parent",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["ghost"]},
        )
        ghost = _item("ghost", phase=Phase.CANCELLED, metadata={"deleted_by_manager_tool": True})
        _save(store, parent, ghost)
        assert refresh_dependents(store) is True
        new_parent = store.get("parent")
        assert (
            new_parent is not None
            and "ghost" in new_parent.metadata["pruned_dependency_work_item_ids"]
        )

    def test_settlement_cascade_cancels_stuck_descendants(self, store: Any):
        fail = _item("fail", phase=Phase.FAILED)
        leaf = _item(
            "leaf",
            phase=Phase.QUEUED,
            metadata={"dependency_work_item_ids": ["fail"]},
        )
        triage = _item(
            "triage",
            phase=Phase.APPROVED,
            metadata={
                "dependency_settlement": {
                    "failed": ["fail"],
                    "stuck": ["leaf"],
                    "settled_at": "T00:00:00",
                }
            },
        )
        _save(store, fail, leaf, triage)
        assert refresh_dependents(store) is True
        assert store.get("leaf") is not None and store.get("leaf").phase == Phase.CANCELLED
        stamp = store.get("triage").metadata["dependency_settlement"]
        assert "cascaded_at" in stamp

    def test_reentrancy_guard_skips(self, store: Any):
        from bandi.company.work_item_transition import _REFRESH_IN_FLIGHT

        token = _REFRESH_IN_FLIGHT.set(frozenset({"active"}))
        try:
            assert refresh_dependents(store) is False
        finally:
            _REFRESH_IN_FLIGHT.reset(token)

    def test_settlement_cascade_partial_failure_retries(self, store: Any):
        from unittest.mock import patch

        import bandi.company.work_item_transition as wit

        fail = _item("fail", phase=Phase.FAILED)
        leaf_a = _item(
            "leaf-a",
            phase=Phase.QUEUED,
            metadata={"dependency_work_item_ids": ["fail"]},
        )
        leaf_b = _item(
            "leaf-b",
            phase=Phase.QUEUED,
            metadata={"dependency_work_item_ids": ["fail"]},
        )
        triage = _item(
            "triage",
            phase=Phase.APPROVED,
            metadata={
                "dependency_settlement": {
                    "failed": ["fail"],
                    "stuck": ["leaf-a", "leaf-b"],
                    "settled_at": "T00:00:00",
                }
            },
        )
        _save(store, fail, leaf_a, leaf_b, triage)

        real_transition = wit.transition_work_item

        def flaky(item, target_phase, target_store, reason=""):
            if item.id == "leaf-b":
                raise RuntimeError("boom")
            return real_transition(item, target_phase, target_store, reason)

        with patch.object(wit, "transition_work_item", side_effect=flaky):
            assert refresh_dependents(store) is True
        assert store.get("leaf-a") is not None and store.get("leaf-a").phase == Phase.CANCELLED
        assert store.get("leaf-b") is not None and store.get("leaf-b").phase == Phase.QUEUED
        stamp = store.get("triage").metadata["dependency_settlement"]
        assert "cascaded_at" not in stamp

        assert refresh_dependents(store) is True
        assert store.get("leaf-b") is not None and store.get("leaf-b").phase == Phase.CANCELLED
        stamp = store.get("triage").metadata["dependency_settlement"]
        assert "cascaded_at" in stamp


class TestCleanIdsNonIterable:
    def test_non_iterable_inputs_return_empty(self):
        assert _clean_ids(123) == []
        assert _clean_ids(None) == []

    def test_string_and_list_inputs_normalized(self):
        assert _clean_ids("wi-1") == ["wi-1"]
        assert _clean_ids(["wi-1", "wi-2", "wi-1"]) == ["wi-1", "wi-2"]


class TestDetectStagnantNaiveDatetime:
    def test_naive_or_missing_updated_at_skipped(self, store: Any):
        item = _item("wi-1", phase=Phase.WAITING_DEPENDENCIES)
        item.updated_at = datetime.now().replace(tzinfo=None)
        _save(store, item)
        assert detect_stagnant_work_items(store) == []

        class RefStore:
            def __init__(self, *items):
                self.items = list(items)

            def list_all(self):
                return self.items

            def save(self, item):
                return None

        item = _item("wi-1", phase=Phase.WAITING_DEPENDENCIES)
        del item.updated_at
        assert detect_stagnant_work_items(RefStore(item)) == []


class TestRefreshDependentsSaveFailureRestoresState:
    def test_save_failure_logs_warning_without_crash(self, caplog):
        class RefStore:
            def __init__(self, *items):
                self.items = list(items)

            def list_all(self):
                return self.items

            def save(self, item):
                return None

            def save_checked(self, item, expected_revision):
                raise RuntimeError("save boom")

        dep = _item("dep-1", phase=Phase.APPROVED)
        parent = _item(
            "parent-1",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={"dependency_work_item_ids": ["dep-1"]},
        )
        store = RefStore(dep, parent)

        with caplog.at_level("WARNING", logger="bandi.company.work_item_transition"):
            # 提交失败不崩溃、不静默——记入 warning（临时对象中间态无需还原
            # 下轮 refresh 用新 list_all 重算收敛）
            refresh_dependents(store)
        assert any("parent-1" in r.message for r in caplog.records)
        assert any("parent-1" in r.message and r.levelname == "WARNING" for r in caplog.records)
