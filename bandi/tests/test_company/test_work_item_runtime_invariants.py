from __future__ import annotations

import pytest

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.company.work_item_runtime_invariants import Severity, check_invariants


@pytest.fixture
def store(tmp_path: str) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "invariants.db"))


class TestCheckInvariants:
    def test_empty_store_produces_no_violations(self, store: WorkItemStore):
        result = check_invariants(store)
        assert len(result.violations) == 0

    def test_orphan_child_detected(self, store: WorkItemStore):
        child = create_work_item(
            title="orphan", turn_type=WorkItemTurnType.EXECUTE, parent_id="nonexistent"
        )
        store.save(child)
        result = check_invariants(store)
        assert any(
            v.category == "orphan" and v.severity == Severity.ERROR for v in result.violations
        )

    def test_cycle_detected(self, store: WorkItemStore):
        a = create_work_item(title="A", turn_type=WorkItemTurnType.EXECUTE)
        b = create_work_item(title="B", turn_type=WorkItemTurnType.EXECUTE, parent_id=a.id)
        a.children_ids.append(b.id)
        b.children_ids.append(a.id)
        store.save(a)
        store.save(b)
        result = check_invariants(store)
        assert any(
            v.category == "cycle" and v.severity == Severity.ERROR for v in result.violations
        )

    def test_terminal_with_children_warning(self, store: WorkItemStore):
        parent = create_work_item(
            title="done", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.APPROVED
        )
        child = create_work_item(
            title="child", turn_type=WorkItemTurnType.EXECUTE, parent_id=parent.id
        )
        parent.children_ids.append(child.id)
        store.save(parent)
        store.save(child)
        result = check_invariants(store)
        assert any(v.category == "terminal_with_children" for v in result.violations)

    def test_assignee_missing_for_rework(self, store: WorkItemStore):
        item = create_work_item(title="rework no owner", turn_type=WorkItemTurnType.EXECUTE)
        item.phase = Phase.READY_FOR_REWORK
        store.save(item)
        result = check_invariants(store)
        assert any(v.category == "assignee_missing" for v in result.violations)

    def test_running_without_output_warning(self, store: WorkItemStore):
        item = create_work_item(
            title="running empty", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        store.save(item)
        result = check_invariants(store)
        assert any(v.category == "running_no_output" for v in result.violations)

    def test_healthy_items_no_violations(self, store: WorkItemStore):
        a = create_work_item(title="parent", turn_type=WorkItemTurnType.EXECUTE)
        b = create_work_item(title="child", turn_type=WorkItemTurnType.EXECUTE, parent_id=a.id)
        a.children_ids.append(b.id)
        store.save(a)
        store.save(b)
        result = check_invariants(store)
        assert len(result.violations) == 0

    def test_has_errors_property(self, store: WorkItemStore):
        child = create_work_item(
            title="orphan", turn_type=WorkItemTurnType.EXECUTE, parent_id="ghost"
        )
        store.save(child)
        result = check_invariants(store)
        assert result.has_errors

    def test_has_warnings_only(self, store: WorkItemStore):
        item = create_work_item(
            title="running", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
        )
        store.save(item)
        result = check_invariants(store)
        assert not result.has_errors
        assert result.has_warnings

    def test_violation_to_dict(self, store: WorkItemStore):
        from bandi.company.work_item_runtime_invariants import InvariantViolation

        v = InvariantViolation(item_id="x", severity=Severity.ERROR, category="test", message="msg")
        d = v.to_dict()
        assert d["item_id"] == "x"
        assert d["severity"] == "error"
