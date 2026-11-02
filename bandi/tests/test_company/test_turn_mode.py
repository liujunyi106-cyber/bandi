from __future__ import annotations

from typing import Any

from bandi.company.phase import Phase, TurnMode
from bandi.company.turn_mode import (
    MANAGER_DISPATCH_TURN_METADATA_KEYS,
    infer_turn_mode,
    reset_manager_dispatch_turn_metadata,
)
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _item(
    item_id: str = "wi-1",
    *,
    phase: Phase = Phase.QUEUED,
    turn_type: WorkItemTurnType = WorkItemTurnType.EXECUTE,
    metadata: dict[str, Any] | None = None,
) -> Any:
    item = create_work_item(
        title="work item",
        turn_type=turn_type,
        metadata=metadata,
        phase=phase,
    )
    item.id = item_id
    return item


class TestManagerDispatchTurnMetadataKeys:
    def test_six_keys_aligned_with_reference(self) -> None:
        assert len(MANAGER_DISPATCH_TURN_METADATA_KEYS) == 6
        assert "manager_board_mutation_performed" in MANAGER_DISPATCH_TURN_METADATA_KEYS
        assert "manager_board_modified_work_item_ids" in MANAGER_DISPATCH_TURN_METADATA_KEYS
        assert "manager_board_deleted_work_item_ids" in MANAGER_DISPATCH_TURN_METADATA_KEYS
        assert "manager_no_delegation_justification" in MANAGER_DISPATCH_TURN_METADATA_KEYS
        assert "no_delegation_justification" in MANAGER_DISPATCH_TURN_METADATA_KEYS
        assert "manager_dispatch_guard_unresolved" in MANAGER_DISPATCH_TURN_METADATA_KEYS


class TestResetManagerDispatchTurnMetadata:
    def test_removes_all_manager_dispatch_keys(self) -> None:
        metadata = {
            "manager_board_mutation_performed": True,
            "manager_board_modified_work_item_ids": ["wi-2"],
            "manager_board_deleted_work_item_ids": ["wi-3"],
            "manager_no_delegation_justification": "no seats",
            "no_delegation_justification": "no seats",
            "manager_dispatch_guard_unresolved": True,
        }
        result = reset_manager_dispatch_turn_metadata(metadata)
        for key in MANAGER_DISPATCH_TURN_METADATA_KEYS:
            assert key not in result

    def test_keeps_unrelated_keys(self) -> None:
        metadata = {"dependency_work_item_ids": ["wi-9"], "frontier": "resumed"}
        result = reset_manager_dispatch_turn_metadata(metadata)
        assert result["dependency_work_item_ids"] == ["wi-9"]
        assert result["frontier"] == "resumed"

    def test_none_input_returns_empty_dict(self) -> None:
        assert reset_manager_dispatch_turn_metadata(None) == {}

    def test_empty_input_returns_empty_dict(self) -> None:
        assert reset_manager_dispatch_turn_metadata({}) == {}

    def test_does_not_mutate_original(self) -> None:
        metadata = {"manager_board_mutation_performed": True, "keep": 1}
        reset_manager_dispatch_turn_metadata(metadata)
        assert "manager_board_mutation_performed" in metadata
        assert metadata["keep"] == 1

    def test_returns_mutable_dict(self) -> None:
        result = reset_manager_dispatch_turn_metadata({"a": 1})
        result["b"] = 2
        assert result == {"a": 1, "b": 2}


class TestInferTurnModeReport:
    def test_report_flag_from_metadata(self) -> None:
        item = _item(metadata={"report_execution_work_item": True})
        assert infer_turn_mode(item) == TurnMode.REPORT

    def test_report_by_turn_type(self) -> None:
        item = _item(turn_type=WorkItemTurnType.REPORT)
        assert infer_turn_mode(item) == TurnMode.REPORT

    def test_report_outranks_review_flags(self) -> None:
        item = _item(
            metadata={"report_execution_work_item": True, "review_execution_work_item": True}
        )
        assert infer_turn_mode(item) == TurnMode.REPORT


class TestInferTurnModeReview:
    def test_review_entry_flag(self) -> None:
        item = _item()
        assert infer_turn_mode(item, is_review_entry=True) == TurnMode.REVIEW

    def test_review_flag_from_metadata(self) -> None:
        item = _item(metadata={"review_execution_work_item": True})
        assert infer_turn_mode(item) == TurnMode.REVIEW

    def test_review_by_turn_type(self) -> None:
        item = _item(turn_type=WorkItemTurnType.REVIEW)
        assert infer_turn_mode(item) == TurnMode.REVIEW

    def test_review_phase_alone_not_enough(self) -> None:
        item = _item(phase=Phase.AWAITING_MANAGER_REVIEW)
        assert infer_turn_mode(item) == TurnMode.REVIEW


class TestInferTurnModeRework:
    def test_rework_by_phase(self) -> None:
        item = _item(phase=Phase.READY_FOR_REWORK)
        assert infer_turn_mode(item) == TurnMode.REWORK

    def test_rework_by_feedback_trail_when_running(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"rework_feedback": "needs more detail"})
        assert infer_turn_mode(item) == TurnMode.REWORK

    def test_rework_by_review_rework_count(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"review_rework_count": 2})
        assert infer_turn_mode(item) == TurnMode.REWORK

    def test_empty_feedback_and_zero_count_not_rework(self) -> None:
        item = _item(
            phase=Phase.RUNNING, metadata={"rework_feedback": "", "review_rework_count": 0}
        )
        assert infer_turn_mode(item) != TurnMode.REWORK


