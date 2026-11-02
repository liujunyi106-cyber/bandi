from __future__ import annotations

import pytest

from bandi.company.phase import (
    ALLOWED_TRANSITIONS,
    InvalidPhaseTransitionError,
    InvalidPhaseValueError,
    Phase,
    TurnMode,
    classify_turn_for_phase,
    coerce_phase,
    is_terminal,
    kanban_column,
    validate_transition,
    verdict,
)


class TestPhaseEnum:
    def test_phase_values(self):
        assert len(Phase) == 14
        assert Phase.QUEUED.value == "queued"
        assert Phase.READY.value == "ready"
        assert Phase.READY_FOR_REWORK.value == "ready_for_rework"
        assert Phase.WAITING_DEPENDENCIES.value == "waiting_dependencies"
        assert Phase.RUNNING.value == "running"
        assert Phase.WAITING_FOR_PEER.value == "waiting_for_peer"
        assert Phase.WAITING_FOR_CHILDREN.value == "waiting_for_children"
        assert Phase.PAUSED.value == "paused"
        assert Phase.NEEDS_ATTENTION.value == "needs_attention"
        assert Phase.AWAITING_MANAGER_REVIEW.value == "awaiting_manager_review"
        assert Phase.AWAITING_HUMAN.value == "awaiting_human"
        assert Phase.APPROVED.value == "approved"
        assert Phase.FAILED.value == "failed"
        assert Phase.CANCELLED.value == "cancelled"

    def test_terminal_phases(self):
        terminal = {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}
        for p in Phase:
            if p in terminal:
                assert is_terminal(p)
            else:
                assert not is_terminal(p)

    def test_kanban_column_mapping(self):
        assert kanban_column(Phase.QUEUED) == "todo"
        assert kanban_column(Phase.READY) == "todo"
        assert kanban_column(Phase.WAITING_DEPENDENCIES) == "todo"
        assert kanban_column(Phase.READY_FOR_REWORK) == "todo"
        assert kanban_column(Phase.RUNNING) == "in_progress"
        assert kanban_column(Phase.WAITING_FOR_PEER) == "in_progress"
        assert kanban_column(Phase.WAITING_FOR_CHILDREN) == "in_progress"
        assert kanban_column(Phase.PAUSED) == "todo"
        assert kanban_column(Phase.NEEDS_ATTENTION) == "in_progress"
        assert kanban_column(Phase.AWAITING_MANAGER_REVIEW) == "in_review"
        assert kanban_column(Phase.AWAITING_HUMAN) == "in_review"
        assert kanban_column(Phase.APPROVED) == "done"
        assert kanban_column(Phase.FAILED) == "done"
        assert kanban_column(Phase.CANCELLED) == "done"

    def test_verdict_approve(self):
        assert verdict(Phase.APPROVED) == "approve"

    def test_verdict_rework(self):
        assert verdict(Phase.READY_FOR_REWORK) == "rework"

    def test_verdict_fail(self):
        assert verdict(Phase.FAILED) == "fail"

    def test_verdict_cancel(self):
        assert verdict(Phase.CANCELLED) == "cancel"

    def test_verdict_other(self):
        for p in Phase:
            if p not in (Phase.APPROVED, Phase.READY_FOR_REWORK, Phase.FAILED, Phase.CANCELLED):
                assert verdict(p) is None


class TestTransitions:
    def test_allowed_transitions_covers_all_phases(self):
        for p in Phase:
            assert p in ALLOWED_TRANSITIONS, f"{p} missing transition definition"

    def test_ready_to_running(self):
        assert Phase.RUNNING in ALLOWED_TRANSITIONS[Phase.READY]

    def test_running_to_waiting_review(self):
        assert Phase.AWAITING_MANAGER_REVIEW in ALLOWED_TRANSITIONS[Phase.RUNNING]

    def test_cancel_always_allowed(self):
        for src, targets in ALLOWED_TRANSITIONS.items():
            if not is_terminal(src):
                assert Phase.CANCELLED in targets, f"{src} missing CANCELLED"

    def test_fail_always_allowed(self):
        for src, targets in ALLOWED_TRANSITIONS.items():
            if not is_terminal(src):
                assert Phase.FAILED in targets, f"{src} missing FAILED"

    def test_no_transition_from_failed_cancelled(self):
        for p in (Phase.FAILED, Phase.CANCELLED):
            assert len(ALLOWED_TRANSITIONS[p]) == 0

    def test_approved_has_single_reactivation_exit(self):
        assert ALLOWED_TRANSITIONS[Phase.APPROVED] == frozenset({Phase.READY})

    def test_approved_to_ready_valid(self):
        validate_transition(Phase.APPROVED, Phase.READY)

    def test_approved_to_other_still_invalid(self):
        with pytest.raises(InvalidPhaseTransitionError):
            validate_transition(Phase.APPROVED, Phase.RUNNING)

    def test_validate_transition_valid(self):
        validate_transition(Phase.READY, Phase.RUNNING)

    def test_validate_transition_invalid(self):
        with pytest.raises(InvalidPhaseTransitionError):
            validate_transition(Phase.READY, Phase.APPROVED)


class TestTurnMode:
    def test_turn_mode_values(self):
        assert len(TurnMode) == 6
        assert TurnMode.EXECUTE.value == "execute"
        assert TurnMode.DELEGATE.value == "delegate"
        assert TurnMode.REVIEW.value == "review"
        assert TurnMode.INTEGRATE.value == "integrate"
        assert TurnMode.REWORK.value == "rework"
        assert TurnMode.REPORT.value == "report"

    def test_classify_execute(self):
        assert classify_turn_for_phase(Phase.RUNNING) == TurnMode.EXECUTE

    def test_classify_delegate(self):
        assert classify_turn_for_phase(Phase.READY) == TurnMode.DELEGATE

    def test_classify_review(self):
        assert classify_turn_for_phase(Phase.AWAITING_MANAGER_REVIEW) == TurnMode.REVIEW

    def test_classify_integrate(self):
        assert classify_turn_for_phase(Phase.WAITING_FOR_CHILDREN) == TurnMode.INTEGRATE

    def test_classify_rework(self):
        assert classify_turn_for_phase(Phase.READY_FOR_REWORK) == TurnMode.REWORK

    def test_classify_report_default(self):
        assert classify_turn_for_phase(Phase.APPROVED) == TurnMode.REPORT


class TestCoercePhase:
    def test_coerce_from_phase(self):
        assert coerce_phase(Phase.RUNNING) is Phase.RUNNING

    def test_coerce_from_valid_string(self):
        assert coerce_phase("running") == Phase.RUNNING

    def test_coerce_from_another_valid_string(self):
        assert coerce_phase("paused") == Phase.PAUSED

    def test_coerce_invalid_string_raises(self):
        with pytest.raises(InvalidPhaseValueError):
            coerce_phase("nonexistent_phase")
