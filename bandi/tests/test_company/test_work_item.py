from __future__ import annotations

from datetime import datetime

import pytest

from bandi.company.phase import InvalidPhaseTransitionError, Phase
from bandi.company.work_item import (
    CANONICAL_WORK_ITEM_TURN_TYPES,
    WorkItemSnapshot,
    WorkItemStore,
    WorkItemTurnType,
    create_work_item,
    normalize_turn_type,
    transition_work_item,
)


class TestWorkItemTurnType:
    def test_canonical_count(self):
        assert len(CANONICAL_WORK_ITEM_TURN_TYPES) == 12

    def test_normalize_valid(self):
        assert normalize_turn_type("execute") == WorkItemTurnType.EXECUTE

    def test_normalize_alias(self):
        assert normalize_turn_type("exec") == WorkItemTurnType.EXECUTE

    def test_normalize_invalid(self):
        with pytest.raises(ValueError, match="Unknown turn type"):
            normalize_turn_type("invalid_type")


class TestWorkItemCreate:
    def test_create_defaults(self):
        item = create_work_item(title="test task", turn_type=WorkItemTurnType.EXECUTE)
        assert item.title == "test task"
        assert item.turn_type == WorkItemTurnType.EXECUTE
        assert item.phase == Phase.QUEUED
        assert item.parent_id is None
        assert item.children_ids == []
        assert isinstance(item.created_at, datetime)

    def test_create_with_parent(self):
        item = create_work_item(
            title="sub task", turn_type=WorkItemTurnType.EXECUTE, parent_id="parent-1"
        )
        assert item.parent_id == "parent-1"

    def test_create_with_assignee(self):
        item = create_work_item(
            title="assigned task", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        assert item.assignee_role == "worker"

    def test_snapshot_immutability(self):
        item = create_work_item(title="snapshot test", turn_type=WorkItemTurnType.EXECUTE)
        snap = item.snapshot()
        assert isinstance(snap, WorkItemSnapshot)
        assert snap.title == item.title
        assert snap.phase == item.phase


class TestTransition:
    def test_valid_transition(self, mock_store):
        item = create_work_item(title="transition test", turn_type=WorkItemTurnType.EXECUTE)
        updated = transition_work_item(item, Phase.READY, store=mock_store)
        assert updated.phase == Phase.READY
        assert updated.updated_at >= item.created_at

    def test_invalid_transition_raises(self, mock_store):
        item = create_work_item(title="invalid transition", turn_type=WorkItemTurnType.EXECUTE)
        with pytest.raises(InvalidPhaseTransitionError):
            transition_work_item(item, Phase.APPROVED, store=mock_store)

    def test_transition_updates_store(self, mock_store):
        item = create_work_item(title="persistence", turn_type=WorkItemTurnType.EXECUTE)
        transition_work_item(item, Phase.READY, store=mock_store)
        mock_store.save.assert_called_once()

    def test_transition_same_phase_noop(self, mock_store):
        item = create_work_item(title="noop", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.READY)
        updated = transition_work_item(item, Phase.READY, store=mock_store)
        assert updated is item
        mock_store.save.assert_not_called()

    def test_transition_same_phase_with_reason_persists(self, mock_store):
        item = create_work_item(
            title="reasoned", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.READY
        )
        transition_work_item(item, Phase.READY, store=mock_store, reason="re-ack")
        assert item.runtime_metadata["last_transition_reason"] == "re-ack"
        mock_store.save.assert_called_once()


@pytest.fixture
def mock_store():
    import unittest.mock

    store = unittest.mock.Mock(spec=WorkItemStore)
    store.save.return_value = None
    return store