class TestInferTurnModeIntegrate:
    def test_integrate_with_dependencies_running(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"dependency_work_item_ids": ["wi-2"]})
        assert infer_turn_mode(item) == TurnMode.INTEGRATE

    def test_integrate_with_dependencies_ready(self) -> None:
        item = _item(phase=Phase.READY, metadata={"dependency_work_item_ids": ["wi-2"]})
        assert infer_turn_mode(item) == TurnMode.INTEGRATE

    def test_integrate_with_waiting_for_children(self) -> None:
        item = _item(
            phase=Phase.WAITING_FOR_CHILDREN, metadata={"dependency_work_item_ids": ["wi-2"]}
        )
        assert infer_turn_mode(item) == TurnMode.INTEGRATE

    def test_chain_step_dependency_executes_not_integrates(self) -> None:
        # 顺序链上的执行卡：依赖满足后应执行自己的交付物，而不是父卡聚合轮
        item = _item(
            phase=Phase.READY,
            metadata={"dependency_work_item_ids": ["wi-1"], "chain_step": True},
        )
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_integrate_with_frontier_resumed(self) -> None:
        item = _item(
            phase=Phase.QUEUED,
            metadata={"frontier": "resumed", "dependency_work_item_ids": ["wi-2"]},
        )
        assert infer_turn_mode(item) == TurnMode.INTEGRATE

    def test_frontier_resumed_alone_not_integrate(self) -> None:
        item = _item(phase=Phase.QUEUED, metadata={"frontier": "resumed"})
        assert infer_turn_mode(item) != TurnMode.INTEGRATE

    def test_integrate_outranks_delegate(self) -> None:
        item = _item(
            phase=Phase.RUNNING,
            metadata={
                "dependency_work_item_ids": ["wi-2"],
                "allowed_delegate_role_ids": ["worker"],
            },
        )
        assert infer_turn_mode(item) == TurnMode.INTEGRATE


class TestInferTurnModeDelegate:
    def test_delegate_with_allowed_roles(self) -> None:
        item = _item(metadata={"allowed_delegate_role_ids": ["worker"]})
        assert infer_turn_mode(item) == TurnMode.DELEGATE

    def test_delegate_without_dependencies(self) -> None:
        item = _item(phase=Phase.READY, metadata={"allowed_delegate_role_ids": ["worker"]})
        assert infer_turn_mode(item) == TurnMode.DELEGATE


class TestInferTurnModeExecute:
    def test_default_execute(self) -> None:
        item = _item()
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_execute_with_running_phase(self) -> None:
        item = _item(phase=Phase.RUNNING)
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_execute_with_none_metadata(self) -> None:
        item = _item(metadata=None)
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_execute_with_empty_dependency_list(self) -> None:
        item = _item(metadata={"dependency_work_item_ids": []})
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_execute_with_empty_allowed_roles(self) -> None:
        item = _item(metadata={"allowed_delegate_role_ids": []})
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_execute_unknown_turn_type(self) -> None:
        item = _item()
        item.turn_type = "mystery"  # type: ignore[assignment]
        assert infer_turn_mode(item) == TurnMode.EXECUTE


class TestInferTurnModeRobustness:
    def test_object_without_attributes_defaults_execute(self) -> None:
        class Bare:
            pass

        assert infer_turn_mode(Bare()) == TurnMode.EXECUTE

    def test_string_phase_coerced(self) -> None:
        item = _item(phase=Phase.READY_FOR_REWORK)
        item.phase = "ready_for_rework"  # type: ignore[assignment]
        assert infer_turn_mode(item) == TurnMode.REWORK

    def test_invalid_phase_string_defaults_execute(self) -> None:
        item = _item()
        item.phase = "not-a-phase"  # type: ignore[assignment]
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_dependency_ids_string_normalized(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"dependency_work_item_ids": "wi-2"})
        assert infer_turn_mode(item) == TurnMode.INTEGRATE

    def test_priority_chain_full(self) -> None:
        item = _item(
            phase=Phase.READY_FOR_REWORK,
            turn_type=WorkItemTurnType.REVIEW,
            metadata={
                "report_execution_work_item": True,
                "dependency_work_item_ids": ["wi-2"],
                "allowed_delegate_role_ids": ["worker"],
            },
        )
        assert infer_turn_mode(item) == TurnMode.REPORT


class TestInferTurnModeRobustDirtyInput:
    def test_non_numeric_review_rework_count_does_not_crash(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"review_rework_count": "abc"})
        assert infer_turn_mode(item) == TurnMode.EXECUTE

    def test_none_review_rework_count_no_rework(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"review_rework_count": None})
        assert infer_turn_mode(item) != TurnMode.REWORK

    def test_dict_rework_feedback_ignored(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"rework_feedback": {"detail": "x"}})
        assert infer_turn_mode(item) != TurnMode.REWORK

    def test_negative_rework_count_not_rework(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"review_rework_count": -1})
        assert infer_turn_mode(item) != TurnMode.REWORK

    def test_zero_rework_count_not_rework(self) -> None:
        item = _item(phase=Phase.RUNNING, metadata={"review_rework_count": 0})
        assert infer_turn_mode(item) != TurnMode.REWORK
